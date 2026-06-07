"""
Decision Copilot LangGraph 定义
"""

from langgraph.graph import StateGraph, END
from typing_extensions import TypedDict, Annotated
from typing import List, Dict, Any, Optional
from app.copilot.copilot_state import (
    CopilotState, ClarifiedContext, DecisionOption
)
from app.copilot.copilot_nodes import CopilotNodes
from app.copilot.copilot_tools import CopilotTools


# Define state as TypedDict for LangGraph
class CopilotGraphState(TypedDict, total=False):
    """LangGraph 状态定义"""
    decision_id: str
    user_id: str
    domain: str
    status: str
    problem_statement: str
    evaluation_criteria: Dict[str, float]
    clarified_context: Optional[Dict[str, Any]]
    explicit_assumptions: List[Dict[str, Any]]
    generated_options: List[Dict[str, Any]]
    primary_recommendation: Optional[Dict[str, Any]]
    recommendation_confidence: str
    recommendation_confidence_score: float
    recommendation_rationale: str
    ranked_recommendations: List[Dict[str, Any]]
    alternative_recommendations: List[Dict[str, Any]]
    key_risks: List[Dict[str, Any]]
    mitigation_strategies: List[str]
    next_steps_to_strengthen: List[str]
    user_feedback: Optional[str]
    accepted: bool
    created_at: Optional[str]
    updated_at: Optional[str]
    audit_trail: List[Dict[str, Any]]


def _convert_state_to_copilot_state(state: Dict[str, Any]) -> CopilotState:
    """将字典状态转换为 CopilotState"""
    return CopilotState(**state)


def _convert_copilot_state_to_dict(state: CopilotState) -> Dict[str, Any]:
    """将 CopilotState 转换为字典"""
    return state.dict()


def build_copilot_graph():
    """
    构建 Decision Copilot 工作流 - 新版本支持假设显性化
    
    Graph:
    START 
      → ClarifyContext        (澄清目标/约束)
      → ExtractAssumptions    (显性化假设) 【新增】
      → GenerateOptions       (生成方案)
      → EvaluateOptions       (轻量评估)
      → RankAndRecommend      (推荐+下一步)
      → SaveRecord            (保存为Draft)
      → END
    """
    
    # 初始化工具和节点
    tools = CopilotTools()
    nodes_obj = CopilotNodes(tools)
    
    # 创建状态图
    g = StateGraph(CopilotGraphState)
    
    # 包装节点函数，处理状态转换
    async def clarify_node_wrapper(state):
        copilot_state = _convert_state_to_copilot_state(state)
        result_state = await nodes_obj.clarify_context_node(copilot_state)
        return _convert_copilot_state_to_dict(result_state)
    
    async def extract_assumptions_node_wrapper(state):
        copilot_state = _convert_state_to_copilot_state(state)
        result_state = await nodes_obj.extract_assumptions_node(copilot_state)
        return _convert_copilot_state_to_dict(result_state)
    
    async def generate_node_wrapper(state):
        copilot_state = _convert_state_to_copilot_state(state)
        result_state = await nodes_obj.generate_options_node(copilot_state)
        return _convert_copilot_state_to_dict(result_state)
    
    async def evaluate_node_wrapper(state):
        copilot_state = _convert_state_to_copilot_state(state)
        result_state = await nodes_obj.evaluate_options_node(copilot_state)
        return _convert_copilot_state_to_dict(result_state)
    
    async def rank_node_wrapper(state):
        copilot_state = _convert_state_to_copilot_state(state)
        result_state = await nodes_obj.rank_and_recommend_node(copilot_state)
        return _convert_copilot_state_to_dict(result_state)
    
    async def save_node_wrapper(state):
        copilot_state = _convert_state_to_copilot_state(state)
        result_state = await nodes_obj.save_record_node(copilot_state)
        return _convert_copilot_state_to_dict(result_state)
    
    # 添加节点
    g.add_node("ClarifyContext", clarify_node_wrapper)
    g.add_node("ExtractAssumptions", extract_assumptions_node_wrapper)  # 新增
    g.add_node("GenerateOptions", generate_node_wrapper)
    g.add_node("EvaluateOptions", evaluate_node_wrapper)
    g.add_node("RankAndRecommend", rank_node_wrapper)
    g.add_node("SaveRecord", save_node_wrapper)
    
    # 设置入口点
    g.set_entry_point("ClarifyContext")
    
    # 添加边（线性流程）
    g.add_edge("ClarifyContext", "ExtractAssumptions")
    g.add_edge("ExtractAssumptions", "GenerateOptions")
    g.add_edge("GenerateOptions", "EvaluateOptions")
    g.add_edge("EvaluateOptions", "RankAndRecommend")
    g.add_edge("RankAndRecommend", "SaveRecord")
    g.add_edge("SaveRecord", END)
    
    # 编译图
    return g.compile()
