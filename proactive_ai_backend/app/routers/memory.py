"""
语义记忆与摘要的管理 API：用于人工或上游服务向系统注入“关于用户的事实/偏好/承诺”，
并查询当前 session 的滚动摘要。

注：这里不做鉴权，生产环境必须挂上身份校验。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ..logging_setup import get_trace_id
from ..semantic_store import (
    list_facts,
    load_latest_summary,
    search_facts,
    upsert_fact,
)

router = APIRouter(prefix="/v1/memory", tags=["memory"])


class FactCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(profile|fact|reminder)$")
    content: str = Field(min_length=1, max_length=2000)


@router.post("/facts")
async def create_fact(payload: FactCreate) -> dict:
    fact_id = await upsert_fact(
        user_id=payload.user_id,
        kind=payload.kind,
        content=payload.content,
        source_trace_id=get_trace_id(),
    )
    return {"id": fact_id}


@router.get("/facts")
async def get_facts(user_id: str, limit: int = 50) -> dict:
    return {"items": await list_facts(user_id=user_id, limit=limit)}


@router.get("/facts/search")
async def search(user_id: str, q: str, top_k: int = 3) -> dict:
    return {"items": await search_facts(user_id=user_id, query=q, top_k=top_k)}


@router.get("/summaries/{session_id}")
async def get_summary(session_id: str) -> dict:
    summary = await load_latest_summary(session_id)
    return {"summary": summary}
