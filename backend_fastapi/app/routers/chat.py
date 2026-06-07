from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agent.chat_agent import execute_agent, stream_agent_answer
from ..core.trace import get_trace_id
from ..database import get_db
from ..models import AuditLog, Conversation
from ..redis_store import append_memory, update_session_state
from ..schemas_chat import ChatRequest, ChatResponse, ReplayResponse

router = APIRouter(prefix="/internal", tags=["chat"])


async def verify_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    from ..config import settings

    if x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal token")


async def _save_chat(
    db: AsyncSession,
    payload: ChatRequest,
    answer: str,
    evidence: list[dict],
    route: str,
    client_ip: str,
) -> None:
    trace_id = get_trace_id()
    db.add(
        Conversation(
            session_id=payload.session_id,
            user_id=payload.user_id,
            role="user",
            content=payload.message,
            metadata_json={"trace_id": trace_id},
        )
    )
    db.add(
        Conversation(
            session_id=payload.session_id,
            user_id=payload.user_id,
            role="assistant",
            content=answer,
            metadata_json={"trace_id": trace_id, "evidence_count": len(evidence)},
        )
    )
    db.add(
        AuditLog(
            event_type="chat.completed" if not payload.force_fail else "chat.failed",
            user_id=payload.user_id,
            session_id=payload.session_id,
            route=route,
            client_ip=client_ip,
            details_json={"trace_id": trace_id, "force_fail": payload.force_fail, "evidence": evidence},
        )
    )
    await db.commit()


@router.post(
    "/chat",
    response_model=ChatResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def internal_chat(
    payload: ChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ChatResponse:
    answer, evidence = await execute_agent(payload.message, force_fail=payload.force_fail)

    await _save_chat(
        db=db,
        payload=payload,
        answer=answer,
        evidence=evidence,
        route=request.url.path,
        client_ip=request.client.host if request.client else "",
    )

    await update_session_state(
        payload.session_id,
        {"user_id": payload.user_id, "last_message": payload.message, "trace_id": get_trace_id()},
    )
    await append_memory(payload.user_id, "user", payload.message)
    await append_memory(payload.user_id, "assistant", answer)

    return ChatResponse(
        session_id=payload.session_id,
        answer=answer,
        trace_id=get_trace_id(),
        evidence=evidence,
    )


@router.post(
    "/chat/stream",
    dependencies=[Depends(verify_internal_token)],
)
async def internal_chat_stream(
    payload: ChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    answer, evidence = await execute_agent(payload.message, force_fail=payload.force_fail)

    async def event_gen():
        async for event in stream_agent_answer(answer):
            yield event
        await _save_chat(
            db=db,
            payload=payload,
            answer=answer,
            evidence=evidence,
            route=request.url.path,
            client_ip=request.client.host if request.client else "",
        )
        await update_session_state(
            payload.session_id,
            {"user_id": payload.user_id, "last_message": payload.message, "trace_id": get_trace_id()},
        )
        await append_memory(payload.user_id, "user", payload.message)
        await append_memory(payload.user_id, "assistant", answer)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get(
    "/chat/replay/{trace_id}",
    response_model=ReplayResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def replay(trace_id: str, db: AsyncSession = Depends(get_db)) -> ReplayResponse:
    logs_stmt = select(AuditLog).where(AuditLog.details_json["trace_id"].astext == trace_id)
    conv_stmt = select(Conversation).where(Conversation.metadata_json["trace_id"].astext == trace_id)

    logs = (await db.execute(logs_stmt)).scalars().all()
    conversations = (await db.execute(conv_stmt)).scalars().all()

    return ReplayResponse(
        trace_id=trace_id,
        audit_logs=[
            {
                "event_type": log.event_type,
                "user_id": log.user_id,
                "session_id": log.session_id,
                "route": log.route,
                "details": log.details_json,
                "created_at": log.created_at.isoformat() if log.created_at else "",
            }
            for log in logs
        ],
        conversations=[
            {
                "session_id": item.session_id,
                "user_id": item.user_id,
                "role": item.role,
                "content": item.content,
                "metadata": item.metadata_json,
                "created_at": item.created_at.isoformat() if item.created_at else "",
            }
            for item in conversations
        ],
    )
