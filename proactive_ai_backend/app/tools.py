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

from .integrations.email_accounts import get_default_account  # noqa: E402
from .integrations.email_imap import EmailClient  # noqa: E402


def _email_client() -> EmailClient:
    return EmailClient(get_default_account())


async def email_list_inbox(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {limit?:int=10, mailbox?:str='INBOX'}"""
    client = _email_client()
    items = await client.list_inbox(limit=int(args.get("limit", 10)), mailbox=args.get("mailbox", "INBOX"))
    return {"emails": items}


async def email_read(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client()
    msg = await client.fetch(uid=uid, mailbox=args.get("mailbox", "INBOX"))
    return {"email": msg}


async def email_search(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {q:str, limit?:int=10, mailbox?:str='INBOX'}"""
    q = str(args.get("q") or "").strip()
    if not q:
        return {"error": "q required"}
    client = _email_client()
    items = await client.search(query=q, limit=int(args.get("limit", 10)), mailbox=args.get("mailbox", "INBOX"))
    return {"emails": items}


async def email_send(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {to:str|list, subject:str, body:str, cc?:str|list, html?:str}"""
    to = args.get("to") or ctx.get("to")
    subject = args.get("subject") or ctx.get("subject") or ""
    body = args.get("body") or ctx.get("body") or ""
    if not to or not subject:
        return {"error": "to and subject required"}
    client = _email_client()
    msg_id = await client.send(to=to, subject=subject, body=body, cc=args.get("cc"), html=args.get("html"))
    return {"sent_message_id": msg_id}


async def email_mark_seen(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, seen?:bool=True, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client()
    return await client.mark_seen(uid=uid, seen=bool(args.get("seen", True)), mailbox=args.get("mailbox", "INBOX"))


async def email_mark_flag(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, flagged?:bool=True, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client()
    return await client.mark_flagged(uid=uid, flagged=bool(args.get("flagged", True)), mailbox=args.get("mailbox", "INBOX"))


async def email_move(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, dest_mailbox:str, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    dest = str(args.get("dest_mailbox") or "")
    if not uid or not dest:
        return {"error": "uid and dest_mailbox required"}
    client = _email_client()
    return await client.move(uid=uid, dest_mailbox=dest, mailbox=args.get("mailbox", "INBOX"))


async def email_delete(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, mailbox?:str='INBOX', hard?:bool=False}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client()
    return await client.delete(uid=uid, mailbox=args.get("mailbox", "INBOX"), hard=bool(args.get("hard", False)))


async def email_list_attachments(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    if not uid:
        return {"error": "uid required"}
    client = _email_client()
    items = await client.list_attachments(uid=uid, mailbox=args.get("mailbox", "INBOX"))
    return {"uid": uid, "attachments": items, "count": len(items)}


async def email_reply(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, body:str, reply_all?:bool=False, extra_cc?:list[str], include_quote?:bool=True, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    body = args.get("body") or ""
    if not uid:
        return {"error": "uid required"}
    client = _email_client()
    return await client.reply(
        uid=uid,
        body=body,
        mailbox=args.get("mailbox", "INBOX"),
        reply_all=bool(args.get("reply_all", False)),
        extra_cc=args.get("extra_cc"),
        include_quote=bool(args.get("include_quote", True)),
    )


async def email_forward(args: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
    """args: {uid:str, to:str|list, cc?:str|list, body_prefix?:str, include_quote?:bool=True, mailbox?:str='INBOX'}"""
    uid = str(args.get("uid") or "")
    to = args.get("to")
    if not uid or not to:
        return {"error": "uid and to required"}
    client = _email_client()
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
