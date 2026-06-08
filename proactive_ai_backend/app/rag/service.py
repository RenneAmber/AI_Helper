"""
RAG 编排层 —— 把 embedder / chunker / store 串成 ingest/search/format 三件套。

上层入口（router / prompt_builder）只需调用本文件三个函数：
- ingest_email(user_id, email)        → 落库 + 返回 chunk 数
- ingest_text(user_id, source_type, source_id, text, metadata?) → 任意文本入库
- search(user_id, query, ...)         → 返回 SearchHit 列表
- format_as_context(hits, ...)        → 转成 prompt 友好的字符串
"""

from __future__ import annotations

import logging
from typing import Any

from . import store
from .chunker import EmailDoc, chunk_email
from .embeddings import get_embedder
from .store import SearchHit
from ..metrics import rag_ingest_chunks_total, rag_search_hits, rag_search_total

logger = logging.getLogger("rag.service")


async def ingest_email(user_id: str, email: dict | EmailDoc) -> int:
    """把一封邮件切块、向量化、写库；返回写入的 chunk 数。"""
    if isinstance(email, dict):
        doc = EmailDoc(
            uid=str(email.get("uid") or email.get("id") or ""),
            subject=email.get("subject", "") or "",
            sender=email.get("from", "") or email.get("sender", "") or "",
            date=email.get("date", "") or "",
            body=email.get("body", "") or email.get("content", "") or "",
            folder=email.get("folder", "") or "",
        )
    else:
        doc = email

    if not doc.uid:
        raise ValueError("email.uid is required for ingest")

    pieces = chunk_email(doc)
    embedder = await get_embedder()
    vectors = await embedder.embed(pieces)

    chunks = [
        store.Chunk(
            user_id=user_id,
            source_type="email",
            source_id=doc.uid,
            chunk_seq=i,
            text=p,
            metadata={
                "subject": doc.subject,
                "sender": doc.sender,
                "date": doc.date,
                "folder": doc.folder,
            },
        )
        for i, p in enumerate(pieces)
    ]
    # 同一封邮件再次 ingest 时，旧的多出来的 chunk 应该清掉
    await store.delete_source(user_id, "email", doc.uid)
    n = await store.upsert_chunks(chunks, vectors, embedder_name=embedder.name, dim=embedder.dim)
    rag_ingest_chunks_total.labels(embedder=embedder.name, source_type="email").inc(n)
    logger.info("rag_ingest_email", extra={"user_id": user_id, "uid": doc.uid, "chunks": n, "embedder": embedder.name})
    return n


async def ingest_text(
    user_id: str,
    source_type: str,
    source_id: str,
    text: str,
    metadata: dict | None = None,
) -> int:
    """通用入口：把任意文本作为单源 ingest（自动按 chunk_chars 切）。"""
    from .chunker import _split_long, _with_overlap, _split_by_paragraph
    from ..config import settings as _settings

    paragraphs = _split_by_paragraph(text or "")
    pieces: list[str] = []
    buf = ""
    for p in paragraphs:
        for piece in _split_long(p, _settings.rag_chunk_chars):
            if not buf:
                buf = piece
            elif len(buf) + len(piece) + 2 <= _settings.rag_chunk_chars:
                buf = f"{buf}\n\n{piece}"
            else:
                pieces.append(buf)
                buf = piece
    if buf:
        pieces.append(buf)
    if not pieces:
        pieces = [text or ""]
    pieces = _with_overlap(pieces, _settings.rag_chunk_overlap)

    embedder = await get_embedder()
    vectors = await embedder.embed(pieces)
    chunks = [
        store.Chunk(
            user_id=user_id,
            source_type=source_type,
            source_id=source_id,
            chunk_seq=i,
            text=p,
            metadata=metadata or {},
        )
        for i, p in enumerate(pieces)
    ]
    await store.delete_source(user_id, source_type, source_id)
    n = await store.upsert_chunks(chunks, vectors, embedder_name=embedder.name, dim=embedder.dim)
    rag_ingest_chunks_total.labels(embedder=embedder.name, source_type=source_type).inc(n)
    logger.info(
        "rag_ingest_text",
        extra={"user_id": user_id, "type": source_type, "id": source_id, "chunks": n, "embedder": embedder.name},
    )
    return n


async def search(
    user_id: str,
    query: str,
    *,
    top_k: int = 5,
    source_types: list[str] | None = None,
    min_score: float = -1.0,
) -> list[SearchHit]:
    if not query.strip():
        return []
    embedder = await get_embedder()
    q_vec = await embedder.embed([query])
    try:
        hits = await store.search(
            user_id,
            q_vec[0],
            top_k=top_k,
            source_types=source_types,
            min_score=min_score,
        )
    except Exception:
        rag_search_total.labels(embedder=embedder.name, outcome="error").inc()
        raise
    rag_search_hits.labels(embedder=embedder.name).observe(len(hits))
    rag_search_total.labels(
        embedder=embedder.name, outcome="hit" if hits else "miss",
    ).inc()
    return hits


def format_as_context(hits: list[SearchHit], *, max_chars: int = 4000) -> str:
    """把命中转成可塞进 prompt 的紧凑文本块；超过 max_chars 截断。

    输出形如：
        [RAG #1 email score=0.83]
        [Subject] ...
        [From] ...
        正文...

        [RAG #2 ...]
        ...
    """
    if not hits:
        return ""
    blocks: list[str] = []
    used = 0
    for i, h in enumerate(hits, 1):
        head = f"[RAG #{i} {h.source_type} score={h.score:.2f} id={h.source_id}]"
        body = h.text
        block = f"{head}\n{body}".strip()
        if used + len(block) + 2 > max_chars:
            # 还能塞一部分就塞一部分
            remaining = max_chars - used - len(head) - 8
            if remaining > 80:
                block = f"{head}\n{body[:remaining]}…"
                blocks.append(block)
            break
        blocks.append(block)
        used += len(block) + 2
    return "\n\n".join(blocks)
