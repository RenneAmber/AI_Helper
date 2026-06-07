import uuid
import json
from datetime import datetime
from typing import Dict, Any, List

from app.decision.state import GraphState
from app.decision.repo import DecisionRepo
from app.decision.hashing import stable_hash
from app.decision.tools import fake_retriever, fake_log_query


def _extract_recommendation_from_tags(tags: List[str]) -> str:
    for tag in tags:
        if tag.startswith("rec:"):
            return tag.split("rec:", 1)[1]
    return "NEEDS_REVIEW"


def _extract_confidence_from_tags(tags: List[str], fallback: float = 0.55) -> float:
    for tag in tags:
        if tag.startswith("cf:"):
            try:
                value = float(tag.split("cf:", 1)[1])
                return max(0.0, min(value, 1.0))
            except ValueError:
                return fallback
    return fallback


async def write_event(
    repo: DecisionRepo,
    decision_id: str,
    event_type: str,
    node_name: str,
    status: str,
    payload: Dict[str, Any]
):
    """辅助函数：写入事件日志"""
    await repo.add_event(
        event_id=str(uuid.uuid4()),
        decision_id=decision_id,
        event_type=event_type,
        node_name=node_name,
        status=status,
        payload_json=json.dumps(payload, ensure_ascii=False)
    )


async def normalize_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    第一步：标准化输入
    补齐缺失字段、验证类型
    """
    node = "Normalize"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success",
        {"request": state.get("request")}
    )
    
    req = state["request"]
    normalized = {
        "title": req.get("title") or req.get("question", "")[:60],
        "question": req["question"],
        "domain": req.get("domain", "engineering"),
        "intentMode": req.get("intentMode", "decision"),
        "context": req.get("context", {}),
        "constraints": req.get("constraints", []),
        "riskPosture": req.get("riskPosture", "low"),
        "timeHorizonDays": req.get("timeHorizonDays", 90),
    }
    state["normalized"] = normalized
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"normalized": normalized}
    )
    return state


async def plan_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    第二步：制定计划
    MVP：固定三步；后续可改成 LLM 生成
    """
    node = "Plan"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success",
        {"normalized": state.get("normalized")}
    )
    
    state["plan"] = [
        {"stepId": "S1", "type": "retrieve", "desc": "Collect evidence for the decision question."},
        {"stepId": "S2", "type": "analyze", "desc": "Evaluate options by criteria."},
        {"stepId": "S3", "type": "finalize", "desc": "Assemble decision record with confidence and follow-up."},
    ]
    
    # 初始化工具队列
    state["tool_queue"] = [
        {"tool_name": "fake_retriever", "input": {"query": state["normalized"]["question"]}}
    ]
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"plan": state["plan"], "tool_queue": state["tool_queue"]}
    )
    return state


async def permission_gate_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    权限关卡
    MVP：直接通过；生产中接企业权限系统（SharePoint/ADO）
    """
    node = "PermissionGate"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success",
        {"constraints": state["normalized"].get("constraints", [])}
    )
    
    gates = state.get("gates") or {}
    state["gates"] = gates
    gates["permission"] = {
        "result": "pass",
        "checkedAt": datetime.utcnow().isoformat()
    }
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"permission": gates["permission"]}
    )
    return state


async def tool_execute_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    工具执行
    关键：每个工具调用都写 tool_runs 表 + 事件日志
    """
    node = "ToolExecute"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success",
        {"tool_queue": state.get("tool_queue", [])}
    )
    
    results = []
    for item in (state.get("tool_queue") or []):
        run_id = str(uuid.uuid4())
        tool_name = item["tool_name"]
        tool_input = item["input"]
        input_hash = stable_hash(tool_input)
        started_at = datetime.utcnow()
        
        # 记录工具执行开始
        await repo.add_tool_run(
            run_id=run_id,
            decision_id=state["decision_id"],
            tool_name=tool_name,
            status="running",
            started_at=started_at,
            input_hash=input_hash
        )
        
        try:
            # 执行工具
            if tool_name == "fake_retriever":
                out = await fake_retriever(tool_input["query"])
            elif tool_name == "fake_log_query":
                out = await fake_log_query(tool_input["query"])
            else:
                raise RuntimeError(f"Unknown tool: {tool_name}")
            
            output_hash = stable_hash(out)
            ended_at = datetime.utcnow()
            
            # 标记成功
            await repo.finish_tool_run_success(run_id, ended_at, output_hash)
            results.append({
                "run_id": run_id,
                "tool_name": tool_name,
                "output": out,
                "status": "success"
            })
        except Exception as e:
            ended_at = datetime.utcnow()
            await repo.finish_tool_run_failure(
                run_id, ended_at, "tool_error", str(e)
            )
            results.append({
                "run_id": run_id,
                "tool_name": tool_name,
                "error": str(e),
                "status": "failure"
            })
    
    state["tool_results"] = results
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"tool_results": results}
    )
    return state


