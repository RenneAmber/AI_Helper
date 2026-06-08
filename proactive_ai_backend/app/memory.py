from __future__ import annotations

import json
from typing import Any

import aiosqlite

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    results_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_workflows_user ON workflows(user_id);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 语义记忆：跨会话沉淀的事实/偏好/承诺，供 RAG 注入 prompt
CREATE TABLE IF NOT EXISTS semantic_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL,              -- profile | fact | reminder
    content TEXT NOT NULL,
    source_trace_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_semantic_user ON semantic_facts(user_id);
CREATE INDEX IF NOT EXISTS idx_semantic_kind ON semantic_facts(user_id, kind);

-- 滚动摘要：把旧消息压缩成一段文本，避免 prompt 无限增长
CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    last_message_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id);

-- Calendar 事件持久化（v1 SQLite 后端；联机 Google/Graph 适配器接入时可作为本地缓存层复用）
CREATE TABLE IF NOT EXISTS calendar_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    start_at TEXT NOT NULL,           -- ISO8601 (UTC)
    end_at TEXT NOT NULL,             -- ISO8601 (UTC)
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    attendees_json TEXT NOT NULL DEFAULT '[]',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL          -- ISO8601 (UTC)
);
CREATE INDEX IF NOT EXISTS idx_calendar_user_start ON calendar_events(user_id, start_at);

-- RAG 向量块：邮件/笔记/聊天均入同一表，向量以 float32 raw bytes 存 BLOB
CREATE TABLE IF NOT EXISTS rag_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,        -- 'email' | 'note' | 'chat' | ...
    source_id TEXT NOT NULL,          -- email uid / file path / chat msg id
    chunk_seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    embedding BLOB NOT NULL,          -- np.float32 raw bytes
    dim INTEGER NOT NULL,             -- 向量维度（校验防混入不同 embedder 的向量）
    embedder TEXT NOT NULL,           -- 'mock' | 'azure:text-embedding-3-small' | ...
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, source_type, source_id, chunk_seq)
);
CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_rag_user_source ON rag_chunks(user_id, source_type, source_id);
"""


async def init_db() -> None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def append_message(session_id: str, user_id: str, role: str, content: str) -> None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute(
            "INSERT INTO messages(session_id, user_id, role, content) VALUES (?, ?, ?, ?)",
            (session_id, user_id, role, content),
        )
        await db.commit()


async def load_history(session_id: str, limit: int = settings.memory_window_messages) -> list[dict[str, Any]]:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            "SELECT role, content, created_at FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cursor.fetchall()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in reversed(rows)]


async def count_messages(session_id: str) -> int:
    """统计 session 累计消息数，供摘要器判断是否需要压缩。"""
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id=?",
            (session_id,),
        )
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def load_messages_for_summary(
    session_id: str, after_id: int, keep_recent: int
) -> list[dict[str, Any]]:
    """
    取 session 中 id > after_id 的旧消息，但排除最近 keep_recent 条。
    用于把“远端”消息送去摘要器压缩，近端原样保留供模型使用。
    """
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            """
            SELECT id, role, content FROM messages
            WHERE session_id=? AND id > ?
            ORDER BY id ASC
            """,
            (session_id, after_id),
        )
        rows = await cursor.fetchall()
    if len(rows) <= keep_recent:
        return []
    chosen = rows[:-keep_recent] if keep_recent > 0 else rows
    return [{"id": r[0], "role": r[1], "content": r[2]} for r in chosen]


async def save_workflow(workflow_id: str, user_id: str, goal: str, status: str,
                        steps: list[dict], results: list[dict]) -> None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute(
            """
            INSERT INTO workflows(id, user_id, goal, status, steps_json, results_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              status=excluded.status,
              results_json=excluded.results_json,
              updated_at=CURRENT_TIMESTAMP
            """,
            (workflow_id, user_id, goal, status,
             json.dumps(steps, ensure_ascii=False),
             json.dumps(results, ensure_ascii=False)),
        )
        await db.commit()


async def load_workflow(workflow_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(
            "SELECT id, user_id, goal, status, steps_json, results_json, created_at, updated_at "
            "FROM workflows WHERE id=?",
            (workflow_id,),
        )
        row = await cursor.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "user_id": row[1],
        "goal": row[2],
        "status": row[3],
        "steps": json.loads(row[4]),
        "results": json.loads(row[5]),
        "created_at": row[6],
        "updated_at": row[7],
    }


async def record_incident(trace_id: str, kind: str, payload: dict) -> None:
    async with aiosqlite.connect(settings.sqlite_path) as db:
        await db.execute(
            "INSERT INTO incidents(trace_id, kind, payload_json) VALUES (?, ?, ?)",
            (trace_id, kind, json.dumps(payload, ensure_ascii=False)),
        )
        await db.commit()


async def list_incidents(
    limit: int = 50,
    kind: str | None = None,
    since_iso: str | None = None,
) -> list[dict[str, Any]]:
    """运维查询：按时间倒序拉最近的事故，可按 kind 过滤。
    `since_iso` 形如 '2026-06-01T00:00:00'，仅返回该时刻之后的记录。
    """
    sql = "SELECT id, trace_id, kind, payload_json, created_at FROM incidents"
    where: list[str] = []
    params: list[Any] = []
    if kind:
        where.append("kind = ?")
        params.append(kind)
    if since_iso:
        where.append("created_at >= ?")
        params.append(since_iso)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))

    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r[3])
        except (TypeError, ValueError):
            payload = {"_raw": r[3]}
        out.append(
            {
                "id": r[0],
                "trace_id": r[1],
                "kind": r[2],
                "payload": payload,
                "created_at": r[4],
            }
        )
    return out


async def incident_counts_by_kind(since_iso: str | None = None) -> list[dict[str, Any]]:
    """聚合视图：按 kind 统计事故数量，供运维 dashboard 展示分布。"""
    sql = "SELECT kind, COUNT(*) FROM incidents"
    params: list[Any] = []
    if since_iso:
        sql += " WHERE created_at >= ?"
        params.append(since_iso)
    sql += " GROUP BY kind ORDER BY 2 DESC"
    async with aiosqlite.connect(settings.sqlite_path) as db:
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
    return [{"kind": r[0], "count": int(r[1])} for r in rows]
