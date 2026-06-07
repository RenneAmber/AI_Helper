"""
邮件相关 REST 路由（v1，单账号、IMAP/SMTP）。

提供直接调试用的端点；Aido 的对话主链路 / 工作流编排里也会
通过 tools.py 注册的 "email.*" 工具间接复用同一套客户端。
"""

from __future__ import annotations

import base64
import binascii
import html as html_escape
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, EmailStr, Field

from ..integrations.email_accounts import PRESETS, get_default_account
from ..integrations.email_imap import EmailClient, OutgoingAttachment

router = APIRouter(prefix="/v1/email", tags=["email"])


def _client_or_400() -> EmailClient:
    account = get_default_account()
    if not account.configured:
        raise HTTPException(
            status_code=400,
            detail="Email account not configured. Set EMAIL_PROVIDER / EMAIL_ADDRESS / EMAIL_PASSWORD env vars.",
        )
    return EmailClient(account)


class AttachmentInput(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_b64: str = Field(min_length=1, description="附件原始字节的 base64 编码")
    mime: str = "application/octet-stream"


def _decode_attachments(items: list[AttachmentInput] | None) -> list[OutgoingAttachment] | None:
    if not items:
        return None
    out: list[OutgoingAttachment] = []
    for a in items:
        try:
            data = base64.b64decode(a.content_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"attachment {a.filename}: invalid base64: {exc}")
        out.append(OutgoingAttachment(filename=a.filename, data=data, mime=a.mime))
    return out


class SendRequest(BaseModel):
    to: list[EmailStr] = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=200_000)
    cc: list[EmailStr] | None = None
    html: str | None = None
    attachments: list[AttachmentInput] | None = None


class ReplyRequest(BaseModel):
    body: str = Field(default="", max_length=200_000)
    reply_all: bool = False
    extra_cc: list[EmailStr] | None = None
    attachments: list[AttachmentInput] | None = None
    include_quote: bool = True
    mailbox: str = "INBOX"


class ForwardRequest(BaseModel):
    to: list[EmailStr] = Field(min_length=1, max_length=20)
    cc: list[EmailStr] | None = None
    body_prefix: str = Field(default="", max_length=200_000)
    attachments: list[AttachmentInput] | None = None
    include_quote: bool = True
    mailbox: str = "INBOX"


@router.get("/account")
async def get_account() -> dict:
    """返回当前默认账号的安全信息（隐去密码）+ 已知预设。"""
    a = get_default_account()
    return {
        "configured": a.configured,
        "address": a.address,
        "imap": {"host": a.imap_host, "port": a.imap_port, "ssl": a.imap_ssl},
        "smtp": {"host": a.smtp_host, "port": a.smtp_port, "ssl": a.smtp_ssl},
        "presets": list(PRESETS.keys()),
    }


@router.get("/folders")
async def list_folders() -> dict:
    """诊断用：列出账号下所有可见邮件文件夹名。"""
    client = _client_or_400()
    return {"folders": await client.list_folders()}


@router.get("/inbox")
async def list_inbox(
    limit: int = Query(default=10, ge=1, le=100),
    mailbox: str = Query(default="INBOX"),
) -> dict:
    client = _client_or_400()
    return {"emails": await client.list_inbox(limit=limit, mailbox=mailbox)}


@router.get("/messages/{uid}")
async def read_message(uid: str, mailbox: str = Query(default="INBOX")) -> dict:
    client = _client_or_400()
    msg = await client.fetch(uid=uid, mailbox=mailbox)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    return msg


_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _linkify(text: str) -> str:
    """把纯文本里的 http(s) URL 包成 <a> 标签，并 HTML 转义其它字符。"""
    out = []
    last = 0
    for m in _URL_RE.finditer(text):
        out.append(html_escape.escape(text[last:m.start()]))
        url = m.group(1)
        safe = html_escape.escape(url, quote=True)
        out.append(f'<a href="{safe}" target="_blank" rel="noopener noreferrer">{safe}</a>')
        last = m.end()
    out.append(html_escape.escape(text[last:]))
    return "".join(out).replace("\n", "<br>")


