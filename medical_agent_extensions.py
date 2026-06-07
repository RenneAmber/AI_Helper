"""
医疗 Agent 功能扩展 - 新任务类型（EMR_INTAKE, CHRONIC_DISEASE_MGMT）
这个模块为medical_agent.py添加对新功能的支持
"""

from typing import Dict, List, Any, Optional, Tuple
import json

# ============ 扩展的任务配置 ============

EXTENDED_VALID_TASKS = {"REGISTRATION", "QUERY", "INTERPRET", "EMR_INTAKE", "CHRONIC_DISEASE_MGMT"}

EXTENDED_REQUIRED_SLOTS = {
    "REGISTRATION": ["hospital", "department", "preferred_time"],
    "QUERY": ["query_type"],
    "INTERPRET": ["report"],
    # 新增
    "EMR_INTAKE": ["chief_complaint"],  # symptoms, medical_history, vital_signs optional
    "CHRONIC_DISEASE_MGMT": ["action", "disease_name"],  # action: CREATE|GET_REMINDERS|CHECK_WARNING
}

# ============ 扩展的 Planner System ============

EXTENDED_PLANNER_SYSTEM = (
    "你是一个医疗任务编排助手（仅做流程编排，不提供诊断）。\n"
    "你只能产生一个 JSON 对象，禁止输出任何额外文字。\n\n"
    "【支持的任务和字段】\n"
    "1. REGISTRATION: {\"hospital\":string, \"department\":string, \"preferred_time\":string, \"doctor\":string|null}\n"
    "2. QUERY: {\"query_type\":string(枚举), \"hospital\":string|null, \"department\":string|null}\n"
    "   query_type 值: DOCTOR_LIST|LAB_REPORT|IMAGING|REG_RECORD|VISIT_RECORD\n"
    "3. INTERPRET: {\"report\":object|null, \"report_id\":string|null}\n"
    "4. 【新增】EMR_INTAKE: {\"chief_complaint\":string, \"symptoms\":[string]|null, \"medical_history\":[string]|null, \"vital_signs\":object|null}\n"
    "   vital_signs 可包含: {\"bp\": \"120/80\", \"hr\": 75, \"temp\": 37.0, \"rr\": 15}\n"
    "5. 【新增】CHRONIC_DISEASE_MGMT: {\"action\":string, \"disease_name\":string, \"diagnosis_date\":string|null, \"last_checkup_date\":string|null}\n"
    "   action 值: CREATE|GET_REMINDERS|CHECK_WARNING\n"
    "   disease_name 值: 高血压|糖尿病|冠心病(或其他医学术语)\n\n"
    "【任务识别规则】\n"
    "场景1: 用户说\"我有胸闷、呼吸困难，要挂心内科\" → 可组合成:\n"
    "  Plan: [EMR_INTAKE(先生成结构化病历), REGISTRATION(再预约)]\n"
    "  说明: EMR_INTAKE 的结果中会有 severity_level 和 recommended_dept，可作为后续 REGISTRATION 的参考\n"
    "场景2: 用户说\"我有高血压，需要建立随访提醒\" → \n"
    "  Plan: [CHRONIC_DISEASE_MGMT(action=CREATE)]\n"
    "场景3: 用户说\"提醒我高血压复查\" →\n"
    "  Plan: [CHRONIC_DISEASE_MGMT(action=GET_REMINDERS, disease_name=高血压)]\n"
    "场景4: 用户说\"我血压最近160/95，有没有风险\"→\n"
    "  Plan: [CHRONIC_DISEASE_MGMT(action=CHECK_WARNING, disease_name=高血压)]\n\n"
    "【任务依赖关系】\n"
    "- EMR_INTAKE 可作为 REGISTRATION 的 depends_on（先采集病例，再挂号）\n"
    "- CHRONIC_DISEASE_MGMT(CREATE) 后可 depends_on REGISTRATION（先挂号->就诊->建档）\n\n"
    "【提取和合并规则】\n"
    "1. 从已有的 prior_slots(前轮填过的值) 中保留并继承\n"
    "2. 用本轮消息中的新值覆盖（更新）\n"
    "3. 对于列表类字段(symptoms, medical_history)，优先使用本轮提取的列表\n"
    "4. 只在 missing_slots 中列出最终仍为空的必需字段\n\n"
    "【多任务规划示例】\n"
    "用户: \"我最近胸闷呼吸困难，想挂号\"\n"
    "推荐计划:\n"
    "{\n"
    "  \"intent\": \"MULTI_TASK\",\n"
    "  \"plan\": [\n"
    "    {\"task\": \"EMR_INTAKE\", \"args\": {\"chief_complaint\": \"胸闷、呼吸困难\", \"symptoms\": [\"胸闷\", \"呼吸困难\"]}, \"depends_on\": []},\n"
    "    {\"task\": \"REGISTRATION\", \"args\": {\"department\": \"心内科\"}, \"depends_on\": [\"S1\"]}\n"
    "  ]\n"
    "}\n"
    "说明: S1 是 EMR_INTAKE 的自动 step_id，REGISTRATION 依赖它是为了在生成的结构化病历基础上继续预约\n\n"
    "【输出格式】\n"
    "{\"intent\":\"string\",\"patient_id\":\"string|null\",\"plan\":[{\"task\":\"string\",\"args\":{},\"depends_on\":[]}],\"missing_slots\":[{\"slot\":\"string\",\"question\":\"string\"}]}"
)

