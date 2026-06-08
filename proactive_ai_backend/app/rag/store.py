"""
RAG 向量存储 —— SQLite + 内存 numpy 余弦相似度。

为何不直接用 Chroma / Qdrant / sqlite-vec？
-------------------------------------------
1. 项目其他模块已经统一在 aiosqlite + proactive_ai.db 上落库（messages/
   workflows/calendar_events 等），保持一致。
2. 单用户 < 10K chunks 时，全表扫 + numpy dot 几十毫秒即可；超过这个量级
   再换 sqlite-vec / FAISS / pgvector，只需替换本文件，不影响上层。
3. sqlite-vec 在 Windows 上需要加载 C 扩展，给开发者徒增门槛。

接口与 LangChain VectorStore 对齐：
    upsert_chunks / search / delete_source / delete_user / count
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import aiosqlite
import numpy as np

from ..config import settings

logger = logging.getLogger("rag.store")


@dataclass
class Chunk:
    """落库前的 chunk 表示。embedding 由 service 层补齐。"""
    user_id: str
    source_type: str
    source_id: str
    chunk_seq: int
    text: str
    metadata: dict[str, Any]


@dataclass
class SearchHit:
    id: int
    user_id: str
    source_type: str
    source_id: str
    chunk_seq: int
    text: str
    metadata: dict[str, Any]
    score: float                # cosine similarity, [-1, 1]
    created_at: str


# ---------- upsert ----------

async def upsert_chunks(
    chunks: list[Chunk],
    embeddings: np.ndarray,
    *,
    embedder_name: str,
    dim: int,
) -> int:
    """批量写入；冲突 (user_id, source_type, source_id, chunk_seq) 时整行替换。

    返回成功写入条数。
    """
    if len(chunks) == 0:
        return 0
    if embeddings.shape != (len(chunks), dim):
        raise ValueError(
            f"embeddings shape mismatch: got {embeddings.shape}, expect ({len(chunks)}, {dim})"
        )
    # 强制 float32 + contiguous，确保 .tobytes() 是确定性的
    embeddings = np.ascontiguousarray(embeddings, dtype=np.float32)

    rows = [
        (
            c.user_id,
            c.source_type,
            c.source_id,
            c.chunk_seq,
            c.text,
            json.dumps(c.metadata, ensure_ascii=False),
            embeddings[i].tobytes(),
            dim,
            embedder_name,
        )
        for i, c in enumerate(chunks)
    ]
    sql = (
        "INSERT INTO rag_chunks "
        "(user_id, source_type, source_id, chunk_seq, text, metadata_json, embedding, dim, embedder) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, source_type, source_id, chunk_seq) DO UPDATE SET "
        "  text=excluded.text, metadata_json=excluded.metadata_json, "
        "  embedding=excluded.embedding, dim=excluded.dim, embedder=excluded.embedder, "
        "  created_at=CURRENT_TIMESTAMP"
    )
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.executemany(sql, rows)
        await db.commit()
    logger.info("rag_upsert", extra={"count": len(rows), "embedder": embedder_name, "dim": dim})
    return len(rows)


# ---------- search ----------

async def search(
    user_id: str,
    query_embedding: np.ndarray,
    *,
    top_k: int = 5,
    source_types: list[str] | None = None,
    min_score: float = -1.0,
) -> list[SearchHit]:
    """对该 user 的所有 chunk 做余弦相似度排序，返回 top_k。

    向量已在 upsert/embed 阶段归一化，所以这里只需做内积。
    维度不一致的行（embedder 切换后的脏数据）会被跳过并 warning。

    默认 min_score=-1.0（即不截断），避免 MockEmbedder 这种合成向量产生负分
    时插该 top_k。业务侧需要阈值过滤时显式传入正数即可。
    """
    q = np.ascontiguousarray(query_embedding, dtype=np.float32).reshape(-1)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q = q / q_norm
    q_dim = q.shape[0]

    where = ["user_id = ?"]
    params: list[Any] = [user_id]
    if source_types:
        placeholders = ",".join("?" * len(source_types))
        where.append(f"source_type IN ({placeholders})")
        params.extend(source_types)
    sql = (
        "SELECT id, user_id, source_type, source_id, chunk_seq, text, metadata_json, "
        "       embedding, dim, embedder, created_at "
        f"FROM rag_chunks WHERE {' AND '.join(where)}"
    )

    async with aiosqlite.connect(settings.sqlite_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    if not rows:
        return []

    # 一次性堆成矩阵；脏维度行单独剔除
    valid_rows = []
    mats = []
    skipped = 0
    for r in rows:
        if r["dim"] != q_dim:
            skipped += 1
            continue
        valid_rows.append(r)
        mats.append(np.frombuffer(r["embedding"], dtype=np.float32))
    if skipped:
        logger.warning("rag_search_skipped_dim_mismatch", extra={"skipped": skipped, "q_dim": q_dim})
    if not valid_rows:
        return []

    M = np.vstack(mats)  # shape (N, q_dim)
    scores = M @ q       # shape (N,)
    # 取分数 ≥ min_score 的，按分数降序
    order = np.argsort(-scores)
    out: list[SearchHit] = []
    for idx in order:
        s = float(scores[idx])
        if s < min_score:
            break
        r = valid_rows[idx]
        out.append(SearchHit(
            id=r["id"],
            user_id=r["user_id"],
            source_type=r["source_type"],
            source_id=r["source_id"],
            chunk_seq=r["chunk_seq"],
            text=r["text"],
            metadata=json.loads(r["metadata_json"] or "{}"),
            score=s,
            created_at=str(r["created_at"]),
        ))
        if len(out) >= top_k:
            break
    return out


# ---------- delete / stats ----------

async def delete_source(user_id: str, source_type: str, source_id: str) -> int:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cur = await db.execute(
            "DELETE FROM rag_chunks WHERE user_id = ? AND source_type = ? AND source_id = ?",
            (user_id, source_type, source_id),
        )
        await db.commit()
        return cur.rowcount or 0


async def delete_user(user_id: str) -> int:
    """GDPR 右-被遗忘权：清空该用户全部向量。"""
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cur = await db.execute("DELETE FROM rag_chunks WHERE user_id = ?", (user_id,))
        await db.commit()
        return cur.rowcount or 0


async def count(user_id: str | None = None) -> dict[str, int]:
    """返回 {'total': N, 'by_source_type': {...}} 简单统计。"""
    async with aiosqlite.connect(settings.sqlite_path) as db:
        if user_id:
            async with db.execute(
                "SELECT source_type, COUNT(*) FROM rag_chunks WHERE user_id = ? GROUP BY source_type",
                (user_id,),
            ) as cur:
                by_type = {row[0]: row[1] for row in await cur.fetchall()}
        else:
            async with db.execute(
                "SELECT source_type, COUNT(*) FROM rag_chunks GROUP BY source_type"
            ) as cur:
                by_type = {row[0]: row[1] for row in await cur.fetchall()}
    return {"total": sum(by_type.values()), "by_source_type": by_type}