@router.get("/messages/{uid}/view", response_class=HTMLResponse)
async def view_message(uid: str, mailbox: str = Query(default="INBOX")) -> HTMLResponse:
    """
    把单封邮件渲染成独立 HTML 页面，作为这封邮件的"永久链接"。
    Agent 在引用某封邮件时可以直接把这个 URL 给用户，用户点开即在新标签查看。
    """
    client = _client_or_400()
    msg = await client.fetch(uid=uid, mailbox=mailbox)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")

    subject = msg.get("subject") or "(无主题)"
    sender = msg.get("from_") or msg.get("from") or ""
    to = msg.get("to") or ""
    date = msg.get("date") or ""
    body_html = msg.get("body_html")
    body_text = msg.get("body") or ""

    if body_html:
        # 原始 HTML 直接放进 iframe srcdoc，避免污染外层样式
        body_block = (
            '<iframe style="width:100%;min-height:60vh;border:1px solid #ddd;border-radius:6px;background:#fff" '
            f'sandbox="allow-popups allow-popups-to-escape-sandbox allow-same-origin" srcdoc="{html_escape.escape(body_html, quote=True)}"></iframe>'
        )
    else:
        body_block = f'<pre style="white-space:pre-wrap;word-break:break-word;font-family:inherit;font-size:14px">{_linkify(body_text)}</pre>'

    # 推断 webmail 网页版入口（IMAP 给不了精确的邮件深链，只能跳收件箱）
    account = get_default_account()
    addr = (account.address or "").lower()
    if addr.endswith("@qq.com") or addr.endswith("@foxmail.com"):
        webmail_url, webmail_name = "https://mail.qq.com/", "QQ 邮箱"
    elif addr.endswith("@163.com") or addr.endswith("@126.com") or addr.endswith("@yeah.net"):
        webmail_url, webmail_name = "https://mail.163.com/", "网易邮箱"
    elif addr.endswith("@gmail.com"):
        webmail_url, webmail_name = "https://mail.google.com/", "Gmail"
    elif addr.endswith("@outlook.com") or addr.endswith("@hotmail.com") or addr.endswith("@live.com"):
        webmail_url, webmail_name = "https://outlook.live.com/", "Outlook"
    else:
        webmail_url, webmail_name = "", ""

    webmail_btn = (
        f'<a href="{webmail_url}" target="_blank" rel="noopener" '
        'style="display:inline-block;margin-left:12px;padding:4px 10px;background:#0969da;color:#fff;'
        'border-radius:4px;text-decoration:none;font-size:12px">'
        f'在 {webmail_name} 网页版打开收件箱 ↗</a>'
    ) if webmail_url else ""

    # 附件区
    attachments = await client.list_attachments(uid=uid, mailbox=mailbox)
    if attachments:
        from urllib.parse import quote as _q
        items_html = "".join(
            f'<li><a href="/v1/email/messages/{html_escape.escape(str(uid))}/attachments/{html_escape.escape(a["part_id"])}'
            f'?mailbox={_q(mailbox)}" download>📎 {html_escape.escape(a["filename"])}</a>'
            f' <span style="color:#888">({a["mime"]} · {_human_size(a["size"])})</span></li>'
            for a in attachments
        )
        attachments_block = (
            '<div style="background:#fff8e6;border:1px solid #f0d999;border-radius:6px;'
            'padding:10px 14px;margin-bottom:16px;font-size:13px;">'
            f'<b>附件 ({len(attachments)})</b>'
            f'<ul style="margin:6px 0 0 18px;padding:0;">{items_html}</ul></div>'
        )
    else:
        attachments_block = ""

    page = f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html_escape.escape(subject)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; max-width: 880px; margin: 0 auto; padding: 24px; color: #222; }}
  .meta {{ background: #f6f8fa; border: 1px solid #e3e6ea; border-radius: 6px; padding: 12px 16px; margin-bottom: 16px; font-size: 13px; line-height: 1.7; }}
  .meta b {{ display: inline-block; width: 60px; color: #555; }}
  h1 {{ font-size: 18px; margin: 0 0 12px; }}
  .uid {{ color: #888; font-size: 12px; }}
  a {{ color: #0969da; }}
</style>
</head><body>
<h1>{html_escape.escape(subject)}{webmail_btn}</h1>
<div class="meta">
  <div><b>发件人</b>{html_escape.escape(sender)}</div>
  <div><b>收件人</b>{html_escape.escape(to)}</div>
  <div><b>日期</b>{html_escape.escape(date)}</div>
  <div class="uid"><b>UID</b>{html_escape.escape(str(uid))} · 文件夹 {html_escape.escape(mailbox)}</div>
</div>
{attachments_block}
{body_block}
</body></html>"""
    return HTMLResponse(page)


@router.get("/search")
async def search_messages(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=10, ge=1, le=100),
    mailbox: str = Query(default="INBOX"),
) -> dict:
    client = _client_or_400()
    return {"emails": await client.search(query=q, limit=limit, mailbox=mailbox)}


@router.post("/send")
async def send_message(payload: SendRequest) -> dict:
    client = _client_or_400()
    msg_id = await client.send(
        to=[str(x) for x in payload.to],
        subject=payload.subject,
        body=payload.body,
        cc=[str(x) for x in (payload.cc or [])] or None,
        html=payload.html,
        attachments=_decode_attachments(payload.attachments),
    )
    return {"status": "sent", "message_id": msg_id}


# ---------------- 附件 ----------------

@router.get("/messages/{uid}/attachments")
async def list_attachments(uid: str, mailbox: str = Query(default="INBOX")) -> dict:
    """列出某封邮件的所有附件元数据。"""
    client = _client_or_400()
    items = await client.list_attachments(uid=uid, mailbox=mailbox)
    return {"uid": uid, "mailbox": mailbox, "attachments": items}


@router.get("/messages/{uid}/attachments/{part_id}")
async def download_attachment(
    uid: str, part_id: str, mailbox: str = Query(default="INBOX")
) -> Response:
    """二进制下载附件。浏览器会按 Content-Disposition 弹下载。"""
    client = _client_or_400()
    item = await client.fetch_attachment(uid=uid, part_id=part_id, mailbox=mailbox)
    if item is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    filename, mime, payload = item
    # RFC 5987 安全文件名（含中文）
    safe_ascii = re.sub(r"[^\w.\-]", "_", filename).strip("_") or "attachment"
    quoted_utf8 = re.sub(r"[\r\n]", "_", filename)
    from urllib.parse import quote
    return Response(
        content=payload,
        media_type=mime,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{quote(quoted_utf8)}"
            ),
            "Content-Length": str(len(payload)),
        },
    )


# ---------------- 回复 / 转发 ----------------

@router.post("/messages/{uid}/reply")
async def reply_message(uid: str, payload: ReplyRequest) -> dict:
    client = _client_or_400()
    return await client.reply(
        uid=uid,
        body=payload.body,
        mailbox=payload.mailbox,
        reply_all=payload.reply_all,
        extra_cc=[str(x) for x in (payload.extra_cc or [])] or None,
        attachments=_decode_attachments(payload.attachments),
        include_quote=payload.include_quote,
    )


@router.post("/messages/{uid}/forward")
async def forward_message(uid: str, payload: ForwardRequest) -> dict:
    client = _client_or_400()
    return await client.forward(
        uid=uid,
        to=[str(x) for x in payload.to],
        mailbox=payload.mailbox,
        body_prefix=payload.body_prefix,
        cc=[str(x) for x in (payload.cc or [])] or None,
        attachments=_decode_attachments(payload.attachments),
        include_quote=payload.include_quote,
    )


# ---------------- Update / Delete (CRUD 完整一套) ----------------

class FlagsRequest(BaseModel):
    """显式 +/- IMAP 标记。常用：\\Seen \\Flagged \\Answered \\Draft"""
    add: list[str] | None = None
    remove: list[str] | None = None
    mailbox: str = "INBOX"


class MoveRequest(BaseModel):
    dest_mailbox: str = Field(min_length=1)
    mailbox: str = "INBOX"


@router.patch("/messages/{uid}/seen")
async def mark_seen(uid: str, seen: bool = Query(default=True), mailbox: str = Query(default="INBOX")) -> dict:
    """快捷动作：标记已读 / 未读。"""
    client = _client_or_400()
    return await client.mark_seen(uid=uid, seen=seen, mailbox=mailbox)


@router.patch("/messages/{uid}/flag")
async def mark_flag(uid: str, flagged: bool = Query(default=True), mailbox: str = Query(default="INBOX")) -> dict:
    """快捷动作：星标 / 取消星标。"""
    client = _client_or_400()
    return await client.mark_flagged(uid=uid, flagged=flagged, mailbox=mailbox)


@router.patch("/messages/{uid}/flags")
async def update_flags(uid: str, payload: FlagsRequest) -> dict:
    """通用动作：直接增删任意 IMAP 标记。"""
    client = _client_or_400()
    return await client.update_flags(uid=uid, add=payload.add, remove=payload.remove, mailbox=payload.mailbox)


@router.post("/messages/{uid}/move")
async def move_message(uid: str, payload: MoveRequest) -> dict:
    """把邮件从 mailbox 移到 dest_mailbox（如归档、自定义文件夹）。"""
    client = _client_or_400()
    return await client.move(uid=uid, dest_mailbox=payload.dest_mailbox, mailbox=payload.mailbox)


@router.delete("/messages/{uid}")
async def delete_message(
    uid: str,
    mailbox: str = Query(default="INBOX"),
    hard: bool = Query(default=False, description="True=直接 EXPUNGE 不可恢复；False=尝试移到回收站"),
) -> dict:
    """删除邮件。默认软删除（移到「已删除」文件夹）。"""
    client = _client_or_400()
    return await client.delete(uid=uid, mailbox=mailbox, hard=hard)
