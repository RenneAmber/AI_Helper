from typing import TypedDict, List, Dict, Any, Optional


class GraphState(TypedDict, total=False):
    """
    LangGraph 状态定义
    每个节点都可以读写这个状态
    """
    decision_id: str
    request: Dict[str, Any]              # 原始输入
    normalized: Dict[str, Any]           # 补齐后的结构化输入
    plan: List[Dict[str, Any]]           # 执行计划
    evidence_pack: Dict[str, Any]        # 证据包
    conflicts: List[Dict[str, Any]]      # 冲突检测结果
    analysis: Dict[str, Any]             # 分析结果
    decision_out: Dict[str, Any]         # 最终决策输出
    tool_queue: List[Dict[str, Any]]     # 待执行工具队列
    tool_results: List[Dict[str, Any]]   # 工具执行结果
    gates: Dict[str, Any]                # 各个关卡的检查结果
    errors: List[Dict[str, Any]]         # 错误日志
    final_status: str                    # 最终状态
