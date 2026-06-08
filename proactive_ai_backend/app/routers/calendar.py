"""
Calendar 只读 REST + 简易 Web 视图。

为何独立：
- agent 模式调用 calendar_list_events 是"会话式"，需要 LLM 配合
- 直接验证 / 排查时希望有一条"绕过 LLM"的路径，所以加这个只读 REST
- 写操作仍然只走 agent + allow_send 守门，避免误触发
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import APIRouter, HTTPException, Query

from ..config import settings

router = APIRouter(prefix="/v1/calendar", tags=["calendar"])


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "title": row["title"],
        "start_at": row["start_at"],
        "end_at": row["end_at"],
        "location": row["location"],
        "description": row["description"],
        "attendees": row["attendees_json"],   # 保留原 JSON 字符串，前端自己 parse
        "source": row["source"],
        "created_at": row["created_at"],
    }


@router.get("/events")
async def list_events(
    user_id: str | None = Query(default=None, description="为空则返回所有用户的事件"),
    time_min: str | None = Query(default=None, description="ISO8601，仅返回结束晚于此时刻"),
    time_max: str | None = Query(default=None, description="ISO8601，仅返回开始早于此时刻"),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """SQLite/Memory 后端：直查 calendar_events 表。msgraph 后端不在此实现（请走 agent）。"""
    if settings.calendar_backend not in {"sqlite", "memory"}:
        raise HTTPException(
            400,
            f"only sqlite/memory backend supported here; current={settings.calendar_backend}. "
            "Use agent (calendar_list_events) for msgraph.",
        )

    where = ["1=1"]
    params: list[Any] = []
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if time_min:
        where.append("end_at > ?")
        params.append(time_min)
    if time_max:
        where.append("start_at < ?")
        params.append(time_max)

    sql = (
        f"SELECT id, user_id, title, start_at, end_at, location, description, "
        f"       attendees_json, source, created_at "
        f"FROM calendar_events WHERE {' AND '.join(where)} "
        f"ORDER BY start_at ASC LIMIT ?"
    )
    params.append(limit)

    async with aiosqlite.connect(settings.sqlite_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            rows = await cur.fetchall()

    return {
        "backend": settings.calendar_backend,
        "count": len(rows),
        "events": [_row_to_dict(r) for r in rows],
    }


@router.get("/stats")
async def stats() -> dict:
    """每用户事件总数 + 最早/最晚事件时间，用于一眼判断库里有没有数据。"""
    if settings.calendar_backend not in {"sqlite", "memory"}:
        return {"backend": settings.calendar_backend, "hint": "use /v1/calendar/events via agent"}
    async with aiosqlite.connect(settings.sqlite_path) as db:
        async with db.execute(
            "SELECT user_id, COUNT(*), MIN(start_at), MAX(start_at) "
            "FROM calendar_events GROUP BY user_id"
        ) as cur:
            rows = await cur.fetchall()
    return {
        "backend": settings.calendar_backend,
        "users": [
            {"user_id": r[0], "count": r[1], "earliest": r[2], "latest": r[3]}
            for r in rows
        ],
        "total": sum(r[1] for r in rows),
    }
