"""
Aido 日程协同适配器 —— v2：Microsoft Graph（真接 Outlook / Teams 日历）。

接口完全兼容 `SqliteCalendarBackend` / `InMemoryCalendarBackend`：
- `create_event(...)` → POST /me/events
- `list_events(...)`  → GET  /me/calendarview?startDateTime=...&endDateTime=...
- `find_conflict(...)` → /me/calendar/getSchedule  （比手动算更准，支持忙闲状态）
  - 简化实现：list_events 后 Python 端做相交，跟其他 backend 行为一致
- `propose_slot(...)` → /me/findMeetingTimes  或 简易扫描；这里走简易扫描保持语义一致

关键设计
--------
1. `user_id` 在 Graph 后端**仅作日志 / 上游兼容**——Graph 永远对当前登录账号操作（/me）。
   多用户场景需要 per-user GraphAuth 实例，留待后续重构。
2. 所有时间统一在 UTC 进出；Graph 字段 `start.timeZone` 固定 "UTC"。
3. 在线会议：当 args 含 `online_meeting=true` 时（个人账号 / Teams license 未启用会忽略），
   设置 `isOnlineMeeting=true, onlineMeetingProvider="teamsForBusiness"`。
4. 错误处理：401 → 提示 token 失效；429 → 简单退避一次；其余直接抛。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from .calendar_local import CalendarEvent, _iso, _parse_iso  # 复用数据类与时间工具
from .ms_auth import auth as _auth

logger = logging.getLogger("calendar_msgraph")

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


def _event_from_graph(item: dict[str, Any]) -> CalendarEvent:
    """Graph event → 我们的 CalendarEvent。"""
    start = item.get("start") or {}
    end = item.get("end") or {}
    attendees_raw = item.get("attendees") or []
    attendees = [
        (a.get("emailAddress") or {}).get("address") or ""
        for a in attendees_raw
    ]
    return CalendarEvent(
        id=item.get("id") or "",
        user_id=(item.get("organizer") or {}).get("emailAddress", {}).get("address", "") or "",
        title=item.get("subject") or "(no title)",
        start=_normalize_graph_dt(start.get("dateTime"), start.get("timeZone") or "UTC"),
        end=_normalize_graph_dt(end.get("dateTime"), end.get("timeZone") or "UTC"),
        location=(item.get("location") or {}).get("displayName") or "",
        description=(item.get("bodyPreview") or "").strip(),
        attendees=[a for a in attendees if a],
        source="msgraph",
        created_at=item.get("createdDateTime") or "",
    )


def _normalize_graph_dt(dt_str: str | None, tz_name: str) -> str:
    """Graph 返回的时间是 "2026-06-15T10:00:00.0000000" + 单独 timezone 字段，
    我们统一拼成带时区的 ISO 字符串并转 UTC。"""
    if not dt_str:
        return ""
    # Graph 微秒位是 7 位，Python fromisoformat 在 3.11+ 能吃；保险起见截到 6 位
    if "." in dt_str:
        head, frac = dt_str.split(".", 1)
        frac = frac[:6]
        dt_str = f"{head}.{frac}"
    try:
        naive = datetime.fromisoformat(dt_str)
    except ValueError:
        return dt_str  # 不认识就原样返回
    # tz_name 大多是 "UTC"；少数请求方传别的需要 zoneinfo，先 best-effort
    if naive.tzinfo is None:
        if tz_name.upper() == "UTC":
            naive = naive.replace(tzinfo=timezone.utc)
        else:
            # 没装 zoneinfo / IANA 名不规范都退到 UTC，不让流程死
            naive = naive.replace(tzinfo=timezone.utc)
    return naive.astimezone(timezone.utc).isoformat()


class MsGraphCalendarBackend:
    """直接对接 Microsoft Graph 的日历后端（当前登录账号 = /me）。"""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _http(self) -> httpx.AsyncClient:
        # 懒初始化 + 复用连接池
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
        return self._client

    async def _headers(self) -> dict[str, str]:
        token = await _auth.get_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Prefer": 'outlook.timezone="UTC"',
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{_GRAPH_BASE}{path}"
        client = await self._http()
        headers = await self._headers()
        # merge headers
        h = dict(headers)
        h.update(kwargs.pop("headers", {}) or {})
        resp = await client.request(method, url, headers=h, **kwargs)
        if resp.status_code == 429:
            # 退避一次再试（Graph 偶有节流）
            retry_after = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(min(retry_after, 5.0))
            resp = await client.request(method, url, headers=h, **kwargs)
        if resp.status_code >= 400:
            body = resp.text[:1000]
            logger.warning(
                "graph_error",
                extra={"method": method, "path": path, "status": resp.status_code, "body": body},
            )
            resp.raise_for_status()
        return resp

    # ---------- 写 ----------

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
        online_meeting: bool = False,
    ) -> CalendarEvent:
        if end <= start:
            raise ValueError("end must be after start")
        attendees = list(attendees or [])
        body: dict[str, Any] = {
            "subject": title.strip() or "(no title)",
            "body": {"contentType": "text", "content": description or ""},
            "start": {"dateTime": _iso(start), "timeZone": "UTC"},
            "end": {"dateTime": _iso(end), "timeZone": "UTC"},
        }
        if location:
            body["location"] = {"displayName": location}
        if attendees:
            body["attendees"] = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in attendees
            ]
        if online_meeting:
            body["isOnlineMeeting"] = True
            body["onlineMeetingProvider"] = "teamsForBusiness"

        resp = await self._request("POST", "/me/events", json=body)
        return _event_from_graph(resp.json())

    # ---------- 读 ----------

    async def list_events(
        self,
        *,
        user_id: str,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
    ) -> list[CalendarEvent]:
        # 默认窗口：今天到 +30 天，避免拉全量
        now = datetime.now(timezone.utc)
        start = time_min or now
        end = time_max or (now + timedelta(days=30))
        params = {
            "startDateTime": _iso(start),
            "endDateTime": _iso(end),
            "$orderby": "start/dateTime",
            "$top": "100",
        }
        resp = await self._request("GET", "/me/calendarView", params=params)
        items = (resp.json().get("value") or [])
        return [_event_from_graph(it) for it in items]

    async def find_conflict(
        self,
        *,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> list[CalendarEvent]:
        # 直接在窗口内拉事件并在 Python 端相交，跟其他 backend 行为完全一致
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

    # ---------- 维护 ----------

    async def _clear(self, user_id: str | None = None) -> int:
        """⚠ Graph 后端**默认拒绝**清空——会真删除你 Outlook 日历里的事件。
        测试 / 演示场景请改用 sqlite / memory 后端。
        显式传入 `user_id="__force__"` 才会执行（仅供高级运维）。
        """
        if user_id != "__force__":
            raise RuntimeError(
                "MsGraphCalendarBackend._clear is disabled to protect real Outlook data. "
                "Pass user_id='__force__' if you really mean it."
            )
        deleted = 0
        # 删 30 天内的全部事件（最常见的演示清理用例）
        events = await self.list_events(
            user_id="*",
            time_min=datetime.now(timezone.utc) - timedelta(days=30),
            time_max=datetime.now(timezone.utc) + timedelta(days=365),
        )
        for ev in events:
            try:
                await self._request("DELETE", f"/me/events/{ev.id}")
                deleted += 1
            except Exception as exc:
                logger.warning("graph_delete_failed", extra={"id": ev.id, "err": str(exc)})
        return deleted

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# 模块级单例（与 sqlite / local 后端一致的导出形态）
backend = MsGraphCalendarBackend()
