"""
Aido 邮件协同适配器 —— Microsoft Graph（OAuth2）后端。

接口与 `email_imap.EmailClient` 完全同构，可作为它的 drop-in 替换：
    list_inbox / list_folders / fetch / search / send /
    list_attachments / fetch_attachment / reply / forward /
    mark_seen / mark_flagged / update_flags / move / delete

实现要点
--------
1. 共享 `ms_auth.GraphAuth` 单例：与 Calendar 后端使用同一份 device-code token；
   首次使用 Calendar 已登录过，**不会再次弹 device flow**（token 缓存命中）。
2. `uid` 映射为 Graph `message.id`（不透明字符串）；调用方语义不变。
3. `mailbox` 映射为 Graph 文件夹 displayName，自动解析为 folderId；
   也接受 well-known 名（INBOX / Drafts / SentItems / DeletedItems / JunkEmail / Archive）。
4. 写邮件统一走 `POST /me/sendMail`，附件以 fileAttachment 形式 inline base64。
5. 发送 / 回复 / 转发后无服务端返回 message-id（sendMail 是 202 fire-and-forget），
   返回合成 `sent:<isoformat>` 占位，与 IMAP backend 的语义保持兼容（路由层只用作日志）。
6. 单 host = graph.microsoft.com，复用 httpx.AsyncClient 连接池。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from .email_imap import (
    AttachmentMeta,
    EmailFull,
    EmailMeta,
    OutgoingAttachment,
    _html_to_text,
)
from .ms_auth import auth as _auth

logger = logging.getLogger("email_msgraph")

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=8.0)

# Graph 内置 well-known 文件夹名（大小写不敏感）→ folderId 别名
_WELLKNOWN_FOLDERS: dict[str, str] = {
    "inbox": "inbox",
    "drafts": "drafts",
    "sentitems": "sentitems",
    "sent": "sentitems",
    "deleteditems": "deleteditems",
    "trash": "deleteditems",
    "junkemail": "junkemail",
    "junk": "junkemail",
    "archive": "archive",
    "outbox": "outbox",
}


def _addr_str(addr_obj: dict[str, Any] | None) -> str:
    """Graph emailAddress 字段 → 'Name <addr@x>' 字符串。"""
    if not addr_obj:
        return ""
    inner = addr_obj.get("emailAddress") or addr_obj
    name = (inner.get("name") or "").strip()
    addr = (inner.get("address") or "").strip()
    if name and addr and name.lower() != addr.lower():
        return f'"{name}" <{addr}>'
    return addr or name


def _addr_list_str(items: list[dict[str, Any]] | None) -> str:
    return ", ".join(filter(None, (_addr_str(x) for x in items or [])))


def _meta_from_graph(item: dict[str, Any]) -> EmailMeta:
    return EmailMeta(
        uid=item.get("id") or "",
        subject=item.get("subject") or "",
        from_=_addr_str(item.get("from")) or _addr_str(item.get("sender")),
        to=_addr_list_str(item.get("toRecipients")),
        date=item.get("receivedDateTime") or item.get("sentDateTime") or "",
        snippet=(item.get("bodyPreview") or "")[:160],
    )


def _full_from_graph(item: dict[str, Any]) -> EmailFull:
    body_obj = item.get("body") or {}
    content_type = (body_obj.get("contentType") or "text").lower()
    raw = body_obj.get("content") or ""
    if content_type == "html":
        body_text = _html_to_text(raw)
        body_html = raw
    else:
        body_text = raw
        body_html = None
    return EmailFull(
        uid=item.get("id") or "",
        subject=item.get("subject") or "",
        from_=_addr_str(item.get("from")) or _addr_str(item.get("sender")),
        to=_addr_list_str(item.get("toRecipients")),
        cc=_addr_list_str(item.get("ccRecipients")),
        date=item.get("receivedDateTime") or item.get("sentDateTime") or "",
        snippet=(item.get("bodyPreview") or body_text[:160] or "")[:160],
        body=body_text.strip()[:20000],
        body_html=body_html,
    )


def _to_recipient(addr: str) -> dict[str, Any]:
    """'name <a@b>' 或纯地址 → Graph emailAddress 结构。"""
    addr = addr.strip()
    m = re.match(r'\s*"?([^"<]*)"?\s*<([^>]+)>', addr)
    if m:
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip()
    else:
        name, email = "", addr
    return {"emailAddress": {"address": email, **({"name": name} if name else {})}}


def _to_recipients(items: list[str] | None) -> list[dict[str, Any]]:
    return [_to_recipient(x) for x in (items or []) if x]


def _attachment_to_graph(att: OutgoingAttachment) -> dict[str, Any]:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": att.filename,
        "contentType": att.mime or "application/octet-stream",
        "contentBytes": base64.b64encode(att.data).decode("ascii"),
    }


class EmailGraphClient:
    """Graph 邮件客户端（与 IMAP `EmailClient` 接口同构）。

    注意：构造签名故意接受一个被忽略的 `account` 参数，方便在工厂里和
    `EmailClient(account)` 走同一签名。
    """

    def __init__(self, account: Any = None) -> None:  # noqa: ARG002
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._folder_cache: dict[str, str] = {}  # displayName.lower() → folderId

    # ---------- HTTP plumbing ----------

    async def _http(self) -> httpx.AsyncClient:
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
            "Prefer": 'outlook.body-content-type="text"',
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{_GRAPH_BASE}{path}"
        client = await self._http()
        h = dict(await self._headers())
        h.update(kwargs.pop("headers", {}) or {})
        resp = await client.request(method, url, headers=h, **kwargs)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            await asyncio.sleep(min(retry_after, 5.0))
            resp = await client.request(method, url, headers=h, **kwargs)
        if resp.status_code >= 400:
            body = resp.text[:1000]
            logger.warning(
                "graph_email_error",
                extra={"method": method, "path": path, "status": resp.status_code, "body": body},
            )
            resp.raise_for_status()
        return resp

    # ---------- folder 解析 ----------

    async def _resolve_folder_id(self, mailbox: str) -> str:
        """displayName / well-known 名 → folderId。"""
        name = (mailbox or "INBOX").strip()
        key = name.lower()
        if key in _WELLKNOWN_FOLDERS:
            return _WELLKNOWN_FOLDERS[key]
        if key in self._folder_cache:
            return self._folder_cache[key]
        # 拉取顶层文件夹列表（够用；多层级未来再做递归）
        resp = await self._request("GET", "/me/mailFolders", params={"$top": "100"})
        for f in resp.json().get("value") or []:
            disp = (f.get("displayName") or "").lower()
            if disp:
                self._folder_cache[disp] = f.get("id") or disp
        if key in self._folder_cache:
            return self._folder_cache[key]
        # 退回直接当 folderId 用（用户可能就是传的 id）
        return name

    # ---------- 列表 / 搜索 / 详情 ----------

    async def list_inbox(self, limit: int = 20, mailbox: str = "INBOX") -> list[dict[str, Any]]:
        folder_id = await self._resolve_folder_id(mailbox)
        params = {
            "$top": str(int(limit)),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,sender,toRecipients,receivedDateTime,bodyPreview",
        }
        resp = await self._request("GET", f"/me/mailFolders/{folder_id}/messages", params=params)
        items = resp.json().get("value") or []
        return [asdict(_meta_from_graph(i)) for i in items]

    async def list_folders(self) -> list[str]:
        resp = await self._request("GET", "/me/mailFolders", params={"$top": "100"})
        return [f.get("displayName") or "" for f in resp.json().get("value") or [] if f.get("displayName")]

    async def fetch(self, uid: str, mailbox: str = "INBOX") -> Optional[dict[str, Any]]:  # noqa: ARG002
        try:
            resp = await self._request("GET", f"/me/messages/{uid}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return asdict(_full_from_graph(resp.json()))

    async def search(self, query: str, limit: int = 20, mailbox: str = "INBOX") -> list[dict[str, Any]]:
        # /me/messages?$search="..."（注意 $search 与 $orderby 互斥；Graph 自带相关性排序）
        # mailbox 通过 $filter parentFolderId eq '...' 限定（可选）
        params: dict[str, str] = {
            "$top": str(int(limit)),
            "$search": f'"{query}"',
            "$select": "id,subject,from,sender,toRecipients,receivedDateTime,bodyPreview,parentFolderId",
        }
        headers = {"ConsistencyLevel": "eventual"}  # $search 推荐
        resp = await self._request("GET", "/me/messages", params=params, headers=headers)
        items = resp.json().get("value") or []
        if mailbox and mailbox.upper() not in ("", "ALL"):
            try:
                folder_id = await self._resolve_folder_id(mailbox)
                # well-known 别名跟 parentFolderId 不直接相等；只在 displayName 命中缓存的 id 时过滤
                if folder_id and folder_id not in _WELLKNOWN_FOLDERS.values():
                    items = [i for i in items if i.get("parentFolderId") == folder_id]
            except Exception:
                pass
        return [asdict(_meta_from_graph(i)) for i in items]

    # ---------- 写：发送 / 回复 / 转发 ----------

    async def send(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: Optional[list[str] | str] = None,
        html: Optional[str] = None,
        attachments: Optional[list[OutgoingAttachment]] = None,
        in_reply_to: Optional[str] = None,  # noqa: ARG002 - Graph 自带线程
        references: Optional[str] = None,   # noqa: ARG002
    ) -> str:
        to_list = [to] if isinstance(to, str) else list(to)
        cc_list = [cc] if isinstance(cc, str) else (list(cc) if cc else None)
        message: dict[str, Any] = {
            "subject": subject or "",
            "body": {
                "contentType": "html" if html else "text",
                "content": html if html else (body or ""),
            },
            "toRecipients": _to_recipients(to_list),
        }
        if cc_list:
            message["ccRecipients"] = _to_recipients(cc_list)
        if attachments:
            message["attachments"] = [_attachment_to_graph(a) for a in attachments]
        await self._request(
            "POST", "/me/sendMail", json={"message": message, "saveToSentItems": True}
        )
        # Graph sendMail 不返回服务端 id；用合成时间戳保留 IMAP 接口语义
        return f"sent:{datetime.now(timezone.utc).isoformat()}"

    async def reply(
        self,
        uid: str,
        body: str,
        *,
        mailbox: str = "INBOX",  # noqa: ARG002
        reply_all: bool = False,
        extra_cc: Optional[list[str]] = None,
        attachments: Optional[list[OutgoingAttachment]] = None,
        include_quote: bool = True,  # noqa: ARG002 - Graph reply 自动带原文
    ) -> dict[str, Any]:
        path = f"/me/messages/{uid}/{'replyAll' if reply_all else 'reply'}"
        msg_extras: dict[str, Any] = {}
        if extra_cc:
            msg_extras["ccRecipients"] = _to_recipients(extra_cc)
        if attachments:
            msg_extras["attachments"] = [_attachment_to_graph(a) for a in attachments]
        payload: dict[str, Any] = {"comment": body or ""}
        if msg_extras:
            payload["message"] = msg_extras
        await self._request("POST", path, json=payload)
        return {
            "sent_message_id": f"sent:{datetime.now(timezone.utc).isoformat()}",
            "in_reply_to": uid,
            "reply_all": reply_all,
            "to": [],  # Graph 自动用原邮件 from 作为收件人
            "cc": list(extra_cc or []),
        }

    async def forward(
        self,
        uid: str,
        to: list[str] | str,
        *,
        mailbox: str = "INBOX",  # noqa: ARG002
        body_prefix: str = "",
        cc: Optional[list[str] | str] = None,
        attachments: Optional[list[OutgoingAttachment]] = None,
        include_quote: bool = True,  # noqa: ARG002 - Graph forward 自动带原文
    ) -> dict[str, Any]:
        to_list = [to] if isinstance(to, str) else list(to)
        cc_list = [cc] if isinstance(cc, str) else (list(cc) if cc else None)
        message_extras: dict[str, Any] = {}
        if cc_list:
            message_extras["ccRecipients"] = _to_recipients(cc_list)
        if attachments:
            message_extras["attachments"] = [_attachment_to_graph(a) for a in attachments]
        payload: dict[str, Any] = {
            "comment": body_prefix or "",
            "toRecipients": _to_recipients(to_list),
        }
        if message_extras:
            payload["message"] = message_extras
        await self._request("POST", f"/me/messages/{uid}/forward", json=payload)
        return {
            "sent_message_id": f"sent:{datetime.now(timezone.utc).isoformat()}",
            "forwarded_uid": uid,
            "to": to_list,
            "cc": cc_list or [],
        }

    # ---------- Flags / 状态 ----------

    async def mark_seen(self, uid: str, seen: bool = True, mailbox: str = "INBOX") -> dict[str, Any]:  # noqa: ARG002
        await self._request("PATCH", f"/me/messages/{uid}", json={"isRead": bool(seen)})
        return {"ok": True, "uid": uid, "isRead": bool(seen)}

    async def mark_flagged(self, uid: str, flagged: bool = True, mailbox: str = "INBOX") -> dict[str, Any]:  # noqa: ARG002
        flag_status = "flagged" if flagged else "notFlagged"
        await self._request(
            "PATCH", f"/me/messages/{uid}", json={"flag": {"flagStatus": flag_status}}
        )
        return {"ok": True, "uid": uid, "flagStatus": flag_status}

    async def update_flags(
        self,
        uid: str,
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
        mailbox: str = "INBOX",  # noqa: ARG002
    ) -> dict[str, Any]:
        # IMAP \Seen → isRead；\Flagged → flag.flagStatus；其它 IMAP 标位 Graph 不直接支持
        body: dict[str, Any] = {}
        for f in (add or []):
            if f.lower() in ("\\seen", "seen"):
                body["isRead"] = True
            elif f.lower() in ("\\flagged", "flagged"):
                body["flag"] = {"flagStatus": "flagged"}
        for f in (remove or []):
            if f.lower() in ("\\seen", "seen"):
                body["isRead"] = False
            elif f.lower() in ("\\flagged", "flagged"):
                body["flag"] = {"flagStatus": "notFlagged"}
        if not body:
            return {"ok": False, "reason": "no supported flags", "add": add, "remove": remove}
        await self._request("PATCH", f"/me/messages/{uid}", json=body)
        return {"ok": True, "uid": uid, "applied": body}

    # ---------- 移动 / 删除 ----------

    async def move(self, uid: str, dest_mailbox: str, mailbox: str = "INBOX") -> dict[str, Any]:  # noqa: ARG002
        dest_id = await self._resolve_folder_id(dest_mailbox)
        await self._request(
            "POST", f"/me/messages/{uid}/move", json={"destinationId": dest_id}
        )
        return {"ok": True, "uid": uid, "to": dest_mailbox}

    async def delete(self, uid: str, mailbox: str = "INBOX", hard: bool = False) -> dict[str, Any]:  # noqa: ARG002
        if hard:
            await self._request("DELETE", f"/me/messages/{uid}")
            return {"ok": True, "uid": uid, "hard": True}
        # 软删 = 移到 Deleted Items
        await self._request(
            "POST", f"/me/messages/{uid}/move", json={"destinationId": "deleteditems"}
        )
        return {"ok": True, "uid": uid, "moved_to": "DeletedItems", "hard": False}

    # ---------- 附件 ----------

    async def list_attachments(self, uid: str, mailbox: str = "INBOX") -> list[dict[str, Any]]:  # noqa: ARG002
        resp = await self._request(
            "GET",
            f"/me/messages/{uid}/attachments",
            params={"$select": "id,name,contentType,size"},
        )
        out: list[dict[str, Any]] = []
        for a in resp.json().get("value") or []:
            out.append(asdict(AttachmentMeta(
                part_id=a.get("id") or "",
                filename=a.get("name") or "",
                mime=a.get("contentType") or "application/octet-stream",
                size=int(a.get("size") or 0),
            )))
        return out

    async def fetch_attachment(
        self, uid: str, part_id: str, mailbox: str = "INBOX",  # noqa: ARG002
    ) -> Optional[tuple[str, str, bytes]]:
        try:
            resp = await self._request("GET", f"/me/messages/{uid}/attachments/{part_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        a = resp.json()
        if a.get("@odata.type", "").endswith("fileAttachment"):
            data = base64.b64decode(a.get("contentBytes") or "")
        else:
            # itemAttachment / referenceAttachment：先不支持，返回空 payload
            data = b""
        return (
            a.get("name") or f"part-{part_id}",
            a.get("contentType") or "application/octet-stream",
            data,
        )

    # ---------- 资源管理 ----------

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
