"""
Aido 日程协同适配器 —— v0：进程内存储。

定位：
- Calendar 是 Aido 协同工具矩阵的第二位公民（Email 之后）
- 真实 Google Calendar / Microsoft Graph 适配器尚未接入；先用一个 **结构完整、行为可预测** 的内存实现，
  把上层 Agent / Workflow / scenario 测试串通。等 OAuth 落地后，本模块只需把 `_BACKEND` 切换为
  真实客户端、保留同样的协程签名即可（零改动上游）。

线程模型：
- FastAPI 单进程内多协程并发；asyncio.Lock 保护事件字典
- 进程重启即清空（这是 v0 故意为之；持久化在 SQLite 适配器里做）
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


@dataclass
class CalendarEvent:
    id: str
    user_id: str
    title: str
    start: str                # ISO8601 (UTC)
    end: str                  # ISO8601 (UTC)
    location: str = ""
    description: str = ""
    attendees: list[str] = field(default_factory=list)
    source: str = "manual"    # 'manual' | 'agent.email' | etc.
    created_at: str = ""


class InMemoryCalendarBackend:
    """协议是真实 Calendar provider 的最小子集：list / create / find_conflict / propose_slot。"""

    def __init__(self) -> None:
        self._events: dict[str, CalendarEvent] = {}
        self._lock = asyncio.Lock()

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
        async with self._lock:
            self._events[ev.id] = ev
        return ev

    # —— 读 ——

    async def list_events(
        self,
        *,
        user_id: str,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> list[CalendarEvent]:
        async with self._lock:
            items = [e for e in self._events.values() if e.user_id == user_id]
        if time_min is not None:
            items = [e for e in items if _parse_iso(e.end) > time_min]
        if time_max is not None:
            items = [e for e in items if _parse_iso(e.start) < time_max]
        items.sort(key=lambda e: e.start)
        return items

    async def find_conflict(
        self,
        *,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        """返回与 [start, end) 时间窗有重叠的事件列表。"""
        events = await self.list_events(user_id=user_id, time_min=start, time_max=end)
        return [
            e for e in events
            if _parse_iso(e.start) < end and _parse_iso(e.end) > start
        ]

    async def propose_slot(
        self,
        *,
        user_id: str,
        duration_minutes: int,
        earliest: datetime,
        latest: datetime,
        granularity_minutes: int = 30,
    ) -> datetime | None:
        """在 [earliest, latest] 区间里找第一个能放得下 `duration_minutes` 的空档。
        简单算法：按 granularity 步进，遇到无冲突的 slot 即返回。
        """
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

    # —— 测试钩子 ——

    async def _clear(self) -> None:
        async with self._lock:
            self._events.clear()


# 模块级单例（同进程共享；scenario 测试可直接通过 `backend._clear()` 重置）。
backend = InMemoryCalendarBackend()


# ---------- 时间工具 ----------

def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: str) -> datetime:
    # Python 3.11+ 支持 'Z' 后缀
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def event_to_dict(ev: CalendarEvent) -> dict[str, Any]:
    return asdict(ev)
