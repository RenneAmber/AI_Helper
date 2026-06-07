# medical_agent.py
"""医疗 Agent 核心编排模块（新手阅读版）。

整体框架：
1) Planner 阶段：让大模型只产出结构化任务计划（不直接回答医学结论）。
2) Slot 阶段：缺参数时只做定向槽位提取，避免每轮都重规划带来的漂移。
3) Execute 阶段：严格校验计划/参数后再调用工具，输出可追踪执行轨迹。
4) Summary 阶段：只允许基于工具结果总结，且 sources 必须来自允许集合。

核心目标：把“模型自由发挥”收敛为“模型受控编排 + 工具事实驱动”。
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# ============ Planner Prompt（严格 JSON 计划） ============
MEDICAL_PLANNER_SYSTEM = (
    "你是一个医疗任务编排助手（仅做流程编排，不提供诊断）。\n"
    "你只能产生一个 JSON 对象，禁止输出任何额外文字。\n\n"
    "【任务及其 args 标准字段——必须使用以下确切 key，不得自创字段名】\n"
    "1. REGISTRATION: {\"hospital\":string, \"department\":string, \"preferred_time\":string, \"doctor\":string|null}\n"
    "2. QUERY: {\"query_type\":\"DOCTOR_LIST|LAB_REPORT|IMAGING|REG_RECORD|VISIT_RECORD\", \"hospital\":string|null, \"department\":string|null}\n"
    "3. INTERPRET: {\"report\":object|null, \"report_id\":string|null}\n"
    "4. EMR_INTAKE: {\"chief_complaint\":string, \"symptoms\":[string]|null, \"medical_history\":[string]|null, \"vital_signs\":object|null}\n"
    "5. CHRONIC_DISEASE_MGMT: {\"action\":\"CREATE|GET_REMINDERS|CHECK_WARNING\", \"disease_name\":string, \"diagnosis_date\":string|null, \"last_checkup_date\":string|null}\n\n"
    "【任务识别规则】\n"
    "- 挂号需求（有时间、医院、科室）→ REGISTRATION\n"
    "- 查询需求（医生/报告/记录）→ QUERY\n"
    "- 报告解读需求 → INTERPRET\n"
    "- 初诊填表、病例采集 → EMR_INTAKE\n"
    "- 慢病档案创建/提醒/预警 → CHRONIC_DISEASE_MGMT\n"
    "- 多个需求 → 组合用 MULTI_TASK，依赖关系用 depends_on\n\n"
    "【从消息中主动推断槽位，不得无故置 null】\n"
    "- preferred_time：消息中任何时间表达都算，如「明天上午10点」「2026-04-04 10:00」「下周三」\n"
    "- hospital：任何医院名称\n"
    "- department：任何科室，如「心内科」「皮肤科」\n"
    "- chief_complaint：患者主诉症状，从消息直接提取\n"
    "- symptoms 和 medical_history：从医疗相关内容提取成列表\n"
    "- disease_name：「高血压」|「糖尿病」|「冠心病」 等\n"
    "- action：「CREATE」创建档案|「GET_REMINDERS」获取提醒|「CHECK_WARNING」检查预警\n\n"
    "【槽位合并——必须严格遵守】\n"
    "1. 「已填槽位」中所有 key-value 必须原样保留在对应 task.args 中，禁止丢弃。\n"
    "2. 用当前用户消息补充或覆盖（更具体的值优先）。\n"
    "3. missing_slots 只列出最终仍为 null/空 的必需槽位。\n\n"
    "【多任务依赖示例】\n"
    "用户：「我胸闷呼吸困难，想挂心内科\"\n"
    "推荐：[EMR_INTAKE(first), REGISTRATION(depends_on S1)]\n"
    "说明：EMR 先生成病历和严重程度，REGISTRATION 再基于此挂号\n\n"
    "输出 JSON（无额外字符）：\n"
    "{\"intent\":\"MULTI_TASK|REGISTRATION|QUERY|INTERPRET|EMR_INTAKE|CHRONIC_DISEASE_MGMT|UNKNOWN\","
    "\"patient_id\":\"string|null\","
    "\"plan\":[{\"task\":\"REGISTRATION|QUERY|INTERPRET|EMR_INTAKE|CHRONIC_DISEASE_MGMT\",\"args\":{},\"depends_on\":[]}],"
    "\"missing_slots\":[{\"slot\":\"string\",\"question\":\"string\"}]}"
)

MEDICAL_PLANNER_USER_TMPL = (
    "用户当前输入：{message}\n\n"
    "已填槽位（必须全部保留，禁止重复追问）：\n{prior_slots}\n\n"
    "其他已知信息：{known_extra}\n"
)

# ── 槽位提取 Prompt（只在追问回复轮使用，比全量规划更可靠） ──────────────────
SLOT_EXTRACTOR_SYSTEM = (
    "你是信息提取助手。从用户消息中提取指定槽位的值。\n"
    "只输出 JSON 对象，key 为槽位名（如 REGISTRATION.preferred_time），value 为提取到的字符串。\n"
    "禁止输出任何其他文字。若某槽位无法从消息中提取，其值为 null。\n"
    "时间表达示例：「明天上午10点」「下周三上午」「2026-04-04 10:00」均为合法的 preferred_time 值，直接原文保留。"
)

SLOT_EXTRACTOR_USER_TMPL = (
    "用户消息：{message}\n\n"
    "需提取的槽位（task.slot : 追问问题）：\n{slots_desc}\n\n"
    "输出格式：{{\"TASK.slot\": \"提取值或 null\"}}"
)

# ============ Summarizer Prompt（严格 JSON 给用户） ============
MEDICAL_SUMMARY_SYSTEM = (
    "你是一个医疗流程结果汇总助手。你必须：\n"
    "1) 只基于 tools_result 给出总结，不得编造。\n"
    "2) 不提供诊断和处方，只做信息解释与就医建议。\n"
    "3) 只允许在 sources 中填写 allowed_sources 提供的值（格式如 tool:S1:QUERY）。\n"
    "4) 如果 execution 中没有任何成功步骤，answer 必须是‘我不知道’，sources 必须为空。\n"
    "3) 输出严格 JSON："
    "{\"answer\":\"string\",\"sources\":[],\"confidence\":number(0~1)}\n"
    "5) answer 内必须包含免责声明：仅供参考，不替代医生诊断。\n"
)

MEDICAL_SUMMARY_USER_TMPL = (
    "用户原始诉求：{message}\n"
    "工具调用结果（JSON）：\n{tools_result}\n"
)

# ============ 工具任务缺参规则（MVP） ============
REQUIRED_SLOTS = {
    "REGISTRATION": ["hospital", "department", "preferred_time"],
    "QUERY": ["query_type"],  # report/registration/etc
    "INTERPRET": ["report"],  # 或 report_id；MVP 用 report
    # 新增任务
    "EMR_INTAKE": ["chief_complaint"],
    "CHRONIC_DISEASE_MGMT": ["action", "disease_name"],
}

VALID_TASKS = {"REGISTRATION", "QUERY", "INTERPRET", "EMR_INTAKE", "CHRONIC_DISEASE_MGMT"}
VALID_QUERY_TYPES = {"DOCTOR_LIST", "LAB_REPORT", "IMAGING", "REG_RECORD", "VISIT_RECORD"}
VALID_CHRONIC_ACTIONS = {"CREATE", "GET_REMINDERS", "CHECK_WARNING"}

CHRONIC_DISEASE_NAMES = ["高血压", "糖尿病", "冠心病"]
BOOKING_WORDS = ["挂号", "预约", "门诊", "问诊", "挂", "加号"]
INTAKE_WORDS = ["主诉", "症状", "胸闷", "呼吸困难", "头晕", "腹痛", "发热", "心悸"]
REMINDER_WORDS = ["提醒", "复查", "复诊", "配药", "随访"]
CREATE_ARCHIVE_WORDS = ["建档", "档案", "慢病管家", "管理"]
WARNING_WORDS = ["预警", "风险", "危险", "异常"]
# 用户对"是否需要预约"确认回复的关键词
CONFIRM_WORDS = ["是", "好的", "好", "需要", "帮我预约", "帮我挂", "预约", "可以", "行",
                 "同意", "确认", "继续", "ok", "OK", "要", "麻烦帮我", "安排"]


def _is_booking_confirmation(text: str) -> bool:
    """判断用户是否在回复"是否需要预约"的确认。"""
    return any(w in (text or "") for w in CONFIRM_WORDS)

DEPARTMENT_KEYWORDS = [
    "心内科", "内分泌科", "呼吸科", "呼吸内科", "消化科", "消化内科",
    "神经内科", "普通内科", "皮肤科", "骨科", "妇科", "儿科",
]

def parse_json_strict(s: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(s)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def _contains_any(text: str, words: List[str]) -> bool:
    return any(word in text for word in words)


def _extract_hospital(text: str) -> Optional[str]:
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,30}(?:医院|院区|总院|分院))", text)
    return match.group(1) if match else None


def _extract_preferred_time(text: str) -> Optional[str]:
    time_patterns = [
        r"今天(?:上午|下午|晚上)?",
        r"明天(?:上午|下午|晚上)?",
        r"后天(?:上午|下午|晚上)?",
        r"下周[一二三四五六日天](?:上午|下午|晚上)?",
        r"\d{4}-\d{2}-\d{2}(?:\s*\d{1,2}:\d{2})?",
        r"\d{1,2}月\d{1,2}日(?:上午|下午|晚上)?",
    ]
    for pattern in time_patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    return None


def _extract_department(text: str) -> Optional[str]:
    for department in DEPARTMENT_KEYWORDS:
        if department in text:
            return department
    if _contains_any(text, ["胸闷", "胸痛", "心悸", "高血压"]):
        return "心内科"
    if _contains_any(text, ["糖尿病", "血糖"]):
        return "内分泌科"
    if _contains_any(text, ["咳嗽", "呼吸困难"]):
        return "呼吸内科"
    return None


def _extract_bp(text: str) -> Optional[Dict[str, Any]]:
    match = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", text)
    if not match:
        return None
    systolic, diastolic = match.groups()
    return {"bp": f"{systolic}/{diastolic}", "systolic": int(systolic), "diastolic": int(diastolic)}


def _extract_symptoms(text: str) -> List[str]:
    symptom_keywords = [
        "胸闷", "胸痛", "呼吸困难", "心悸", "头晕", "头痛", "发热", "咳嗽",
        "腹痛", "腹泻", "呕吐", "乏力", "视物模糊", "多尿", "口渴",
    ]
    return [item for item in symptom_keywords if item in text]


def _extract_medical_history(text: str) -> List[str]:
    histories: List[str] = []
    for disease in CHRONIC_DISEASE_NAMES:
        if disease in text:
            duration_match = re.search(rf"{disease}(?:史)?(\d+年)?", text)
            if duration_match and duration_match.group(1):
                histories.append(f"{disease}{duration_match.group(1)}")
            else:
                histories.append(disease)
    return histories


def _build_heuristic_plan(message: str, patient_id: str) -> Optional[Dict[str, Any]]:
    text = message.strip()
    found_diseases = [name for name in CHRONIC_DISEASE_NAMES if name in text]
    symptoms = _extract_symptoms(text)

    # 急性症状优先：避免“高血压”把胸闷/呼吸困难场景误导到慢病预警。
    if symptoms and (_contains_any(text, BOOKING_WORDS) or _contains_any(text, INTAKE_WORDS)):
        emr_args: Dict[str, Any] = {
            "chief_complaint": "、".join(symptoms) if symptoms else text,
            "symptoms": symptoms,
            "medical_history": _extract_medical_history(text),
        }
        bp = _extract_bp(text)
        if bp:
            emr_args["vital_signs"] = {"bp": bp["bp"]}
        plan = [{"task": "EMR_INTAKE", "args": emr_args, "depends_on": []}]
        # 不在此处追加 REGISTRATION：先完成问诊评估，再由用户主动确认是否预约。
        return {"intent": "EMR_INTAKE", "patient_id": patient_id, "plan": plan, "missing_slots": []}

    if found_diseases and _contains_any(text, REMINDER_WORDS):
        plan = []
        for disease in found_diseases:
            plan.append({
                "task": "CHRONIC_DISEASE_MGMT",
                "args": {"action": "GET_REMINDERS", "disease_name": disease},
                "depends_on": [],
            })
        return {"intent": "MULTI_TASK" if len(plan) > 1 else "CHRONIC_DISEASE_MGMT", "patient_id": patient_id, "plan": plan, "missing_slots": []}

    if found_diseases and _contains_any(text, CREATE_ARCHIVE_WORDS):
        plan = []
        for disease in found_diseases:
            plan.append({
                "task": "CHRONIC_DISEASE_MGMT",
                "args": {
                    "action": "CREATE",
                    "disease_name": disease,
                    "diagnosis_date": datetime.now().date().isoformat(),
                },
                "depends_on": [],
            })
        return {"intent": "MULTI_TASK" if len(plan) > 1 else "CHRONIC_DISEASE_MGMT", "patient_id": patient_id, "plan": plan, "missing_slots": []}

    if found_diseases and (_contains_any(text, WARNING_WORDS) or _extract_bp(text) is not None):
        bp = _extract_bp(text) or {}
        args: Dict[str, Any] = {"action": "CHECK_WARNING", "disease_name": found_diseases[0]}
        if bp.get("systolic") is not None:
            args["systolic"] = bp["systolic"]
            args["diastolic"] = bp["diastolic"]
        glucose_match = re.search(r"血糖\s*(\d+(?:\.\d+)?)", text)
        if glucose_match:
            args["blood_glucose"] = float(glucose_match.group(1))
        return {"intent": "CHRONIC_DISEASE_MGMT", "patient_id": patient_id, "plan": [{"task": "CHRONIC_DISEASE_MGMT", "args": args, "depends_on": []}], "missing_slots": []}

    return None

def normalize_plan(plan_obj: Dict[str, Any]) -> Dict[str, Any]:
    # 轻量校验 + 归一化
    intent = plan_obj.get("intent", "UNKNOWN")
    patient_id = plan_obj.get("patient_id", None)
    plan = plan_obj.get("plan", []) or []
    missing = plan_obj.get("missing_slots", []) or []
    if not isinstance(plan, list) or not isinstance(missing, list):
        return {"intent": "UNKNOWN", "patient_id": patient_id, "plan": [], "missing_slots": []}
    # 确保字段存在，并给每个步骤稳定 step_id（用于依赖与可追踪执行）
    for idx, step in enumerate(plan, start=1):
        step.setdefault("step_id", f"S{idx}")
        step.setdefault("depends_on", [])
        step.setdefault("args", {})
        if not isinstance(step["depends_on"], list):
            step["depends_on"] = []
        if not isinstance(step["args"], dict):
            step["args"] = {}
        if isinstance(step.get("task"), str):
            step["task"] = step["task"].upper()
    return {"intent": intent, "patient_id": patient_id, "plan": plan, "missing_slots": missing}


def _required_slots_for_step(step: Dict[str, Any]) -> List[str]:
    task = step.get("task")
    args = step.get("args", {}) or {}
    if task == "INTERPRET":
        # INTERPRET 支持 report 或 report_id 任一存在
        report = args.get("report")
        report_id = args.get("report_id")
        has_report = report not in (None, "", "null", "None")
        has_report_id = report_id not in (None, "", "null", "None")
        return [] if (has_report or has_report_id) else ["report"]
    return REQUIRED_SLOTS.get(task, [])


def _missing_required_slots(step: Dict[str, Any]) -> List[str]:
    args = step.get("args", {}) or {}
    missing: List[str] = []
    for slot in _required_slots_for_step(step):
        val = args.get(slot, None)
        if val is None or str(val).strip().lower() in ("", "null", "none"):
            missing.append(slot)
    return missing


def validate_plan_structure(plan: List[Dict[str, Any]]) -> List[str]:
    """检查计划结构合法性。

    为什么需要这一步：
    - LLM 计划可能出现拼写错误、依赖缺失、重复 step_id。
    - 先做结构校验可以把“幻觉计划”在执行前拦截。
    """
    errors: List[str] = []
    if not plan:
        return ["plan 为空"]

    ids: List[str] = []
    for idx, step in enumerate(plan, start=1):
        sid = str(step.get("step_id") or f"S{idx}")
        step["step_id"] = sid
        ids.append(sid)
        task = step.get("task")
        if task not in VALID_TASKS:
            errors.append(f"{sid}: 不支持的 task={task}")

    id_set = set(ids)
    if len(id_set) != len(ids):
        errors.append("step_id 重复")

    for step in plan:
        sid = step["step_id"]
        deps = step.get("depends_on", []) or []
        if not isinstance(deps, list):
            errors.append(f"{sid}: depends_on 必须是数组")
            continue
        for d in deps:
            if d not in id_set:
                errors.append(f"{sid}: depends_on 引用了不存在的 step_id={d}")

    return errors


def validate_task_args(task: str, args: Dict[str, Any]) -> List[str]:
    """按任务类型校验参数字段和值类型。"""
    errors: List[str] = []
    if task == "QUERY":
        q = args.get("query_type")
        if q is not None:
            q_norm = str(q).upper().strip()
            args["query_type"] = q_norm
            if q_norm not in VALID_QUERY_TYPES:
                errors.append(f"query_type 非法: {q_norm}")
        for k in ("hospital", "department"):
            if k in args and args[k] is not None and not isinstance(args[k], str):
                errors.append(f"{k} 必须为字符串或 null")

    if task == "REGISTRATION":
        for k in ("hospital", "department", "preferred_time", "doctor"):
            if k in args and args[k] is not None and not isinstance(args[k], str):
                errors.append(f"{k} 必须为字符串或 null")

    if task == "INTERPRET":
        if "report" in args and args["report"] is not None and not isinstance(args["report"], dict):
            errors.append("report 必须为对象或 null")
        if "report_id" in args and args["report_id"] is not None and not isinstance(args["report_id"], str):
            errors.append("report_id 必须为字符串或 null")

    # ─── 新任务校验 ───────────────────────────────────────────────
    if task == "EMR_INTAKE":
        # chief_complaint 必需
        cc = args.get("chief_complaint")
        if cc is None or str(cc).strip() == "":
            errors.append("chief_complaint 为必填（描述主诉症状）")
        # 可选检查其他字段类型
        if "symptoms" in args and args["symptoms"] is not None:
            if not isinstance(args["symptoms"], list):
                errors.append("symptoms 必须为列表或 null")
        if "medical_history" in args and args["medical_history"] is not None:
            if not isinstance(args["medical_history"], list):
                errors.append("medical_history 必须为列表或 null")
        if "vital_signs" in args and args["vital_signs"] is not None:
            if not isinstance(args["vital_signs"], dict):
                errors.append("vital_signs 必须为对象或 null")
    
    elif task == "CHRONIC_DISEASE_MGMT":
        action = args.get("action")
        if action is None:
            errors.append("action 为必填")
        else:
            action_norm = str(action).upper().strip()
            args["action"] = action_norm
            if action_norm not in VALID_CHRONIC_ACTIONS:
                errors.append(f"action 必须是 {VALID_CHRONIC_ACTIONS} 之一，得到: {action_norm}")
        
        disease_name = args.get("disease_name")
        if disease_name is None or str(disease_name).strip() == "":
            errors.append("disease_name 为必填（如：高血压、糖尿病）")

    return errors

def detect_missing_slots(plan: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    questions = []
    for step in plan:
        task = step.get("task")
        for slot in _missing_required_slots(step):
            # 原有任务
            if task == "REGISTRATION" and slot == "hospital":
                q = "你想挂哪个医院/院区？"
            elif task == "REGISTRATION" and slot == "department":
                q = "你要挂哪个科室？（例如：心内科/皮肤科）"
            elif task == "REGISTRATION" and slot == "preferred_time":
                q = "你希望就诊时间是什么时候？（例如：明天上午/2026-04-02 10:00）"
            elif task == "QUERY" and slot == "query_type":
                q = "你要查询什么？（可挂号医生/化验报告/影像报告/挂号记录/就诊记录）"
            elif task == "INTERPRET" and slot == "report":
                q = "请提供需要解读的报告内容（或报告编号/类型+日期）。"
            # 新任务
            elif task == "EMR_INTAKE" and slot == "chief_complaint":
                q = "请描述你的主要症状和不适（如「胸闷2天、伴呼吸困难」）"
            elif task == "CHRONIC_DISEASE_MGMT" and slot == "disease_name":
                q = "你要管理哪个慢性病？（如：高血压、糖尿病、冠心病）"
            elif task == "CHRONIC_DISEASE_MGMT" and slot == "action":
                q = "你要做什么？（选项：创建档案/生成提醒/检查预警）"
            else:
                q = f"请补充 {slot}。"
            questions.append({"slot": f"{task}.{slot}", "question": q})
    return questions

# ============ 执行计划：按依赖顺序调用工具 ============
def run_plan_sync(plan: List[Dict[str, Any]], tool_client, patient_id: str) -> Dict[str, Any]:
    """按依赖顺序执行计划并返回结构化执行轨迹。

    返回值包含：
    - status: ok/partial/failed
    - steps: 每一步的 args/status/response/error
    - by_task: 按任务名聚合后的步骤信息

    这样做的好处：
    - 前端和日志都能复盘“哪一步失败、为什么失败”。
    - Summary 可以只引用真正成功的工具步骤，避免无依据结论。
    """
    plan_errors = validate_plan_structure(plan)
    if plan_errors:
        return {
            "status": "failed",
            "plan_errors": plan_errors,
            "steps": [],
            "by_task": {},
        }

    step_states: Dict[str, Dict[str, Any]] = {
        step["step_id"]: {
            "step_id": step["step_id"],
            "task": step.get("task"),
            "depends_on": step.get("depends_on", []) or [],
            "args": step.get("args", {}) or {},
            "status": "pending",
            "response": None,
            "error": None,
        }
        for step in plan
    }

    completed: set[str] = set()
    while len(completed) < len(plan):
        progressed = False
        for step in plan:
            sid = step["step_id"]
            state = step_states[sid]
            if state["status"] != "pending":
                continue

            deps = state["depends_on"]
            if any(d not in completed for d in deps):
                continue

            # 上游失败则跳过当前步骤，避免带病执行
            if any(step_states[d]["status"] in ("failed", "skipped") for d in deps):
                state["status"] = "skipped"
                state["error"] = "upstream_failed"
                completed.add(sid)
                progressed = True
                continue

            task = state["task"]
            args = state["args"]
            missing = _missing_required_slots(step)
            if missing:
                state["status"] = "failed"
                state["error"] = f"missing_slots: {missing}"
                completed.add(sid)
                progressed = True
                continue

            arg_errors = validate_task_args(task, args)
            if arg_errors:
                state["status"] = "failed"
                state["error"] = "; ".join(arg_errors)
                completed.add(sid)
                progressed = True
                continue

            payload = {"patient_id": patient_id, **args}
            try:
                if task == "REGISTRATION":
                    resp = tool_client.register(payload)
                elif task == "QUERY":
                    resp = tool_client.query(payload)
                elif task == "INTERPRET":
                    resp = tool_client.interpret(payload)
                # 新增任务处理
                elif task == "EMR_INTAKE":
                    resp = tool_client.intake_emr(payload)
                elif task == "CHRONIC_DISEASE_MGMT":
                    # 根据 action 调用不同接口
                    action = args.get("action", "").upper()
                    if action == "CREATE":
                        resp = tool_client.record_chronic_disease(payload)
                    elif action == "GET_REMINDERS":
                        resp = tool_client.generate_chronic_reminders(payload)
                    elif action == "CHECK_WARNING":
                        resp = tool_client.check_urgent_warning(payload) or {"status": "no_warning"}
                    else:
                        raise RuntimeError(f"Unknown CHRONIC_DISEASE_MGMT action: {action}")
                else:
                    raise RuntimeError(f"Unknown task: {task}")

                if not isinstance(resp, dict):
                    raise RuntimeError("Tool response must be JSON object")
                state["status"] = "ok"
                state["response"] = resp
            except Exception as ex:
                state["status"] = "failed"
                state["error"] = str(ex)

            completed.add(sid)
            progressed = True

        if not progressed:
            # 循环依赖或非法依赖导致无法推进
            for sid, state in step_states.items():
                if state["status"] == "pending":
                    state["status"] = "skipped"
                    state["error"] = "blocked_by_dependency_cycle"
                    completed.add(sid)
            break

    success_count = sum(1 for s in step_states.values() if s["status"] == "ok")
    if success_count == len(step_states):
        status = "ok"
    elif success_count > 0:
        status = "partial"
    else:
        status = "failed"

    by_task: Dict[str, Any] = {}
    for state in step_states.values():
        t = state["task"]
        by_task.setdefault(t, []).append(state)

    return {
        "status": status,
        "steps": list(step_states.values()),
        "by_task": by_task,
        "plan_errors": [],
    }

# ============ Agent 主入口 ============
@dataclass
class AgentResult:
    type: str  # "clarification" | "final"
    session_state: Dict[str, Any]
    response_json: Dict[str, Any]


def _extract_slot_values(
    message: str,
    missing_slots: List[Dict],
    call_llm,
) -> Tuple[Dict[str, Any], Dict]:
    """
    定向提取：只从 message 中抽取 missing_slots 列出的槽位值。
    比全量 Planner 更可靠，专门用于追问-回答轮。
    返回 ({slot_key: value}, usage)
    """
    slots_desc = "\n".join(f"- {s['slot']} : {s.get('question', s['slot'])}" for s in missing_slots)
    user_content = SLOT_EXTRACTOR_USER_TMPL.format(
        message=message,
        slots_desc=slots_desc,
    )
    messages = [
        {"role": "system", "content": SLOT_EXTRACTOR_SYSTEM},
        {"role": "user",   "content": user_content},
    ]
    content, usage = call_llm(messages)
    return parse_json_strict(content) or {}, usage


def _apply_extracted(plan: List[Dict], extracted: Dict[str, Any]) -> None:
    """将提取结果回填进 plan args（in-place）。"""
    for step in plan:
        task = step.get("task", "")
        args = step.setdefault("args", {})
        for key, val in extracted.items():
            if val is None or str(val).strip().lower() in ("", "null", "none"):
                continue
            if "." in key:
                t, k = key.split(".", 1)
                if t == task:
                    args[k] = val
            elif key == "patient_id":
                pass  # patient_id 单独处理


def medical_agent_step(
    *,
    message: str,
    known: Dict[str, Any],
    call_llm,
    tool_client,
) -> Tuple[AgentResult, Dict[str, Any]]:
    """
    返回 (AgentResult, usage)
    - clarification：需要追问
    - final：执行完工具并汇总

        两种模式（降低多轮场景下的计划漂移）：
      A) 槽位填充模式（已有 plan + 上轮缺槽）：定向提取，不重新规划
      B) 全量规划模式（新会话 or 意图切换）
    """
    last_plan: List[Dict[str, Any]] = known.get("last_plan") or []
    last_missing: List[Dict] = known.get("last_missing_slots") or []
    patient_id = known.get("patient_id") or "P001"
    usage: Dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    # ── 模式 0：待确认预约 ────────────────────────────────────────────────────
    # EMR 完成后提示用户"是否预约"，用户确认后才创建 REGISTRATION。
    # 用户已在医院/小程序内，因此 hospital 不再追问，只需确认就诊时间。
    pending_booking = known.get("pending_booking")
    if pending_booking and isinstance(pending_booking, dict) and _is_booking_confirmation(message):
        dept = pending_booking.get("department") or "门诊"
        preferred_time = _extract_preferred_time(message)
        reg_args: Dict[str, Any] = {
            "hospital": pending_booking.get("hospital") or "当前医院",
            "department": dept,
            "preferred_time": preferred_time,
            "doctor": None,
        }
        reg_step: Dict[str, Any] = {
            "task": "REGISTRATION", "step_id": "S1",
            "args": reg_args, "depends_on": [],
        }
        session_state = dict(known)
        session_state.pop("pending_booking", None)
        session_state["last_plan"] = [reg_step]
        if not preferred_time:
            time_q = [{"slot": "REGISTRATION.preferred_time",
                       "question": "你希望什么时间就诊？（例如：今天下午 / 明天上午 / 2026-04-07 10:00）"}]
            session_state["last_missing_slots"] = time_q
            return AgentResult(
                type="clarification",
                session_state=session_state,
                response_json={
                    "answer": "好的，正在为您安排预约！请问您希望什么时间就诊？",
                    "sources": [], "confidence": 0.0,
                    "missing_slots": time_q,
                },
            ), usage
        session_state.pop("last_missing_slots", None)
        return _execute_and_summarize(message, [reg_step], patient_id, session_state,
                                      call_llm, tool_client, usage)

    # ── 模式 A：槽位填充 ──────────────────────────────────────────────────────
    if last_plan and last_missing:
        extracted, u = _extract_slot_values(message, last_missing, call_llm)
        _merge_usage(usage, u)

        # 更新 patient_id
        if extracted.get("patient_id"):
            patient_id = extracted["patient_id"]

        # 深拷贝 plan 并回填
        plan = copy.deepcopy(last_plan)
        _apply_extracted(plan, extracted)

        # 还有缺参则继续追问
        still_missing = detect_missing_slots(plan)
        session_state = dict(known)
        session_state.update({"patient_id": patient_id, "last_plan": plan})

        if still_missing:
            session_state["last_missing_slots"] = still_missing
            return AgentResult(
                type="clarification",
                session_state=session_state,
                response_json={
                    "answer": "为了帮你继续，我需要确认以下信息：\n" +
                              "\n".join(f"- {q['question']}" for q in still_missing[:3]),
                    "sources": [],
                    "confidence": 0.0,
                    "missing_slots": still_missing[:3],
                },
            ), usage

        # 槽位已全部填满 → 执行
        session_state.pop("last_missing_slots", None)
        return _execute_and_summarize(message, plan, patient_id, session_state, call_llm, tool_client, usage)

    # ── 模式 B：全量规划 ──────────────────────────────────────────────────────
    # 从 last_plan 提取已知槽（非 null）
    prior_slots: Dict[str, Any] = {}
    for step in last_plan:
        for k, v in (step.get("args") or {}).items():
            if v not in (None, "", "null"):
                prior_slots[f"{step.get('task', '')}.{k}"] = v
    if known.get("patient_id"):
        prior_slots["patient_id"] = known["patient_id"]

    known_extra = {k: v for k, v in known.items()
                   if k not in ("last_plan", "last_missing_slots", "tools_result", "patient_id")}

    heuristic_plan_obj = _build_heuristic_plan(message, patient_id)
    if heuristic_plan_obj:
        plan_obj = normalize_plan(heuristic_plan_obj)
        plan: List[Dict] = plan_obj.get("plan", [])

        for step in plan:
            task = step.get("task", "")
            args = step.setdefault("args", {})
            for slot_key, slot_val in prior_slots.items():
                if "." in slot_key:
                    t, k = slot_key.split(".", 1)
                    if t == task and args.get(k) in (None, "", "null"):
                        args[k] = slot_val

        merged_missing = detect_missing_slots(plan)
        session_state = dict(known)
        session_state.update({"patient_id": patient_id, "last_plan": plan})

        if merged_missing:
            session_state["last_missing_slots"] = merged_missing[:3]
            return AgentResult(
                type="clarification",
                session_state=session_state,
                response_json={
                    "answer": "为了帮你继续，我需要确认以下信息：\n" +
                              "\n".join(f"- {q['question']}" for q in merged_missing[:3]),
                    "sources": [], "confidence": 0.0,
                    "missing_slots": merged_missing[:3],
                },
            ), usage

        session_state.pop("last_missing_slots", None)
        return _execute_and_summarize(message, plan, patient_id, session_state, call_llm, tool_client, usage)

    planner_user = MEDICAL_PLANNER_USER_TMPL.format(
        message=message,
        prior_slots=json.dumps(prior_slots, ensure_ascii=False) if prior_slots else "（无）",
        known_extra=json.dumps(known_extra, ensure_ascii=False) if known_extra else "（无）",
    )
    content, u = call_llm([
        {"role": "system", "content": MEDICAL_PLANNER_SYSTEM},
        {"role": "user",   "content": planner_user},
    ])
    _merge_usage(usage, u)

    plan_obj = parse_json_strict(content)
    if not plan_obj:
        return AgentResult(
            type="clarification",
            session_state=known,
            response_json={
                "answer": "我没能正确解析你的需求。你是要：挂号 / 查询报告 / 解读报告 / 初诊病例整理 / 慢病提醒（可多选）？",
                "sources": [], "confidence": 0.0,
            },
        ), usage

    plan_obj = normalize_plan(plan_obj)
    patient_id = plan_obj.get("patient_id") or patient_id
    plan: List[Dict] = plan_obj.get("plan", [])

    # prior_slots 兜底回填
    for step in plan:
        task = step.get("task", "")
        args = step.setdefault("args", {})
        for slot_key, slot_val in prior_slots.items():
            if "." in slot_key:
                t, k = slot_key.split(".", 1)
                if t == task and args.get(k) in (None, "", "null"):
                    args[k] = slot_val

    missing = plan_obj.get("missing_slots") or []
    auto_missing = detect_missing_slots(plan)
    merged_missing = missing + [m for m in auto_missing if m not in missing]

    session_state = dict(known)
    session_state.update({"patient_id": patient_id, "last_plan": plan})

    if merged_missing:
        session_state["last_missing_slots"] = merged_missing[:3]
        return AgentResult(
            type="clarification",
            session_state=session_state,
            response_json={
                "answer": "为了帮你继续，我需要确认以下信息：\n" +
                          "\n".join(f"- {q['question']}" for q in merged_missing[:3]),
                "sources": [], "confidence": 0.0,
                "missing_slots": merged_missing[:3],
            },
        ), usage

    session_state.pop("last_missing_slots", None)
    return _execute_and_summarize(message, plan, patient_id, session_state, call_llm, tool_client, usage)


# ── 执行工具 + 汇总（抽出公共逻辑避免重复） ─────────────────────────────────
def _merge_usage(base: Dict, extra: Dict) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        base[k] = base.get(k, 0) + extra.get(k, 0)


def _execute_and_summarize(
    message: str,
    plan: List[Dict],
    patient_id: str,
    session_state: Dict,
    call_llm,
    tool_client,
    usage: Dict,
) -> Tuple[AgentResult, Dict]:
    """执行工具并生成最终回复。

    注意：Summary 的 sources 会经过白名单校验。
    即使 LLM 输出了来源，只要不在 allowed_sources 内就会被拒绝并回退。
    """
    tools_result = run_plan_sync(plan, tool_client, patient_id)
    _augment_urgent_workflow(tools_result, plan, tool_client, patient_id)

    allowed_sources = [
        f"tool:{s.get('step_id')}:{s.get('task')}"
        for s in tools_result.get("steps", [])
        if s.get("status") == "ok"
    ]

    content2, u2 = call_llm([
        {"role": "system", "content": MEDICAL_SUMMARY_SYSTEM},
        {"role": "user",   "content": MEDICAL_SUMMARY_USER_TMPL.format(
            message=message,
            tools_result=json.dumps(
                {
                    "execution": tools_result,
                    "allowed_sources": allowed_sources,
                    "summary_rules": {
                        "must_reference_allowed_sources": True,
                        "must_not_diagnose": True,
                        "if_no_successful_tool": "answer=我不知道, confidence<0.4",
                    },
                },
                ensure_ascii=False,
            ),
        )},
    ])
    _merge_usage(usage, u2)

    summary_obj = _validate_summary_obj(parse_json_strict(content2), allowed_sources)
    if not summary_obj:
        summary_obj = _build_fallback_summary(tools_result, allowed_sources)

    _append_urgent_action_guidance(summary_obj, tools_result)

    summary_obj["execution"] = {
        "status": tools_result.get("status"),
        "step_count": len(tools_result.get("steps", [])),
        "ok_steps": len([s for s in tools_result.get("steps", []) if s.get("status") == "ok"]),
    }

    sev_obj = _extract_display_severity(tools_result)
    if sev_obj:
        summary_obj["severity"] = sev_obj

    _maybe_offer_booking(summary_obj, plan, tools_result, session_state)

    session_state["tools_result"] = tools_result
    return AgentResult(type="final", session_state=session_state, response_json=summary_obj), usage


def _maybe_offer_booking(
    summary_obj: Dict[str, Any],
    plan: List[Dict[str, Any]],
    tools_result: Dict[str, Any],
    session_state: Dict[str, Any],
) -> None:
    """EMR 评估完成后，若本轮没有执行 REGISTRATION，追加预约确认提示并保存 pending_booking。
    下一轮用户确认后，模式 0 会接管执行挂号，不再追问医院名称。
    """
    reg_done = any(
        s.get("task") == "REGISTRATION" and s.get("status") == "ok"
        for s in tools_result.get("steps", [])
    )
    if reg_done:
        session_state.pop("pending_booking", None)
        return

    emr_done = any(
        s.get("task") == "EMR_INTAKE" and s.get("status") == "ok"
        for s in tools_result.get("steps", [])
    )
    reg_in_plan = any(s.get("task") == "REGISTRATION" for s in plan)
    if not emr_done or reg_in_plan:
        return

    dept: Optional[str] = None
    for emr_step in tools_result.get("by_task", {}).get("EMR_INTAKE", []):
        if emr_step.get("status") == "ok":
            dept = (emr_step.get("response") or {}).get("recommended_dept")
            break
    dept = dept or "相关科室"

    session_state["pending_booking"] = {
        "department": dept,
        "hospital": "当前医院",
    }
    booking_prompt = (
        "\n\n---\n"
        f"如需预约 **{dept}** 就诊，请回复【是】或【帮我预约】，"
        "我将立即为您安排，只需再告知希望的就诊时间即可。"
    )
    summary_obj["answer"] = (summary_obj.get("answer") or "").rstrip() + booking_prompt


def _extract_display_severity(tools_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # 优先使用 EMR 的标准严重度
    for emr_step in tools_result.get("by_task", {}).get("EMR_INTAKE", []):
        if emr_step.get("status") != "ok":
            continue
        sev = (emr_step.get("response") or {}).get("severity")
        if isinstance(sev, dict) and sev.get("level"):
            return sev

    # 慢病预警返回 URGENT 时，映射为 RED 供前端统一高亮展示。
    for chronic_step in tools_result.get("by_task", {}).get("CHRONIC_DISEASE_MGMT", []):
        if chronic_step.get("status") != "ok":
            continue
        resp = chronic_step.get("response") or {}
        if str(resp.get("level", "")).upper() != "URGENT":
            continue
        msg = str(resp.get("message") or "触发慢病紧急预警")
        actions = resp.get("actions") if isinstance(resp.get("actions"), list) else []
        reason_lines = [msg]
        reason_lines.extend([str(item) for item in actions[:2]])
        return {
            "level": "RED",
            "score": 90,
            "reasons": reason_lines,
        }

    return None


def _is_red_emergency(tools_result: Dict[str, Any]) -> bool:
    for emr_step in tools_result.get("by_task", {}).get("EMR_INTAKE", []):
        if emr_step.get("status") != "ok":
            continue
        sev = (emr_step.get("response") or {}).get("severity") or {}
        if str(sev.get("level", "")).upper() == "RED":
            return True
    for chronic_step in tools_result.get("by_task", {}).get("CHRONIC_DISEASE_MGMT", []):
        if chronic_step.get("status") != "ok":
            continue
        level = str((chronic_step.get("response") or {}).get("level", "")).upper()
        if level == "URGENT":
            return True
    return False


def _pick_registration_context(plan: List[Dict[str, Any]], tools_result: Dict[str, Any]) -> Dict[str, str]:
    hospital = None
    preferred_time = None
    department = None

    for step in plan:
        if step.get("task") != "REGISTRATION":
            continue
        args = step.get("args") or {}
        hospital = hospital or args.get("hospital")
        preferred_time = preferred_time or args.get("preferred_time")
        department = department or args.get("department")

    if not department:
        for emr_step in tools_result.get("by_task", {}).get("EMR_INTAKE", []):
            if emr_step.get("status") != "ok":
                continue
            department = (emr_step.get("response") or {}).get("recommended_dept")
            if department:
                break

    return {
        "hospital": hospital or "就近综合医院",
        "preferred_time": preferred_time or "立即就诊",
        "department": department or "急诊科",
    }


def _next_auto_step_id(existing_steps: List[Dict[str, Any]]) -> str:
    existing_ids = {str(s.get("step_id")) for s in existing_steps}
    idx = 1
    while f"AUTO{idx}" in existing_ids:
        idx += 1
    return f"AUTO{idx}"


def _append_step_result(
    tools_result: Dict[str, Any],
    *,
    task: str,
    depends_on: List[str],
    args: Dict[str, Any],
    status: str,
    response: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    step = {
        "step_id": _next_auto_step_id(tools_result.get("steps", [])),
        "task": task,
        "depends_on": depends_on,
        "args": args,
        "status": status,
        "response": response,
        "error": error,
        "auto_generated": True,
    }
    tools_result.setdefault("steps", []).append(step)
    tools_result.setdefault("by_task", {}).setdefault(task, []).append(step)
    return step


def _recompute_tools_result_status(tools_result: Dict[str, Any]) -> None:
    steps = tools_result.get("steps", [])
    if not steps:
        tools_result["status"] = "failed"
        return
    success_count = sum(1 for s in steps if s.get("status") == "ok")
    if success_count == len(steps):
        tools_result["status"] = "ok"
    elif success_count > 0:
        tools_result["status"] = "partial"
    else:
        tools_result["status"] = "failed"


def _augment_urgent_workflow(
    tools_result: Dict[str, Any],
    plan: List[Dict[str, Any]],
    tool_client,
    patient_id: str,
) -> None:
    """紧急场景下自动补充下一步动作：联系医生与挂号。"""
    if not _is_red_emergency(tools_result):
        return

    urgent_actions: List[str] = []
    context = _pick_registration_context(plan, tools_result)
    emergency_department = "急诊科"
    specialist_department = context["department"]

    existing_ok_registration = any(
        s.get("task") == "REGISTRATION" and s.get("status") == "ok"
        for s in tools_result.get("steps", [])
    )
    existing_ok_doctor_query = any(
        s.get("task") == "QUERY"
        and s.get("status") == "ok"
        and str((s.get("args") or {}).get("query_type", "")).upper() == "DOCTOR_LIST"
        for s in tools_result.get("steps", [])
    )

    if not existing_ok_doctor_query:
        query_args = {
            "query_type": "DOCTOR_LIST",
            "hospital": context["hospital"],
            "department": specialist_department,
        }
        query_payload = {"patient_id": patient_id, **query_args}
        try:
            query_resp = tool_client.query(query_payload)
            _append_step_result(
                tools_result,
                task="QUERY",
                depends_on=[],
                args=query_args,
                status="ok",
                response=query_resp,
            )
            doctors = query_resp.get("doctors") if isinstance(query_resp, dict) else None
            if isinstance(doctors, list) and doctors:
                names = "、".join(str(doc.get("name", "")) for doc in doctors[:2] if doc.get("name"))
                urgent_actions.append(f"已查询{specialist_department}可联系医生：{names}。")
            else:
                urgent_actions.append(f"已发起{specialist_department}医生排班查询。")
        except Exception as ex:
            _append_step_result(
                tools_result,
                task="QUERY",
                depends_on=[],
                args=query_args,
                status="failed",
                error=str(ex),
            )
            urgent_actions.append("医生排班查询未成功，请直接拨打医院总机或急诊电话联系当班医生。")

    if not existing_ok_registration:
        reg_args = {
            "hospital": context["hospital"],
            "department": emergency_department,
            "preferred_time": context["preferred_time"],
            "doctor": None,
        }
        reg_payload = {"patient_id": patient_id, **reg_args}
        try:
            reg_resp = tool_client.register(reg_payload)
            _append_step_result(
                tools_result,
                task="REGISTRATION",
                depends_on=[],
                args=reg_args,
                status="ok",
                response=reg_resp,
            )
            rid = reg_resp.get("registration_id") if isinstance(reg_resp, dict) else None
            when = reg_resp.get("scheduled_time") if isinstance(reg_resp, dict) else None
            where = reg_resp.get("location") if isinstance(reg_resp, dict) else None
            urgent_actions.append(
                "已发起急诊挂号"
                + (f"（单号：{rid}）" if rid else "")
                + (f"，时间：{when}" if when else "")
                + (f"，地点：{where}" if where else "")
                + "。"
            )
        except Exception as ex:
            _append_step_result(
                tools_result,
                task="REGISTRATION",
                depends_on=[],
                args=reg_args,
                status="failed",
                error=str(ex),
            )
            urgent_actions.append("自动挂号未成功，请立即前往最近急诊窗口或拨打120。")

    if urgent_actions:
        tools_result["urgent_actions"] = urgent_actions
    _recompute_tools_result_status(tools_result)


def _append_urgent_action_guidance(summary_obj: Dict[str, Any], tools_result: Dict[str, Any]) -> None:
    actions = tools_result.get("urgent_actions") or []
    if not actions:
        return
    lines = [
        "",
        "【已执行的下一步处置】",
        *[f"- {item}" for item in actions],
        "【现在建议你立即执行】",
        "- 立刻停止活动并保持坐位休息，避免独自外出。",
        "- 如果胸闷/呼吸困难持续或加重，或出现胸痛、出汗、意识模糊，请立即拨打120。",
        "- 携带既往病历与用药清单尽快到急诊就医。",
    ]
    summary_obj["answer"] = f"{summary_obj.get('answer', '').rstrip()}\n" + "\n".join(lines)


def _validate_summary_obj(obj: Optional[Dict[str, Any]], allowed_sources: List[str]) -> Optional[Dict[str, Any]]:
    """校验 Summary 输出，确保只引用真实工具证据。"""
    if not isinstance(obj, dict):
        return None
    if not isinstance(obj.get("answer"), str):
        return None
    if not isinstance(obj.get("sources"), list):
        return None
    if not isinstance(obj.get("confidence"), (int, float)):
        return None

    normalized_sources: List[str] = [str(x) for x in obj.get("sources", [])]
    allowed = set(allowed_sources)
    if any(src not in allowed for src in normalized_sources):
        return None

    answer = obj["answer"].strip()
    if "仅供参考" not in answer:
        answer = f"{answer}\n\n仅供参考，不替代医生诊断。"

    confidence = max(0.0, min(1.0, float(obj["confidence"])))
    return {
        "answer": answer,
        "sources": normalized_sources,
        "confidence": confidence,
    }


def _build_fallback_summary(tools_result: Dict[str, Any], allowed_sources: List[str]) -> Dict[str, Any]:
    """当 LLM 汇总不可信或格式错误时，使用确定性兜底总结。"""
    ok_steps = [s for s in tools_result.get("steps", []) if s.get("status") == "ok"]
    if not ok_steps:
        return {
            "answer": "我不知道。未获得可用的工具结果。仅供参考，不替代医生诊断。",
            "sources": [],
            "confidence": 0.0,
        }

    lines = ["已完成工具执行，关键结果如下："]
    for s in ok_steps[:3]:
        resp = s.get("response") or {}
        snippet = json.dumps(resp, ensure_ascii=False)
        if len(snippet) > 180:
            snippet = snippet[:180] + "..."
        lines.append(f"- {s.get('task')}({s.get('step_id')}): {snippet}")
    lines.append("仅供参考，不替代医生诊断。")
    return {
        "answer": "\n".join(lines),
        "sources": allowed_sources[:3],
        "confidence": 0.6 if ok_steps else 0.0,
    }