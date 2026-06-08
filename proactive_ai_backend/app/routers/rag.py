"""
RAG REST API —— ingest（单封/批量邮件、自由文本）+ search + stats + delete。

注：未加鉴权。生产环境必须挂身份校验（避免任意用户 ingest/search 他人邮件）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..rag import service as rag_service
from ..rag import store as rag_store
from ..rag.embeddings import get_embedder

router = APIRouter(prefix="/v1/rag", tags=["rag"])


# ---------- payload models ----------

class EmailIngestItem(BaseModel):
    uid: str = Field(min_length=1, max_length=256)
    subject: str = ""
    sender: str = Field(default="", alias="from")  # 兼容 "from" 字段名
    date: str = ""
    body: str = ""
    folder: str = ""

    model_config = {"populate_by_name": True}


class IngestEmailRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    emails: list[EmailIngestItem] = Field(min_length=1, max_length=200)


class IngestTextRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    source_type: str = Field(min_length=1, max_length=32)
    source_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1, max_length=200_000)
    metadata: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=50)
    source_types: list[str] | None = None
    min_score: float = Field(default=-1.0, ge=-1.0, le=1.0)


# ---------- endpoints ----------

@router.post("/ingest/email")
async def ingest_emails(req: IngestEmailRequest) -> dict:
    total = 0
    per_email: list[dict] = []
    for item in req.emails:
        try:
            n = await rag_service.ingest_email(req.user_id, item.model_dump(by_alias=False))
            per_email.append({"uid": item.uid, "chunks": n, "ok": True})
            total += n
        except Exception as exc:  # 单封邮件失败不阻断整批
            per_email.append({"uid": item.uid, "ok": False, "error": str(exc)})
    return {"ingested_chunks": total, "details": per_email}


@router.post("/ingest/text")
async def ingest_text(req: IngestTextRequest) -> dict:
    n = await rag_service.ingest_text(
        req.user_id, req.source_type, req.source_id, req.text, req.metadata
    )
    return {"ingested_chunks": n}


@router.post("/search")
async def search(req: SearchRequest) -> dict:
    hits = await rag_service.search(
        req.user_id,
        req.query,
        top_k=req.top_k,
        source_types=req.source_types,
        min_score=req.min_score,
    )
    return {
        "count": len(hits),
        "hits": [
            {
                "id": h.id,
                "source_type": h.source_type,
                "source_id": h.source_id,
                "chunk_seq": h.chunk_seq,
                "text": h.text,
                "metadata": h.metadata,
                "score": h.score,
                "created_at": h.created_at,
            }
            for h in hits
        ],
    }


@router.get("/stats")
async def stats(user_id: str | None = None) -> dict:
    embedder = await get_embedder()
    base = await rag_store.count(user_id)
    return {**base, "embedder": embedder.name, "dim": embedder.dim}


@router.delete("/source")
async def delete_source(user_id: str, source_type: str, source_id: str) -> dict:
    if not user_id:
        raise HTTPException(400, "user_id required")
    n = await rag_store.delete_source(user_id, source_type, source_id)
    return {"deleted": n}


@router.delete("/user/{user_id}")
async def delete_user(user_id: str) -> dict:
    """GDPR：清空该用户全部向量。生产环境务必加鉴权。"""
    n = await rag_store.delete_user(user_id)
    return {"deleted": n}
