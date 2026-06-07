from langgraph.graph import StateGraph, END

from app.decision.state import GraphState
from app.decision.repo import DecisionRepo
from app.decision.nodes import (
    normalize_node, plan_node, permission_gate_node,
    tool_execute_node, tool_verify_node, evidence_quality_gate_node,
    build_decision_record_node, finalize_node
)


def _async_node(node_fn, repo: DecisionRepo):
    async def _wrapped(state: GraphState):
        return await node_fn(state, repo)

    return _wrapped


def build_graph(repo: DecisionRepo):
    """
    构建 LangGraph 状态机
    MVP：线性流程
    后续可加入条件边、循环、并行等
    """
    g = StateGraph(GraphState)
    
    # 添加节点（用 async 包装函数注入 repo，避免 coroutine 未被 await）
    g.add_node("Normalize", _async_node(normalize_node, repo))
    g.add_node("Plan", _async_node(plan_node, repo))
    g.add_node("PermissionGate", _async_node(permission_gate_node, repo))
    g.add_node("ToolExecute", _async_node(tool_execute_node, repo))
    g.add_node("ToolVerify", _async_node(tool_verify_node, repo))
    g.add_node("EvidenceQualityGate", _async_node(evidence_quality_gate_node, repo))
    g.add_node("BuildDecisionRecord", _async_node(build_decision_record_node, repo))
    g.add_node("Finalize", _async_node(finalize_node, repo))
    
    # 设置入口点
    g.set_entry_point("Normalize")
    
    # 添加边（线性流程）
    g.add_edge("Normalize", "Plan")
    g.add_edge("Plan", "PermissionGate")
    g.add_edge("PermissionGate", "ToolExecute")
    g.add_edge("ToolExecute", "ToolVerify")
    g.add_edge("ToolVerify", "EvidenceQualityGate")
    g.add_edge("EvidenceQualityGate", "BuildDecisionRecord")
    g.add_edge("BuildDecisionRecord", "Finalize")
    g.add_edge("Finalize", END)
    
    return g.compile()