async def tool_verify_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    工具验证
    关键：将工具输出转换成 evidence_pack 结构
    """
    node = "ToolVerify"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success",
        {"tool_results": state.get("tool_results", [])}
    )
    
    ok = []
    errors = []
    
    for r in (state.get("tool_results") or []):
        if r["status"] == "success" and "output" in r:
            ok.append(r["output"])
        else:
            errors.append({
                "run_id": r["run_id"],
                "tool": r["tool_name"],
                "reason": r.get("error", "unknown")
            })
    
    state["errors"] = (state.get("errors") or []) + errors
    
    # 转成 evidence_pack 结构
    items = []
    for idx, ev in enumerate(ok, start=1):
        evidence_id = f"E{idx}"
        items.append({
            "evidenceId": evidence_id,
            "kind": ev["kind"],
            "source": {
                "sourceType": ev["sourceType"],
                "uri": ev["uri"],
                "title": ev.get("title"),
                "retrievedAt": ev["retrievedAt"]
            },
            "quote": ev["quote"],
            "signals": ev["signals"],
            "tags": ev.get("tags", []),
            "trainingCase": ev.get("trainingCase", {}),
            "hash": stable_hash(ev)
        })
    
    state["evidence_pack"] = {"items": items, "conflicts": []}
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"evidence_pack": state["evidence_pack"], "errors": errors}
    )
    return state


async def evidence_quality_gate_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    证据质量关卡
    检查是否有足够的证据支持决策
    """
    node = "EvidenceQualityGate"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success",
        {"evidence_count": len(state.get("evidence_pack", {}).get("items", []))}
    )
    
    sufficient = len(state.get("evidence_pack", {}).get("items", [])) >= 1
    gates = state.get("gates") or {}
    state["gates"] = gates
    gates["evidence_quality"] = {
        "result": "pass" if sufficient else "fail",
        "checkedAt": datetime.utcnow().isoformat(),
        "minRequired": 1
    }
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"evidence_quality": gates["evidence_quality"]}
    )
    return state


async def build_decision_record_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    构建决策记录
    生成最终建议和置信度
    """
    node = "BuildDecisionRecord"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success", {}
    )
    
    evidence_ids = [x["evidenceId"] for x in state.get("evidence_pack", {}).get("items", [])]
    normalized = state.get("normalized") or {}
    question = normalized.get("question") or ""
    domain = normalized.get("domain", "engineering")
    evidence_items = state.get("evidence_pack", {}).get("items", [])
    has_evidence = state["gates"]["evidence_quality"]["result"] != "fail"
    is_decision_mode = (normalized.get("intentMode") or "decision") == "decision"

    # 决策工作台默认按决策请求处理，避免误判成普通问答。
    if is_decision_mode:
        primary_evidence = evidence_items[0] if evidence_items else {}
        primary_tags = primary_evidence.get("tags", [])
        training_case = primary_evidence.get("trainingCase", {})
        recommendation = _extract_recommendation_from_tags(primary_tags)
        confidence = _extract_confidence_from_tags(primary_tags, fallback=0.45 if not has_evidence else 0.65)

        if not has_evidence:
            recommendation = "NEEDS_REVIEW"
            confidence = min(confidence, 0.45)

        evidence_quote = primary_evidence.get("quote", "训练样例暂未返回可用摘要。")
        evidence_source = primary_evidence.get("source", {}).get("title", "unknown-source")
        case_rationale = training_case.get("rationale") or evidence_quote
        case_uncertainties = training_case.get("uncertainties") or [
            "样例库可能与当前上下文不完全一致。",
            "部分约束（预算/时间/SLA）仍需补充量化。",
        ]
        case_next_steps = training_case.get("nextSteps") or [
            "补充真实业务数据源并重新运行决策。",
            "根据 replay 审计记录进行人工复核后再执行。",
        ]
        decision_out = {
            "recommendation": recommendation,
            "confidence": confidence,
            "rationale": [
                {
                    "text": f"问题：{question}。已命中样例证据：{evidence_source}。",
                    "evidenceIds": evidence_ids,
                },
                {
                    "text": f"依据摘要：{case_rationale}",
                    "evidenceIds": evidence_ids,
                },
                {
                    "text": f"训练样例原文：{evidence_quote}",
                    "evidenceIds": evidence_ids,
                },
                {
                    "text": f"结论按 {domain} 域策略生成，建议在执行前对关键约束进行人工复核。",
                    "evidenceIds": evidence_ids,
                },
            ],
            "safetyNotes": [
                "当前依据来自训练样例库，仅用于决策草案生成。",
                "涉及资金、医疗、安全等高风险事项时必须人工审批。",
            ],
            "uncertainties": case_uncertainties,
            "nextSteps": case_next_steps,
        }
    else:
        decision_out = {
            "recommendation": "NEEDS_REVIEW",
            "confidence": 0.4,
            "rationale": [
                {"text": "该请求未通过决策模式处理，请在请求中明确 intentMode=decision。", "evidenceIds": evidence_ids}
            ],
            "safetyNotes": ["请通过 Decision Studio 或 /decisions 接口触发标准决策流程。"],
            "uncertainties": ["请求模式不明确。"],
            "nextSteps": ["重新提交并携带 intentMode=decision。"],
        }
    state["decision_out"] = decision_out
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"decision_out": decision_out}
    )
    return state


async def finalize_node(state: GraphState, repo: DecisionRepo) -> GraphState:
    """
    最终化
    确定决策状态（final / draft）
    """
    node = "Finalize"
    await write_event(
        repo, state["decision_id"], "NODE_START", node, "success", {}
    )
    
    status = "final" if state["decision_out"]["confidence"] >= 0.7 else "draft"
    state["final_status"] = status
    
    await write_event(
        repo, state["decision_id"], "NODE_END", node, "success",
        {"final_status": status}
    )
    return state
