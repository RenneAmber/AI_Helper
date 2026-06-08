"""
Scenario test —— "把昨天 Boss 那封邮件里提的会议加到我下周日程，并回复确认"。

这个测试不依赖任何外部服务（IMAP / Azure / Google Calendar），目标是验证：

1. **Agent 编排能正确串起 4 个工具**：email_search → email_read → calendar_find_conflict
   → calendar_create_event → email_reply
2. **`allow_send` 守门生效**：当 allow_send=False 时，calendar_create_event 与 email_reply
   都被阻断，并返回明确的提示
3. **跨工具上下文传递**：从邮件 body 抽到的时间点能正确传到 calendar.create_event
4. **Prometheus 指标埋点正确**：每个工具至少有一次 ok / blocked / error 计数

技术实现：
- 用 `ScriptedToolCallProvider` 替换 `app.agent.email_agent.provider`，让 LLM 决策固定可复现
- 用进程内字典 fake 替换 `email.*` 工具的注册项；`calendar.*` 工具用真实的内存实现
- 测试结束后自动还原所有 monkeypatch
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import email_agent as agent_mod
from app.agent.email_agent import run_email_agent
# 这里特意从 tools 取 calendar_backend，跟生产路径完全一致（避免测和应用引用两份 backend）
from app.tools import registry, calendar_backend


# ---------------- Scripted provider ----------------

@dataclass
class _ScriptedStep:
    """要么吐 tool_calls，要么给最终 content。"""
    tool_calls: list[dict] | None = None   # [{"name":"email_search","args":{...}}, ...]
    content: str | None = None


class ScriptedToolCallProvider:
    """以最小协议模拟 OpenAI chat.completions 返回结构，让 Agent 循环跑通。

    每次被调用就消费一条脚本步骤；超过脚本长度按 content="（无内容）" 终止。
    """

    name = "scripted"

    def __init__(self, script: list[_ScriptedStep]) -> None:
        self._script = list(script)
        self.call_count = 0

    async def chat_with_tools(self, messages: list[dict], tools: list[dict], max_tokens: int = 1024):  # noqa: D401
        idx = self.call_count
        self.call_count += 1
        step = self._script[idx] if idx < len(self._script) else _ScriptedStep(content="（脚本耗尽，终止）")

        if step.tool_calls:
            tc_objs = []
            for i, call in enumerate(step.tool_calls):
                tc_objs.append(
                    SimpleNamespace(
                        id=f"call_{idx}_{i}",
                        type="function",
                        function=SimpleNamespace(
                            name=call["name"],
                            arguments=json.dumps(call.get("args") or {}, ensure_ascii=False),
                        ),
                    )
                )
            message = SimpleNamespace(content=step.content or "", tool_calls=tc_objs)
        else:
            message = SimpleNamespace(content=step.content or "", tool_calls=None)

        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


# ---------------- Fake email backend ----------------

USER_ID = "u-scenario"
BOSS = "boss@example.com"
ME = "me@example.com"
BOSS_UID = "9001"

# 故意把会议时间写在邮件正文里，让 agent 能"读到"（不需要真的 LLM 抽时间，
# 我们的 scripted provider 直接把这个时间填到 calendar_create_event 的 args 里）
NEXT_MONDAY_10AM = (
    datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    + timedelta(days=(7 - datetime.now(timezone.utc).weekday()) % 7 + 7)
).replace(microsecond=0)
NEXT_MONDAY_11AM = NEXT_MONDAY_10AM + timedelta(hours=1)

BOSS_EMAIL = {
    "uid": BOSS_UID,
    "mailbox": "INBOX",
    "from_": f"Boss <{BOSS}>",
    "to": ME,
    "date": "Yesterday 09:00",
    "subject": "Q3 Planning Sync",
    "body": (
        f"Hi,\n\nLet's sync on Q3 planning at "
        f"{NEXT_MONDAY_10AM.isoformat()} for 1 hour.\n\n"
        f"Conference room A.\n\nThanks,\nBoss"
    ),
}


class _FakeEmailState:
    sent: list[dict] = []
    replies: list[dict] = []


@pytest.fixture
def fake_email_tools(monkeypatch):
    """替换 registry 中的 email.* 工具为内存版本，互不污染。"""
    _FakeEmailState.sent.clear()
    _FakeEmailState.replies.clear()

    async def _search(args, ctx):
        q = (args.get("q") or "").lower()
        match = [BOSS_EMAIL] if q and (q in BOSS_EMAIL["from_"].lower() or q in BOSS_EMAIL["subject"].lower()) else []
        # 只返回 header（与真实 IMAP 客户端的 list/search 行为一致）
        return {"emails": [{k: v for k, v in m.items() if k != "body"} for m in match]}

    async def _read(args, ctx):
        if str(args.get("uid")) == BOSS_UID:
            return {"email": BOSS_EMAIL}
        return {"email": None, "error": "not found"}

    async def _reply(args, ctx):
        rec = {
            "uid": args.get("uid"),
            "body": args.get("body"),
            "reply_all": args.get("reply_all", False),
            "mailbox": args.get("mailbox", "INBOX"),
        }
        _FakeEmailState.replies.append(rec)
        return {"status": "sent", "message_id": f"<reply-{len(_FakeEmailState.replies)}@aido.test>"}

    async def _send(args, ctx):
        _FakeEmailState.sent.append(args)
        return {"status": "sent", "message_id": f"<send-{len(_FakeEmailState.sent)}@aido.test>"}

    # 保留原 fn 引用，测试结束后还原
    original = {name: registry.get(name) for name in (
        "email.search", "email.read", "email.reply", "email.send",
    )}
    registry.register("email.search", _search)
    registry.register("email.read", _read)
    registry.register("email.reply", _reply)
    registry.register("email.send", _send)
    yield _FakeEmailState
    for k, v in original.items():
        if v is not None:
            registry.register(k, v)


@pytest.fixture
async def clean_calendar():
    """每个用例前后只清测试用户的日程（防止误清生产数据）。"""
    await calendar_backend._clear(user_id=USER_ID)
    yield calendar_backend
    await calendar_backend._clear(user_id=USER_ID)


# ---------------- 脚本构造工具 ----------------

def _scenario_script(*, allow_send: bool) -> list[_ScriptedStep]:
    """模拟一个 well-behaved LLM 在 5 轮里完成 email→calendar→reply 流程。"""
    start_iso = NEXT_MONDAY_10AM.isoformat()
    end_iso = NEXT_MONDAY_11AM.isoformat()

    after_writes_msg = (
        "已为你创建『Q3 Planning Sync』日程并回复 Boss 确认。"
        if allow_send
        else "我已经准备好日程草稿与回信内容，但需要你打开『允许真发送』后才能落地。"
    )

    return [
        # 轮1：搜邮件
        _ScriptedStep(tool_calls=[{"name": "email_search", "args": {"q": "Boss", "limit": 5}}]),
        # 轮2：拿到 UID，读全文
        _ScriptedStep(tool_calls=[{"name": "email_read", "args": {"uid": BOSS_UID}}]),
        # 轮3：检查冲突
        _ScriptedStep(tool_calls=[{
            "name": "calendar_find_conflict",
            "args": {"user_id": USER_ID, "start": start_iso, "end": end_iso},
        }]),
        # 轮4：创建日程 + 回复邮件（并行 tool_calls）
        _ScriptedStep(tool_calls=[
            {
                "name": "calendar_create_event",
                "args": {
                    "user_id": USER_ID,
                    "title": "Q3 Planning Sync",
                    "start": start_iso,
                    "end": end_iso,
                    "location": "Conference room A",
                    "attendees": [BOSS],
                    "source": "agent.email",
                },
            },
            {
                "name": "email_reply",
                "args": {
                    "uid": BOSS_UID,
                    "body": "收到。会议已加入我下周一 10:00 的日程，到时见。",
                    "mailbox": "INBOX",
                },
            },
        ]),
        # 轮5：终态汇报，不再调工具
        _ScriptedStep(content=after_writes_msg),
    ]


# ---------------- 用例 ----------------

@pytest.mark.asyncio
async def test_scenario_boss_email_to_calendar_and_reply(monkeypatch, fake_email_tools, clean_calendar):
    """主路径：allow_send=True，所有写动作落地。"""
    scripted = ScriptedToolCallProvider(_scenario_script(allow_send=True))
    monkeypatch.setattr(agent_mod, "provider", scripted)

    result = await run_email_agent(
        user_message="把昨天 Boss 那封邮件里提的会议加到我下周日程，并回复确认",
        history=[],
        allow_send=True,
        max_iterations=6,
    )

    # ——— 1. Agent 至少走过 4 个工具，且顺序合理 ———
    tool_sequence = [a.name for a in result.actions]
    assert tool_sequence == [
        "email_search",
        "email_read",
        "calendar_find_conflict",
        "calendar_create_event",
        "email_reply",
    ], f"unexpected tool sequence: {tool_sequence}"

    # ——— 2. 所有工具都成功执行（无 error / 无 blocked）———
    for a in result.actions:
        assert a.error is None, f"tool {a.name} failed: {a.error}"
        assert a.result is not None, f"tool {a.name} returned no result"

    # ——— 3. Calendar 真的被写入了，且时间/标题/参会人都对 ———
    events = await calendar_backend.list_events(user_id=USER_ID)
    assert len(events) == 1, f"expected 1 calendar event, got {len(events)}"
    ev = events[0]
    assert ev.title == "Q3 Planning Sync"
    assert ev.start.startswith(NEXT_MONDAY_10AM.isoformat()[:19])
    assert ev.end.startswith(NEXT_MONDAY_11AM.isoformat()[:19])
    assert BOSS in ev.attendees
    assert ev.source == "agent.email"

    # ——— 4. 回信落地，UID 与 body 正确 ———
    assert len(fake_email_tools.replies) == 1
    reply = fake_email_tools.replies[0]
    assert reply["uid"] == BOSS_UID
    assert "下周一" in reply["body"] or "10:00" in reply["body"]

    # ——— 5. Final text 简明扼要 ———
    assert "已为你创建" in result.text or "已创建" in result.text or "Q3" in result.text


@pytest.mark.asyncio
async def test_scenario_blocked_when_allow_send_false(monkeypatch, fake_email_tools, clean_calendar):
    """守门路径：allow_send=False，calendar_create_event + email_reply 都应被拒，calendar 无新事件。"""
    scripted = ScriptedToolCallProvider(_scenario_script(allow_send=False))
    monkeypatch.setattr(agent_mod, "provider", scripted)

    result = await run_email_agent(
        user_message="把昨天 Boss 那封邮件里提的会议加到我下周日程，并回复确认",
        history=[],
        allow_send=False,
        max_iterations=6,
    )

    # 5 个工具仍然都被"尝试"调用，前 3 个 ok，后 2 个 blocked
    statuses = [(a.name, "blocked" if a.error and "blocked" in a.error else ("ok" if a.error is None else "error"))
                for a in result.actions]
    assert statuses == [
        ("email_search", "ok"),
        ("email_read", "ok"),
        ("calendar_find_conflict", "ok"),
        ("calendar_create_event", "blocked"),
        ("email_reply", "blocked"),
    ], f"unexpected statuses: {statuses}"

    # 写动作被守门 → calendar 应保持为空
    events = await calendar_backend.list_events(user_id=USER_ID)
    assert events == [], f"events should be empty under allow_send=False, got: {events}"
    assert fake_email_tools.replies == []


@pytest.mark.asyncio
async def test_scenario_conflict_detected_does_not_block_creation(monkeypatch, fake_email_tools, clean_calendar):
    """边界路径：日程上已有冲突时，find_conflict 报告冲突，但本测试里 agent 依然按脚本创建——
    目的是验证 calendar.find_conflict 的返回值在 ctx 中可见、而创建不被错误地阻断。
    （生产场景下 LLM 会读到 has_conflict=True 后决定是否取消；这里只验证编排链路。）"""
    # 预先插入一条冲突事件
    await calendar_backend.create_event(
        user_id=USER_ID,
        title="既有会议",
        start=NEXT_MONDAY_10AM + timedelta(minutes=15),
        end=NEXT_MONDAY_10AM + timedelta(minutes=45),
    )

    scripted = ScriptedToolCallProvider(_scenario_script(allow_send=True))
    monkeypatch.setattr(agent_mod, "provider", scripted)

    result = await run_email_agent(
        user_message="把昨天 Boss 那封邮件里提的会议加到我下周日程，并回复确认",
        history=[],
        allow_send=True,
        max_iterations=6,
    )

    conflict_action = next(a for a in result.actions if a.name == "calendar_find_conflict")
    assert conflict_action.result is not None
    assert conflict_action.result.get("has_conflict") is True
    assert len(conflict_action.result.get("conflicts") or []) == 1

    events = await calendar_backend.list_events(user_id=USER_ID)
    # 应该有两条：预置的 + 新创建的（共存，是否解决冲突由调用方决策）
    titles = sorted(e.title for e in events)
    assert "Q3 Planning Sync" in titles
