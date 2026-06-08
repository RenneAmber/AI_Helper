"""
Microsoft Graph 日历后端单测 —— 用 httpx.MockTransport 拦截所有 Graph 调用，
**不发任何真实网络请求、不触发 OAuth device code flow**。

测试覆盖：
1. create_event → POST /me/events，请求体格式正确（含 attendees / location）
2. create_event(online_meeting=True) → 请求体含 isOnlineMeeting + onlineMeetingProvider
3. list_events → GET /me/calendarView，时间转 UTC
4. find_conflict → 通过 list_events 实现，Python 端做相交
5. propose_slot → 找到第一个无冲突的窗口
6. _clear 默认拒绝（保护真实 Outlook 数据）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.integrations import calendar_msgraph as cmg
from app.integrations.calendar_msgraph import MsGraphCalendarBackend


# ---------- 公共：构造一条 mock 路由 ----------

class _MockState:
    events: list[dict] = []  # 服务器侧"现有事件"列表

    @classmethod
    def reset(cls) -> None:
        cls.events = []


def _make_mock_transport(captured: list[httpx.Request]) -> httpx.MockTransport:
    """返回一个把所有 Graph 请求拍成简易内存日历的 transport。"""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        path = request.url.path
        method = request.method

        if method == "POST" and path == "/v1.0/me/events":
            body = json.loads(request.content.decode("utf-8"))
            ev = {
                "id": f"evt-{len(_MockState.events) + 1}",
                "subject": body.get("subject"),
                "start": body.get("start"),
                "end": body.get("end"),
                "location": body.get("location") or {},
                "attendees": body.get("attendees") or [],
                "bodyPreview": (body.get("body") or {}).get("content", ""),
                "organizer": {"emailAddress": {"address": "me@example.com"}},
                "createdDateTime": "2026-06-08T00:00:00Z",
                "isOnlineMeeting": body.get("isOnlineMeeting", False),
                "onlineMeetingProvider": body.get("onlineMeetingProvider"),
            }
            _MockState.events.append(ev)
            return httpx.Response(201, json=ev)

        if method == "GET" and path == "/v1.0/me/calendarView":
            qs = dict(request.url.params)
            start_min = qs.get("startDateTime")
            end_max = qs.get("endDateTime")
            # 简化：返回区间内事件（与 SqliteCalendarBackend 行为同：end>min, start<max）
            def _in_window(ev: dict) -> bool:
                ev_start = ev["start"]["dateTime"]
                ev_end = ev["end"]["dateTime"]
                if start_min and ev_end <= start_min:
                    return False
                if end_max and ev_start >= end_max:
                    return False
                return True
            items = [e for e in _MockState.events if _in_window(e)]
            return httpx.Response(200, json={"value": items})

        if method == "DELETE" and path.startswith("/v1.0/me/events/"):
            evt_id = path.rsplit("/", 1)[-1]
            _MockState.events = [e for e in _MockState.events if e["id"] != evt_id]
            return httpx.Response(204)

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return httpx.MockTransport(handler)


# ---------- fixtures ----------

@pytest.fixture
def captured_requests() -> list[httpx.Request]:
    return []


@pytest.fixture
def graph_backend(monkeypatch, captured_requests):
    """构造一个内嵌 mock transport 的 backend，并把 token 获取也桩掉。"""
    _MockState.reset()
    backend = MsGraphCalendarBackend()
    # 注入 mock httpx client
    backend._client = httpx.AsyncClient(transport=_make_mock_transport(captured_requests))
    # 桩掉 token 获取，避免触发真实的 device code flow
    async def _fake_token() -> str:
        return "fake-access-token"
    monkeypatch.setattr(cmg._auth, "get_token", _fake_token)
    yield backend


# ---------- 测试 ----------

@pytest.mark.asyncio
async def test_create_event_basic(graph_backend, captured_requests):
    start = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 15, 11, 0, tzinfo=timezone.utc)
    ev = await graph_backend.create_event(
        user_id="u1",
        title="Q3 同步",
        start=start,
        end=end,
        location="Room A",
        attendees=["boss@example.com"],
    )
    assert ev.title == "Q3 同步"
    assert ev.location == "Room A"
    assert "boss@example.com" in ev.attendees
    # 验证请求体
    req = captured_requests[0]
    body = json.loads(req.content)
    assert body["subject"] == "Q3 同步"
    assert body["start"]["timeZone"] == "UTC"
    assert body["end"]["timeZone"] == "UTC"
    assert body["location"]["displayName"] == "Room A"
    assert body["attendees"][0]["emailAddress"]["address"] == "boss@example.com"
    # 默认不开 Teams
    assert "isOnlineMeeting" not in body


@pytest.mark.asyncio
async def test_create_event_online_meeting(graph_backend, captured_requests):
    start = datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)
    end = start + timedelta(minutes=30)
    await graph_backend.create_event(
        user_id="u1",
        title="Quick sync",
        start=start,
        end=end,
        online_meeting=True,
    )
    body = json.loads(captured_requests[0].content)
    assert body["isOnlineMeeting"] is True
    assert body["onlineMeetingProvider"] == "teamsForBusiness"


@pytest.mark.asyncio
async def test_list_events_window(graph_backend, captured_requests):
    # 先建两条事件
    base = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    await graph_backend.create_event(user_id="u1", title="A", start=base, end=base + timedelta(hours=1))
    await graph_backend.create_event(
        user_id="u1", title="B",
        start=base + timedelta(days=2), end=base + timedelta(days=2, hours=1),
    )
    # 查询窗口只覆盖第一条
    events = await graph_backend.list_events(
        user_id="u1",
        time_min=base - timedelta(hours=1),
        time_max=base + timedelta(hours=2),
    )
    titles = sorted(e.title for e in events)
    assert titles == ["A"]


@pytest.mark.asyncio
async def test_find_conflict_detects_overlap(graph_backend):
    base = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    await graph_backend.create_event(user_id="u1", title="既有", start=base, end=base + timedelta(hours=1))
    # 查询 10:30-11:30 应该跟"既有 10:00-11:00"冲突
    conflicts = await graph_backend.find_conflict(
        user_id="u1",
        start=base + timedelta(minutes=30),
        end=base + timedelta(minutes=90),
    )
    assert len(conflicts) == 1
    assert conflicts[0].title == "既有"


@pytest.mark.asyncio
async def test_propose_slot_skips_busy(graph_backend):
    base = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    # 占掉 10-11
    await graph_backend.create_event(user_id="u1", title="busy", start=base, end=base + timedelta(hours=1))
    slot = await graph_backend.propose_slot(
        user_id="u1",
        duration_minutes=30,
        earliest=base,                       # 10:00
        latest=base + timedelta(hours=3),    # 13:00
        granularity_minutes=30,
    )
    # 第一个空档应该是 11:00（10-10:30 和 10:30-11 都跟既有事件冲突）
    assert slot is not None
    assert slot == base + timedelta(hours=1)


@pytest.mark.asyncio
async def test_clear_refuses_without_force(graph_backend):
    with pytest.raises(RuntimeError, match="disabled to protect real Outlook"):
        await graph_backend._clear()
    with pytest.raises(RuntimeError):
        await graph_backend._clear(user_id="u1")  # 普通 user_id 仍被拒
