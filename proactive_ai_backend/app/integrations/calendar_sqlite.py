"""
Aido 日程协同适配器 —— v1：SQLite 持久化。

替代 `calendar_local.InMemoryCalendarBackend` 的进程内存储——
**进程重启不丢、可被多个 worker 共享、与既有 `proactive_ai.db` 共库**。

接口与 v0 完全兼容：`create_event` / `list_events` / `find_conflict` / `propose_slot` / `_clear`。
因此上游 `app/tools.py` 切换 backend 只需改一行 import；现有 3 个 scenario 测试仅需把
`_clear()` 调用作 user_id 范围化即可（避免清掉真实用户数据）。

未来接入 Google Calendar / Microsoft Graph 时，可以把本模块继续保留为**本地缓存层**：
联机 backend 负责双向同步、SQLite 负责离线读和减少 API 调用。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

import aiosqlite

from ..config import settings
from .calendar_local import CalendarEvent, _iso, _parse_iso, event_to_dict  # noqa: F401  (re-export for callers)


_INIT_LOCK = asyncio.Lock()
_INITIALIZED = False


async def _ensure_table() -> None:
    """懒初始化：scenario 测试 / CLI 调用时绕过 app startup 的 init_db() 也能用。"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    async with _INIT_LOCK:
        if _INITIALIZED:
            return
        async with aiosqlite.connect(settings.sqlite_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_events (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    attendees_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT 'manual',
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_calendar_user_start ON calendar_events(user_id, start_at)"
            )
            await db.commit()
        _INITIALIZED = True


def _row_to_event(row: aiosqlite.Row | tuple) -> CalendarEvent:
    # 与 SELECT 的列顺序一一对应
    (
        id_, user_id, title, start_at, end_at,
        location, description, attendees_json, source, created_at,
    ) = row
    try:
        attendees = json.loads(attendees_json) if attendees_json else []
    except json.JSONDecodeError:
        attendees = []
    return CalendarEvent(
        id=id_,
        user_id=user_id,
        title=title,
        start=start_at,
        end=end_at,
        location=location or "",
        description=description or "",
        attendees=list(attendees),
        source=source or "manual",
        created_at=created_at or "",
    )


_SELECT_COLS = (
    "id, user_id, title, start_at, end_at, location, description, attendees_json, source, created_at"
)


class SqliteCalendarBackend:
    """与 `InMemoryCalendarBackend` 完全同构的 SQLite 实现。"""

    # —— 写 ——

    async def create_event(
        self,
        *,
        user_id: str,
        title: str,
        start: datetime,
        end: datetime,
        location: str = "",
        description: str = "",
        attendees: Iterable[str] | None = None,
        source: str = "manual",
    ) -> CalendarEvent:
        if end <= start:
            raise ValueError("end must be after start")
        await _ensure_table()
        ev = CalendarEvent(
            id=str(uuid.uuid4()),
            user_id=user_id,
            title=title.strip() or "(no title)",
            start=_iso(start),
            end=_iso(end),
            location=location.strip(),
            description=description.strip(),
            attendees=list(attendees or []),
            source=source,
            created_at=_iso(datetime.now(timezone.utc)),
        )
        async with aiosqlite.connect(settings.sqlite_path) as db:
            await db.execute(
                f"INSERT INTO calendar_events({_SELECT_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ev.id, ev.user_id, ev.title, ev.start, ev.end,
                    ev.location, ev.description, json.dumps(ev.attendees, ensure_ascii=False),
                    ev.source, ev.created_at,
                ),
            )
            await db.commit()
        return ev

    # —— 读 ——

    async def list_events(
        self,
        *,
        user_id: str,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> list[CalendarEvent]:
        await _ensure_table()
        # 与 in-memory 版的语义保持一致：time_min 过滤 end>time_min，time_max 过滤 start<time_max
        clauses = ["user_id = ?"]
        params: list = [user_id]
        if time_min is not None:
            clauses.append("end_at > ?")
            params.append(_iso(time_min))
        if time_max is not None:
            clauses.append("start_at < ?")
            params.append(_iso(time_max))
        sql = (
            f"SELECT {_SELECT_COLS} FROM calendar_events "
            f"WHERE {' AND '.join(clauses)} ORDER BY start_at ASC"
        )
        async with aiosqlite.connect(settings.sqlite_path) as db:
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in rows]

    async def find_conflict(
        self,
        *,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        # 数据库层直接做区间相交，比拉全表 Python 过滤快得多
        await _ensure_table()
        sql = (
            f"SELECT {_SELECT_COLS} FROM calendar_events "
            f"WHERE user_id = ? AND start_at < ? AND end_at > ? ORDER BY start_at ASC"
        )
        async with aiosqlite.connect(settings.sqlite_path) as db:
            cur = await db.execute(sql, (user_id, _iso(end), _iso(start)))
            rows = await cur.fetchall()
        return [_row_to_event(r) for r in rows]

    async def propose_slot(
        self,
        *,
        user_id: str,
        duration_minutes: int,
        earliest: datetime,
        latest: datetime,
        granularity_minutes: int = 30,
    ) -> datetime | None:
        if duration_minutes <= 0:
            raise ValueError("duration_minutes must be > 0")
        if latest <= earliest:
            return None
        cursor = earliest
        step = timedelta(minutes=granularity_minutes)
        duration = timedelta(minutes=duration_minutes)
        while cursor + duration <= latest:
            window_end = cursor + duration
            conflicts = await self.find_conflict(user_id=user_id, start=cursor, end=window_end)
            if not conflicts:
                return cursor
            cursor += step
        return None

    # —— 测试 / 维护钩子 ——

    async def _clear(self, user_id: str | None = None) -> int:
        """清理事件，**默认仅清指定 user_id**（避免清掉生产数据）。
        传 None 表示清空整张表，仅用于完全隔离的临时数据库。
        返回受影响行数。
        """
        await _ensure_table()
        async with aiosqlite.connect(settings.sqlite_path) as db:
            if user_id is None:
                cur = await db.execute("DELETE FROM calendar_events")
            else:
                cur = await db.execute("DELETE FROM calendar_events WHERE user_id = ?", (user_id,))
            await db.commit()
            return cur.rowcount or 0


# 模块级单例
backend = SqliteCalendarBackend()
