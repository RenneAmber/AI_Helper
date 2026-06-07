"""
Decision Copilot 状态定义
支持完整的决策辅助工作流：澄清 -> 假设显性化 -> 生成 -> 轻量评估 -> 推荐 -> 保存
关键理念：无证据友好，强调假设显性化而不是追求权威结论
"""

from typing import Any, Optional, List, Dict
from enum import Enum
from pydantic import BaseModel, Field


class ConfidenceLevel(str, Enum):
    """置信度等级"""
    HIGH = "high"          # 基于充分证据
    MEDIUM = "medium"      # 基于部分证据
    LOW = "low"            # 主要是假设
    VERY_LOW = "very_low"  # 极少信息


class Assumption(BaseModel):
    """一个显性化的假设"""
    id: str
    statement: str  # 假设的陈述（例："团队规模 < 5人"）
    justification: str  # 为什么做这个假设
    confidence: ConfidenceLevel  # 这个假设本身的置信度
    can_be_verified: bool = True  # 是否能被验证
    how_to_verify: Optional[str] = None  # 如何验证（例："查看最近的 HR 报告"）
    impact_if_wrong: str = "medium"  # 如果假设错了会怎样 (low/medium/high)


class DecisionOption(BaseModel):
    """决策选项"""
    id: str
    name: str
    description: str
    pros: List[str] = Field(default_factory=list)
    cons: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    estimated_effort: Optional[str] = None  # low/medium/high
    timeline: Optional[str] = None
    dependencies: List[str] = Field(default_factory=list)
    score: float = 0.0  # 0-1
    score_confidence: ConfidenceLevel = ConfidenceLevel.LOW  # 评分的置信度
    rationale: str = ""
    assumption_ids: List[str] = Field(default_factory=list)  # 这个方案依赖的假设 ID


class ClarifiedContext(BaseModel):
    """澄清后的上下文"""
    original_problem: str
    refined_problem: str  # 澄清后的问题
    objectives: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    stakeholders: List[str] = Field(default_factory=list)
    timeline: Optional[str] = None
    budget_or_scope: Optional[str] = None
    success_criteria: List[str] = Field(default_factory=list)


class CopilotState(BaseModel):
    """Decision Copilot 工作流状态"""
    
    class Config:
        arbitrary_types_allowed = True
    
    # 基本信息
    decision_id: str
    user_id: str
    domain: str  # engineering, product, business, personal
    status: str = "draft"  # draft / accepted / rejected
    
    # 输入
    problem_statement: str
    evaluation_criteria: Dict[str, float] = Field(default_factory=dict)  # {criterion: weight}
    
    # 第一步：澄清上下文
    clarified_context: Optional[ClarifiedContext] = None
    
    # 第二步：显性化假设（核心创新）
    explicit_assumptions: List[Assumption] = Field(default_factory=list)
    
    # 第三步：生成方案
    generated_options: List[DecisionOption] = Field(default_factory=list)
    
    # 第四步：轻量评估
    primary_recommendation: Optional[DecisionOption] = None
    recommendation_confidence: ConfidenceLevel = ConfidenceLevel.LOW
    recommendation_confidence_score: float = 0.0  # 0-1 的数值
    recommendation_rationale: str = ""
    ranked_recommendations: List[Dict[str, Any]] = Field(default_factory=list)
    alternative_recommendations: List[DecisionOption] = Field(default_factory=list)
    
    # 风险分析
    key_risks: List[Dict[str, Any]] = Field(default_factory=list)
    mitigation_strategies: List[str] = Field(default_factory=list)
    
    # 假设升级路径（关键：告诉用户如何提升置信度）
    next_steps_to_strengthen: List[str] = Field(default_factory=list)
    
    # 用户反馈
    user_feedback: Optional[str] = None
    accepted: bool = False
    
    # 元数据
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    audit_trail: List[Dict[str, Any]] = Field(default_factory=list)
