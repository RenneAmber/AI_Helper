from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

ToolFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> ToolFn | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())


registry = ToolRegistry()


# ----- Built-in demo tools -------------------------------------------------- #

async def fetch_notes(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(0.02)
    week = args.get("week", "current")
    return {
        "notes": [
            f"[{week}] standup: project Alpha on track",
            f"[{week}] design review: pipeline batching v2",
            f"[{week}] postmortem: provider timeout incident",
        ]
    }


async def summarize(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(0.02)
    notes = ctx.get("notes") or args.get("notes") or []
    style = args.get("style", "bullet")
    bullets = [f"- {n}" for n in notes] if style == "bullet" else notes
    return {"summary": "\n".join(bullets)}


async def extract_actions(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    await asyncio.sleep(0.02)
    summary: str = ctx.get("summary", "")
    actions = []
    for line in summary.splitlines():
        if "incident" in line or "review" in line:
            actions.append({"owner": "backend", "task": line.strip("- ")})
    return {"actions": actions}


async def echo(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    return {"echo": args}


registry.register("fetch_notes", fetch_notes)
registry.register("summarize", summarize)
registry.register("extract_actions", extract_actions)
registry.register("echo", echo)


# ----- Email tools（真实 IMAP/SMTP）------------------------------------------
# 工具协议：args 是本次 step 的参数；ctx 是跨 step 共享上下文。
# 工具返回的 dict 会 merge 到 ctx，供下一步使用（链式编排）。

from .integrations.email_factory import get_email_client  # noqa: E402


def _email_client(args: dict[str, Any] | None = None, ctx: dict[str, Any] | None = None):
    """按 args.account → ctx.email_account → 默认 的优先级挑账号。"""
    name = None
    if args:
        name = args.get("account") or args.get("account_name")
    if not name and ctx:
        name = ctx.get("email_account")
    return get_email_client(name)


async def email_list_inbox(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {limit?:int=10, mailbox?:str='INBOX', account?:str}"""
    client = _email_client(args, ctx)
    items = await client.list_inbox(limit=int(args.get("limit", 10)), mailbox=args.get("mailbox", "INBOX"))
    return {"emails": items}


async def email_read(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client(args, ctx)
    msg = await client.fetch(uid=uid, mailbox=args.get("mailbox", "INBOX"))
    return {"email": msg}


async def email_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {q:str, limit?:int=10, mailbox?:str='INBOX', account?:str}"""
    q = str(args.get("q") or "").strip()
    if not q:
        return {"error": "q required"}
    client = _email_client(args, ctx)
    items = await client.search(query=q, limit=int(args.get("limit", 10)), mailbox=args.get("mailbox", "INBOX"))
    return {"emails": items}


async def email_send(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {to:str|list, subject:str, body:str, cc?:str|list, html?:str, account?:str}"""
    to = args.get("to") or ctx.get("to")
    subject = args.get("subject") or ctx.get("subject") or ""
    body = args.get("body") or ctx.get("body") or ""
    if not to or not subject:
        return {"error": "to and subject required"}
    client = _email_client(args, ctx)
    msg_id = await client.send(to=to, subject=subject, body=body, cc=args.get("cc"), html=args.get("html"))
    return {"sent_message_id": msg_id}


async def email_mark_seen(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, seen?:bool=True, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client(args, ctx)
    return await client.mark_seen(uid=uid, seen=bool(args.get("seen", True)), mailbox=args.get("mailbox", "INBOX"))


async def email_mark_flag(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, flagged?:bool=True, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client(args, ctx)
    return await client.mark_flagged(uid=uid, flagged=bool(args.get("flagged", True)), mailbox=args.get("mailbox", "INBOX"))


async def email_move(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, dest_mailbox:str, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    dest = str(args.get("dest_mailbox") or "")
    if not uid or not dest:
        return {"error": "uid and dest_mailbox required"}
    client = _email_client(args, ctx)
    return await client.move(uid=uid, dest_mailbox=dest, mailbox=args.get("mailbox", "INBOX"))


async def email_delete(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, mailbox?:str='INBOX', hard?:bool=False, account?:str}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client(args, ctx)
    return await client.delete(uid=uid, mailbox=args.get("mailbox", "INBOX"), hard=bool(args.get("hard", False)))


async def email_list_attachments(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client(args, ctx)
    items = await client.list_attachments(uid=uid, mailbox=args.get("mailbox", "INBOX"))
    return {"uid": uid, "attachments": items, "count": len(items)}


async def email_reply(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, body:str, reply_all?:bool=False, extra_cc?:list[str], include_quote?:bool=True, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    body = args.get("body") or ""
    if not uid:
        return {"error": "uid required"}
    client = _email_client(args, ctx)
    return await client.reply(
        uid=uid,
        body=body,
        mailbox=args.get("mailbox", "INBOX"),
        reply_all=bool(args.get("reply_all", False)),
        extra_cc=args.get("extra_cc"),
        include_quote=bool(args.get("include_quote", True)),
    )


async def email_forward(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, to:str|list, cc?:str|list, body_prefix?:str, include_quote?:bool=True, mailbox?:str='INBOX', account?:str}"""
    uid = str(args.get("uid") or "")
    to = args.get("to")
    if not uid or not to:
        return {"error": "uid and to required"}
    client = _email_client(args, ctx)
    return await client.forward(
        uid=uid,
        to=to,
        mailbox=args.get("mailbox", "INBOX"),
        body_prefix=args.get("body_prefix", ""),
        cc=args.get("cc"),
        include_quote=bool(args.get("include_quote", True)),
    )


registry.register("email.list_inbox", email_list_inbox)
registry.register("email.read", email_read)
registry.register("email.search", email_search)
registry.register("email.send", email_send)
registry.register("email.mark_seen", email_mark_seen)
registry.register("email.mark_flag", email_mark_flag)
registry.register("email.move", email_move)
registry.register("email.delete", email_delete)
registry.register("email.list_attachments", email_list_attachments)
registry.register("email.reply", email_reply)
registry.register("email.forward", email_forward)


# ----- Calendar tools（v0：进程内存储；OAuth 真实适配器接入后只需替换 backend）----

from datetime import datetime, timedelta, timezone  # noqa: E402

# Calendar backend：由 calendar_factory 根据 CALENDAR_BACKEND 环境变量挑选
# memory / sqlite / msgraph 三者接口同构，业务代码全部走这个门面。
from .integrations.calendar_factory import backend as calendar_backend  # noqa: E402
from .integrations.calendar_local import event_to_dict  # noqa: E402


def _parse_iso_arg(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError(f"{field} must be ISO8601 string")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def calendar_list_events(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {user_id:str, time_min?:iso, time_max?:iso}"""
    user_id = str(args.get("user_id") or ctx.get("user_id") or "")
    if not user_id:
        return {"error": "user_id required"}
    time_min = _parse_iso_arg(args["time_min"], field="time_min") if args.get("time_min") else None
    time_max = _parse_iso_arg(args["time_max"], field="time_max") if args.get("time_max") else None
    events = await calendar_backend.list_events(user_id=user_id, time_min=time_min, time_max=time_max)
    return {"events": [event_to_dict(e) for e in events]}


async def calendar_create_event(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {user_id, title, start(iso), end(iso) | duration_minutes, location?, description?, attendees?:list, source?:str}"""
    user_id = str(args.get("user_id") or ctx.get("user_id") or "")
    title = str(args.get("title") or "").strip()
    if not user_id or not title:
        return {"error": "user_id and title required"}
    if not args.get("start"):
        return {"error": "start required (ISO8601)"}
    start = _parse_iso_arg(args["start"], field="start")
    if args.get("end"):
        end = _parse_iso_arg(args["end"], field="end")
    elif args.get("duration_minutes"):
        end = start + timedelta(minutes=int(args["duration_minutes"]))
    else:
        return {"error": "either end or duration_minutes required"}
    try:
        ev = await calendar_backend.create_event(
            user_id=user_id,
            title=title,
            start=start,
            end=end,
            location=str(args.get("location") or ""),
            description=str(args.get("description") or ""),
            attendees=args.get("attendees") or [],
            source=str(args.get("source") or "manual"),
            **({"online_meeting": bool(args["online_meeting"])} if "online_meeting" in args else {}),
        )
    except TypeError:
        # 后端不支持 online_meeting（sqlite/memory）→ 静默回退
        ev = await calendar_backend.create_event(
            user_id=user_id,
            title=title,
            start=start,
            end=end,
            location=str(args.get("location") or ""),
            description=str(args.get("description") or ""),
            attendees=args.get("attendees") or [],
            source=str(args.get("source") or "manual"),
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"event": event_to_dict(ev)}


async def calendar_find_conflict(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {user_id, start(iso), end(iso)}"""
    user_id = str(args.get("user_id") or ctx.get("user_id") or "")
    if not user_id or not args.get("start") or not args.get("end"):
        return {"error": "user_id, start, end required"}
    start = _parse_iso_arg(args["start"], field="start")
    end = _parse_iso_arg(args["end"], field="end")
    conflicts = await calendar_backend.find_conflict(user_id=user_id, start=start, end=end)
    return {"conflicts": [event_to_dict(e) for e in conflicts], "has_conflict": bool(conflicts)}


async def calendar_propose_slot(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {user_id, duration_minutes, earliest(iso), latest(iso), granularity_minutes?}"""
    user_id = str(args.get("user_id") or ctx.get("user_id") or "")
    if not user_id or not args.get("earliest") or not args.get("latest"):
        return {"error": "user_id, earliest, latest required"}
    earliest = _parse_iso_arg(args["earliest"], field="earliest")
    latest = _parse_iso_arg(args["latest"], field="latest")
    slot = await calendar_backend.propose_slot(
        user_id=user_id,
        duration_minutes=int(args.get("duration_minutes") or 30),
        earliest=earliest,
        latest=latest,
        granularity_minutes=int(args.get("granularity_minutes") or 30),
    )
    return {"slot_start": slot.isoformat() if slot else None}


registry.register("calendar.list_events", calendar_list_events)
registry.register("calendar.create_event", calendar_create_event)
registry.register("calendar.find_conflict", calendar_find_conflict)
registry.register("calendar.propose_slot", calendar_propose_slot)