def extend_required_slots() -> Dict[str, List[str]]:
    """返回扩展后的 required_slots"""
    return EXTENDED_REQUIRED_SLOTS.copy()

def extend_valid_tasks() -> set:
    """返回扩展后的有效任务集合"""
    return EXTENDED_VALID_TASKS.copy()

def validate_extended_task_args(task: str, args: Dict[str, Any]) -> List[str]:
    """扩展版的任务参数校验"""
    errors: List[str] = []
    
    # 原有任务的校验保持不变
    from medical_agent import validate_task_args as original_validate
    errors.extend(original_validate(task, args))
    
    # 新任务的校验
    if task == "EMR_INTAKE":
        # chief_complaint 必需，其他可选
        chief_complaint = args.get("chief_complaint")
        if chief_complaint is None or str(chief_complaint).strip() == "":
            errors.append("chief_complaint 为必填")
        
        # 可选检查symptoms 和 medical_history 的类型
        if "symptoms" in args and args["symptoms"] is not None:
            if not isinstance(args["symptoms"], list):
                errors.append("symptoms 必须为列表或 null")
        
        if "medical_history" in args and args["medical_history"] is not None:
            if not isinstance(args["medical_history"], list):
                errors.append("medical_history 必须为列表或 null")
        
        # vital_signs 是可选对象
        if "vital_signs" in args and args["vital_signs"] is not None:
            if not isinstance(args["vital_signs"], dict):
                errors.append("vital_signs 必须为对象或 null")
    
    elif task == "CHRONIC_DISEASE_MGMT":
        # action 必需，且必须在指定的值中
        action = args.get("action")
        valid_actions = {"CREATE", "GET_REMINDERS", "CHECK_WARNING"}
        if action is None or str(action).upper() not in valid_actions:
            errors.append(f"action 必须是 {valid_actions} 之一")
        
        # disease_name 必需
        disease_name = args.get("disease_name")
        if disease_name is None or str(disease_name).strip() == "":
            errors.append("disease_name 为必填")
    
    return errors

# ============ 扩展的槽位检测 ============

def detect_extended_missing_slots(plan: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """扩展版的缺槽检测"""
    from medical_agent import _required_slots_for_step, _missing_required_slots
    
    questions = []
    for step in plan:
        task = step.get("task")
        for slot in _missing_required_slots(step):
            # 原有任务的问题生成
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
            # 新任务的问题生成
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

# ============ 工具调用扩展 ============

def call_extended_tool(task: str, args: Dict[str, Any], tool_client, patient_id: str) -> Dict[str, Any]:
    """
    扩展的工具调用函数
    在 run_plan_sync 中使用，支持新的任务类型
    """
    
    payload = {"patient_id": patient_id, **args}
    
    if task == "REGISTRATION":
        return tool_client.register(payload)
    elif task == "QUERY":
        return tool_client.query(payload)
    elif task == "INTERPRET":
        return tool_client.interpret(payload)
    # 新增任务
    elif task == "EMR_INTAKE":
        return tool_client.intake_emr(payload)
    elif task == "CHRONIC_DISEASE_MGMT":
        # 根据 action 调用不同的接口
        action = args.get("action", "").upper()
        if action == "CREATE":
            return tool_client.record_chronic_disease(payload)
        elif action == "GET_REMINDERS":
            return tool_client.generate_chronic_reminders(payload)
        elif action == "CHECK_WARNING":
            return tool_client.check_urgent_warning(payload)
        else:
            raise RuntimeError(f"Unknown CHRONIC_DISEASE_MGMT action: {action}")
    else:
        raise RuntimeError(f"Unknown task: {task}")

# ============ 预处理集成 ============

def integrate_extended_features(original_module) -> None:
    """
    将扩展功能集成到原 medical_agent 模块中
    在 app.py 导入 medical_agent 时调用
    """
    # 动态更新模块中的常量和函数
    original_module.REQUIRED_SLOTS.update(EXTENDED_REQUIRED_SLOTS)
    original_module.VALID_TASKS = EXTENDED_VALID_TASKS
    
    # 更新提示词可选（如果希望直接使用新的系统提示）
    # original_module.MEDICAL_PLANNER_SYSTEM = EXTENDED_PLANNER要系统
    
    # 记录备份原始的校验函数
    if not hasattr(original_module, '_original_validate_task_args'):
        original_module._original_validate_task_args = original_module.validate_task_args
    
    # 替换为扩展版本
    original_module.validate_task_args = validate_extended_task_args
    original_module.detect_missing_slots = detect_extended_missing_slots
