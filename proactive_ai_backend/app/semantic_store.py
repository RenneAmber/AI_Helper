"""
语义记忆与滚动摘要的持久层。

semantic_facts：跨会话保留的“关于用户/世界”的事实、偏好、待办。
summaries：把旧对话压缩成段落文本，按 session 维度滚动。

当前检索使用简单关键词打分。后续可替换为向量召回（embedding 列已预留扩展空间）。
"""

from __future__ import annotations

import re
from typing import Any

import aiosqlite

from .config import settings


_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 2}


# ----- semantic_facts -------------------------------------------------------

async def upsert_fact(
    *,
    user_id: str,
    kind: str,
    content: str,
    source_trace_id: str | None = None,
) -> int:
    """写入一条语义记忆。kind ∈ {profile, fact, reminder}。"""
    if kind not in {"profile", "fact", "reminder"}:
        raise ValueError(f"invalid fact kind: {kind}")
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            """
            INSERT INTO semantic_facts(user_id, kind, content, source_trace_id)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, kind, content, source_trace_id),
        )
        await db.commit()
        return cursor.lastrowid or 0


async def list_facts(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            """
            SELECT id, kind, content, source_trace_id, created_at
            FROM semantic_facts
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": r[0],
            "kind": r[1],
            "content": r[2],
            "source_trace_id": r[3],
            "created_at": r[4],
        }
        for r in rows
    ]


async def search_facts(
    *,
    user_id: str,
    query: str,
    top_k: int = settings.semantic_top_k,
) -> list[dict[str, Any]]:
    """
    基于词项重合度的轻量检索。
    - 命中越多得分越高
    - 兜底返回最新事实，避免空结果
    后续可替换为向量召回（content 旁加 embedding 列即可）。
    """
    facts = await list_facts(user_id, limit=200)
    q_tokens = _tokenize(query)
    if not q_tokens:
        return facts[:top_k]
    scored: list[tuple[int, dict[str, Any]]] = []
    for fact in facts:
        f_tokens = _tokenize(fact["content"])
        score = len(q_tokens & f_tokens)
        if score > 0:
            scored.append((score, fact))
    scored.sort(key=lambda x: (-x[0], -int(x[1]["id"])))
    selected = [fact for _, fact in scored[:top_k]]
    if not selected:
        selected = facts[:top_k]
    return selected


# ----- summaries -----------------------------------------------------------

async def save_summary(session_id: str, last_message_id: int, summary: str) -> None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute(
            """
            INSERT INTO summaries(session_id, last_message_id, summary)
            VALUES (?, ?, ?)
            """,
            (session_id, last_message_id, summary),
        )
        await db.commit()


async def load_latest_summary(session_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            """
            SELECT id, last_message_id, summary, created_at
            FROM summaries
            WHERE session_id=?
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "last_message_id": row[1],
        "summary": row[2],
        "created_at": row[3],
    }
