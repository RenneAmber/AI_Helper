from fastapi import APIRouter, HTTPException, status

from app.decision.schemas import CreateDecisionRequest
from app.routers.copilot import CopilotDecisionRequestBody, run_copilot_decision

router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.post("")
async def create_decision(
    payload: CreateDecisionRequest,
):
    """
    POST /decisions
    兼容旧入口，但直接运行新的 Decision Copilot。
    """
    criteria = {
        item.key: item.weight
        for item in payload.criteria
    }
    copilot_payload = CopilotDecisionRequestBody(
        problem_statement=payload.question,
        domain=payload.domain,
        evaluation_criteria=criteria,
        user_id=payload.requester.get("userId", "unknown"),
    )
    result = await run_copilot_decision(copilot_payload)
    result["decisionId"] = result["decision_id"]
    return result


@router.post("/{decision_id}/run")
async def run_decision(
    decision_id: str,
):
    """
    POST /decisions/{decision_id}/run
    旧接口已废弃：创建时已同步完成执行。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Decision run is obsolete. Use POST /decisions for the new synchronous Copilot workflow."
    )


@router.get("/{decision_id}/replay")
async def replay(
    decision_id: str,
):
    """
    GET /decisions/{decision_id}/replay
    旧 replay 已废弃，新 Copilot 当前返回完整结果但不保留 replay 流。
    """
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail="Decision replay is obsolete in the new Copilot workflow."
    )
