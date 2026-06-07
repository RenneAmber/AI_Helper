"""
Decision Copilot API 路由
"""

import uuid
from datetime import datetime
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from app.copilot.copilot_state import CopilotState
from app.copilot.copilot_graph import build_copilot_graph


router = APIRouter(prefix="/copilot", tags=["copilot"])


SUPPORTED_DOMAINS = ["engineering", "product", "business", "personal", "medical"]


# ============ Schemas ============

class CopilotDecisionRequestBody(BaseModel):
    """决策请求"""
    problem_statement: str = Field(..., description="决策问题描述")
    domain: str = Field("engineering", description="决策域：engineering/product/business/personal/medical")
    evaluation_criteria: Dict[str, float] = Field(
        default_factory=lambda: {
            "effectiveness": 0.4,
            "feasibility": 0.3,
            "risk": 0.2,
            "cost": 0.1
        },
        description="评估标准及权重"
    )
    user_id: str = Field("unknown", description="用户ID")


# ============ Endpoints ============


async def run_copilot_decision(request: CopilotDecisionRequestBody) -> Dict[str, Any]:
    """执行一次完整的 Decision Copilot 工作流。"""
    # Validate input
    problem = request.problem_statement.strip()
    domain = request.domain.lower()
    criteria = request.evaluation_criteria
    user_id = request.user_id

    if not problem:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="problem_statement is required"
        )

    if domain not in SUPPORTED_DOMAINS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"domain must be one of: {', '.join(SUPPORTED_DOMAINS)}"
        )

    # Normalize criteria (ensure they sum to 1.0)
    total_weight = sum(criteria.values())
    if total_weight > 0:
        criteria = {k: v / total_weight for k, v in criteria.items()}

    decision_id = f"dec_{uuid.uuid4().hex[:8]}"
    initial_state = CopilotState(
        decision_id=decision_id,
        user_id=user_id,
        domain=domain,
        problem_statement=problem,
        evaluation_criteria=criteria,
    )

    print(f"\n{'='*60}")
    print(f"Decision Copilot: {decision_id}")
    print(f"Problem: {problem[:80]}")
    print(f"Domain: {domain}")
    print(f"{'='*60}\n")

    graph = build_copilot_graph()
    final_state_dict = await graph.ainvoke(initial_state.dict())
    final_state = CopilotState(**final_state_dict)

    print(f"\n{'='*60}")
    print(f"Decision completed: {final_state.decision_id}")
    print(f"Recommendation: {final_state.primary_recommendation.name if final_state.primary_recommendation else 'N/A'}")
    print(f"Confidence: {final_state.recommendation_confidence}")
    print(f"{'='*60}\n")

    return {
        "decision_id": final_state.decision_id,
        "status": "completed",
        "data": final_state.dict()
    }

@router.post("/decisions")
async def create_copilot_decision(request: CopilotDecisionRequestBody):
    """
    创建并运行 Decision Copilot
    
    一次性调用即返回完整决策 (MVP 版本)
    
    Request Example:
    {
        "problem_statement": "Should we migrate from monolith to microservices?",
        "domain": "engineering",
        "evaluation_criteria": {
            "scalability": 0.3,
            "maintainability": 0.3,
            "risk": 0.2,
            "cost": 0.2
        },
        "user_id": "u_admin"
    }
    
    Response:
    {
        "decision_id": "dec_xxx",
        "status": "completed",
        "data": {
            "clarified_context": {...},
            "generated_options": [...],
            "primary_recommendation": {...},
            "alternative_recommendations": [...],
            "key_risks": [...],
            "mitigation_strategies": [...],
            "recommendation_confidence": 0.85,
            "audit_trail": [...]
        }
    }
    """
    
    try:
        return await run_copilot_decision(request)
    
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR in create_copilot_decision: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Decision creation failed: {str(e)}"
        )


@router.get("/decisions/{decision_id}")
async def get_copilot_decision(decision_id: str):
    """获取已保存的决策"""
    # TODO: 从数据库查询
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Not implemented yet"
    )


@router.post("/decisions/{decision_id}/accept")
async def accept_recommendation(decision_id: str, feedback: dict = None):
    """用户接受推荐"""
    # TODO: 更新决策状态
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Not implemented yet"
    )


@router.post("/decisions/{decision_id}/modify")
async def request_modification(decision_id: str, feedback: dict):
    """用户请求修改（触发循环回到澄清阶段）"""
    # TODO: 支持多轮对话模式 (Phase 2: WebSocket)
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="Not implemented yet (phase 2: WebSocket streaming)"
    )
