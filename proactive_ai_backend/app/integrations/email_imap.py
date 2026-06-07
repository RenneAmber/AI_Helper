"""
通用 IMAP/SMTP 邮件客户端：覆盖 QQ / Gmail / Outlook(可用时) / 163 等。

实现要点：
- 同步的 imaplib / smtplib 通过 asyncio.to_thread 包成 async，避免阻塞事件循环
- 邮件正文优先取 text/plain，其次把 text/html 转纯文本
- 中文字段头解码使用 email.header.decode_header
- 不做正则/HTML 复杂解析；够 LLM 阅读、写邮件足矣
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import re
import smtplib
import ssl
from dataclasses import asdict, dataclass
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formatdate, getaddresses, make_msgid
from typing import Any, Optional

from ..logging_setup import get_logger
from .email_accounts import EmailAccount

logger = get_logger("integrations.email")

_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]+\n")


def _decode_h(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _html_to_text(html: str) -> str:
    txt = _HTML_TAG.sub("", html)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    txt = _WS.sub("\n", txt)
    return txt.strip()


def _extract_body(msg: EmailMessage) -> tuple[str, str | None]:
    """返回 (纯文本 body, 原始 HTML or None)。
    纯文本给 LLM / agent 用；HTML 留给 web 渲染（permalink 页面）。
    """
    plain: str | None = None
    html: str | None = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and plain is None:
                try:
                    plain = part.get_content()
                except Exception:
                    plain = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
            elif ctype == "text/html" and html is None:
                try:
                    html = part.get_content()
                except Exception:
                    html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
    else:
        try:
            raw = msg.get_content()
        except Exception:
            raw = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")
        if msg.get_content_type() == "text/html":
            html = raw
        else:
            plain = raw
    body = plain if plain else (_html_to_text(html) if html else "")
    # 纯文本长度兜底（避免一次塞给模型 100KB），HTML 不截断（webview 用）
    return (body or "").strip()[:20000], html


@dataclass
class EmailMeta:
    uid: str
    subject: str
    from_: str
    to: str
    date: str
    snippet: str


@dataclass
class EmailFull(EmailMeta):
    body: str
    cc: str = ""
    body_html: str | None = None


# ----------------- IMAP helpers (sync, wrapped via to_thread) -----------------

def _imap_login(account: EmailAccount) -> imaplib.IMAP4:
    if account.imap_ssl:
        client = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ssl.create_default_context())
    else:
        client = imaplib.IMAP4(account.imap_host, account.imap_port)
    client.login(account.address, account.password)
    # ✅ QQ / 163 必须发 ID 命令，否则很多操作会沉默返回空
    host = (account.imap_host or "").lower()
    if "qq.com" in host or "163.com" in host or "126.com" in host:
        try:
            client._simple_command(
                "ID",
                '("name" "aido" "version" "1.0" "vendor" "aido-mailbot" "contact" "")',
            )
            client._untagged_response("OK", [None], "ID")
        except Exception as e:
            logger.warning("imap_id_command_failed", extra={"err": str(e)})
    return client


def _select_inbox(client: imaplib.IMAP4, mailbox: str) -> int:
    """选择邮箱并返回邮件总数（失败返回 -1）"""
    typ, data = client.select(mailbox, readonly=True)
    if typ != "OK":
        logger.warning("imap_select_failed", extra={"mailbox": mailbox, "resp": data})
        return -1
    try:
        return int(data[0])
    except Exception:
        return 0


def _list_folders_sync(account: EmailAccount) -> list[str]:
    client = _imap_login(account)
    try:
        typ, data = client.list()
        if typ != "OK":
            return []
        out = []
        for raw in data:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            # 格式示例： (\HasNoChildren) "/" "INBOX"
            parts = line.rsplit(" ", 1)
            name = parts[-1].strip().strip('"')
            out.append(name)
        return out
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _list_inbox_sync(account: EmailAccount, limit: int, mailbox: str) -> list[EmailMeta]:
    client = _imap_login(account)
    try:
        total = _select_inbox(client, mailbox)
        if total < 0:
            return []
        if total == 0:
            logger.info("imap_inbox_empty", extra={"mailbox": mailbox})
            return []
        typ, data = client.uid("search", None, "ALL")
        if typ != "OK":
            logger.warning("imap_search_all_failed", extra={"resp": data})
            return []
        uids = data[0].split()[-limit:]
        uids.reverse()
        out: list[EmailMeta] = []
        for uid in uids:
            typ, msg_data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw, policy=default_policy)
            out.append(EmailMeta(
                uid=uid.decode(),
                subject=_decode_h(msg.get("Subject", "")),
                from_=_decode_h(msg.get("From", "")),
                to=_decode_h(msg.get("To", "")),
                date=msg.get("Date", ""),
                snippet="",
            ))
        return out
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _fetch_sync(account: EmailAccount, uid: str, mailbox: str) -> Optional[EmailFull]:
    client = _imap_login(account)
    try:
        client.select(mailbox, readonly=True)
        typ, msg_data = client.uid("fetch", uid.encode(), "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return None
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw, policy=default_policy)
        body, body_html = _extract_body(msg)
        return EmailFull(
            uid=uid,
            subject=_decode_h(msg.get("Subject", "")),
            from_=_decode_h(msg.get("From", "")),
            to=_decode_h(msg.get("To", "")),
            cc=_decode_h(msg.get("Cc", "")),
            date=msg.get("Date", ""),
            snippet=body[:160],
            body=body,
            body_html=body_html,
        )
    finally:
        try:
            client.logout()
        except Exception:
            pass


# ----------------- Attachments -----------------------------------------------


@dataclass
class AttachmentMeta:
    part_id: str   # 我们内部寻址用：MIME 部件在 walk() 中的索引
    filename: str
    mime: str
    size: int


def _walk_attachments(msg: EmailMessage) -> list[tuple[str, EmailMessage]]:
    """返回 [(part_id, part), ...]，只挑视为"附件"的部件。
    判定：有 filename 或 Content-Disposition: attachment。
    """
    out: list[tuple[str, EmailMessage]] = []
    for i, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        cd = (part.get("Content-Disposition") or "").lower()
        fn = part.get_filename()
        if fn or "attachment" in cd:
            out.append((str(i), part))
    return out


def _list_attachments_sync(account: EmailAccount, uid: str, mailbox: str) -> list[AttachmentMeta]:
    client = _imap_login(account)
    try:
        client.select(mailbox, readonly=True)
        typ, msg_data = client.uid("fetch", uid.encode(), "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return []
        msg = email.message_from_bytes(msg_data[0][1], policy=default_policy)
        items: list[AttachmentMeta] = []
        for part_id, part in _walk_attachments(msg):
            payload = part.get_payload(decode=True) or b""
            items.append(
                AttachmentMeta(
                    part_id=part_id,
                    filename=_decode_h(part.get_filename() or f"part-{part_id}"),
                    mime=part.get_content_type() or "application/octet-stream",
                    size=len(payload),
                )
            )
        return items
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _fetch_attachment_sync(
    account: EmailAccount, uid: str, part_id: str, mailbox: str
) -> Optional[tuple[str, str, bytes]]:
    """返回 (filename, mime, payload bytes)；找不到返回 None。"""
    client = _imap_login(account)
    try:
        client.select(mailbox, readonly=True)
        typ, msg_data = client.uid("fetch", uid.encode(), "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return None
        msg = email.message_from_bytes(msg_data[0][1], policy=default_policy)
        for pid, part in _walk_attachments(msg):
            if pid == part_id:
                payload = part.get_payload(decode=True) or b""
                filename = _decode_h(part.get_filename() or f"part-{pid}")
                mime = part.get_content_type() or "application/octet-stream"
                return filename, mime, payload
        return None
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _search_sync(account: EmailAccount, query: str, limit: int, mailbox: str) -> list[EmailMeta]:
    """关键字搜索：
    - 纯 ASCII 关键字：走 IMAP SEARCH SUBJECT/FROM（QQ 不走 CHARSET，他不支持）
    - 含非 ASCII（中文）：拉最近 N 封 header 本地过滤
    """
    client = _imap_login(account)
    try:
        total = _select_inbox(client, mailbox)
        if total < 0:
            return []

        try:
            query.encode("ascii")
            ascii_only = True
        except UnicodeEncodeError:
            ascii_only = False

        uids: list[bytes] = []
        if ascii_only:
            # IMAP SEARCH 不带 CHARSET：在 QQ / 163 上最稳。OR SUBJECT FROM
            quoted = b'"' + query.replace('"', "").encode("ascii") + b'"'
            typ, data = client.uid("search", None, "OR", "SUBJECT", quoted, "FROM", quoted)
            if typ == "OK" and data and data[0]:
                uids = data[0].split()
            else:
                logger.warning("imap_search_ascii_empty", extra={"q": query, "resp": data})
        else:
            # 中文：本地过滤最近 200 封的 header
            typ, data = client.uid("search", None, "ALL")
            if typ == "OK" and data and data[0]:
                all_uids = data[0].split()[-200:]
                q = query.lower()
                for uid in reversed(all_uids):
                    typ2, msg_data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
                    if typ2 != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw, policy=default_policy)
                    if q in _decode_h(msg.get("Subject", "")).lower() or q in _decode_h(msg.get("From", "")).lower():
                        uids.append(uid)
                        if len(uids) >= limit:
                            break

        uids = uids[-limit:]
        uids.reverse()
        out: list[EmailMeta] = []
        for uid in uids:
            typ, msg_data = client.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)])")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw, policy=default_policy)
            out.append(EmailMeta(
                uid=uid.decode(),
                subject=_decode_h(msg.get("Subject", "")),
                from_=_decode_h(msg.get("From", "")),
                to=_decode_h(msg.get("To", "")),
                date=msg.get("Date", ""),
                snippet="",
            ))
        return out
    finally:
        try:
            client.logout()
        except Exception:
            pass


# ----------------- Update / Delete (写操作) ----------------------------------

# IMAP flag 速查：
#   \Seen / \Answered / \Flagged / \Deleted / \Draft
# QQ 邮箱：删除 = 移动到「已删除」文件夹后 EXPUNGE；否则只是打 \Deleted 标记，邮件还能恢复。

_TRASH_CANDIDATES = ["Deleted Messages", "Trash", "已删除", "&XfJSIJZkkK5O-"]


def _flag_str(flags: list[str]) -> str:
    """把 ['\\Seen', '\\Flagged'] 拼成 '(\\Seen \\Flagged)'。"""
    return "(" + " ".join(flags) + ")"


def _update_flags_sync(
    account: EmailAccount,
    uid: str,
    add: Optional[list[str]] = None,
    remove: Optional[list[str]] = None,
    mailbox: str = "INBOX",
) -> dict:
    client = _imap_login(account)
    try:
        # 注意：改标记不能 readonly
        typ, data = client.select(mailbox, readonly=False)
        if typ != "OK":
            return {"ok": False, "reason": "select_failed", "resp": str(data)}
        results = []
        if add:
            t, d = client.uid("store", uid.encode(), "+FLAGS", _flag_str(add))
            results.append({"op": "add", "flags": add, "resp": t})
        if remove:
            t, d = client.uid("store", uid.encode(), "-FLAGS", _flag_str(remove))
            results.append({"op": "remove", "flags": remove, "resp": t})
        return {"ok": True, "uid": uid, "mailbox": mailbox, "results": results}
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _find_trash_folder(client: imaplib.IMAP4) -> Optional[str]:
    typ, data = client.list()
    if typ != "OK":
        return None
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        parts = line.rsplit(" ", 1)
        name = parts[-1].strip().strip('"')
        if name in _TRASH_CANDIDATES:
            return name
        if "trash" in name.lower() or "deleted" in name.lower() or "已删除" in name:
            return name
    return None


def _delete_sync(
    account: EmailAccount,
    uid: str,
    mailbox: str = "INBOX",
    hard: bool = False,
) -> dict:
    """
    hard=False: 移动到 Trash（QQ 行为：先 COPY 到「已删除」再标记原邮件 \Deleted 并 EXPUNGE）
    hard=True : 直接打 \Deleted 标记并 EXPUNGE（不可恢复）
    """
    client = _imap_login(account)
    try:
        typ, data = client.select(mailbox, readonly=False)
        if typ != "OK":
            return {"ok": False, "reason": "select_failed", "resp": str(data)}

        moved_to = None
        if not hard:
            trash = _find_trash_folder(client)
            if trash and trash != mailbox:
                # 优先 MOVE（RFC 6851），不支持时降级到 COPY + STORE
                try:
                    t, d = client.uid("move", uid.encode(), trash)
                    if t == "OK":
                        moved_to = trash
                    else:
                        raise RuntimeError(f"move not ok: {d}")
                except Exception:
                    client.uid("copy", uid.encode(), trash)
                    client.uid("store", uid.encode(), "+FLAGS", "(\\Deleted)")
                    client.expunge()
                    moved_to = trash
            else:
                # 没找到 Trash → 退化为硬删除
                hard = True

        if hard:
            client.uid("store", uid.encode(), "+FLAGS", "(\\Deleted)")
            client.expunge()

        return {"ok": True, "uid": uid, "mailbox": mailbox, "moved_to": moved_to, "hard": hard}
    finally:
        try:
            client.logout()
        except Exception:
            pass


def _move_sync(
    account: EmailAccount,
    uid: str,
    dest_mailbox: str,
    mailbox: str = "INBOX",
) -> dict:
    client = _imap_login(account)
    try:
        typ, data = client.select(mailbox, readonly=False)
        if typ != "OK":
            return {"ok": False, "reason": "select_failed", "resp": str(data)}
        try:
            t, d = client.uid("move", uid.encode(), dest_mailbox)
            if t != "OK":
                raise RuntimeError(f"move not ok: {d}")
        except Exception:
            client.uid("copy", uid.encode(), dest_mailbox)
            client.uid("store", uid.encode(), "+FLAGS", "(\\Deleted)")
            client.expunge()
        return {"ok": True, "uid": uid, "from": mailbox, "to": dest_mailbox}
    finally:
        try:
            client.logout()
        except Exception:
            pass


# ----------------- SMTP -------------------------------------------------------

@dataclass
class OutgoingAttachment:
    """发件附件载体：data 必须已经是原始二进制（路由层负责 base64 decode）。"""
    filename: str
    data: bytes
    mime: str = "application/octet-stream"


def _send_sync(
    account: EmailAccount,
    to: list[str],
    subject: str,
    body: str,
    cc: Optional[list[str]] = None,
    html: Optional[str] = None,
    attachments: Optional[list[OutgoingAttachment]] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> str:
    msg = EmailMessage()
    msg["From"] = account.address
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body or "")
    if html:
        msg.add_alternative(html, subtype="html")
    for att in (attachments or []):
        maintype, _, subtype = att.mime.partition("/")
        msg.add_attachment(
            att.data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att.filename,
        )

    recipients = to + (cc or [])
    if account.smtp_ssl:
        smtp = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(account.smtp_host, account.smtp_port)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
    try:
        smtp.login(account.address, account.password)
        smtp.send_message(msg, from_addr=account.address, to_addrs=recipients)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass
    return msg["Message-ID"]


# ----------------- Reply / Forward 辅助 --------------------------------------

def _quote_body(orig_from: str, orig_date: str, orig_body: str, max_chars: int = 4000) -> str:
    """生成 reply / forward 的引文块。"""
    body = (orig_body or "").strip()
    if len(body) > max_chars:
        body = body[:max_chars] + "\n…（原文已截断）"
    header = f"在 {orig_date} {orig_from} 写道："
    quoted = "\n".join("> " + line for line in body.splitlines())
    return f"{header}\n{quoted}"


def _split_addrs(value: str) -> list[str]:
    if not value:
        return []
    return [addr for _, addr in getaddresses([value]) if addr]


def _fetch_for_reply_sync(account: EmailAccount, uid: str, mailbox: str) -> Optional[dict]:
    """专门给 reply/forward 用：拉原邮件 Message-ID / References / 主题 / 收发件人 / 正文。"""
    client = _imap_login(account)
    try:
        client.select(mailbox, readonly=True)
        typ, msg_data = client.uid("fetch", uid.encode(), "(RFC822)")
        if typ != "OK" or not msg_data or not msg_data[0]:
            return None
        msg = email.message_from_bytes(msg_data[0][1], policy=default_policy)
        body, _html = _extract_body(msg)
        return {
            "message_id": msg.get("Message-ID", "") or "",
            "references": msg.get("References", "") or "",
            "subject": _decode_h(msg.get("Subject", "")),
            "from": _decode_h(msg.get("From", "")),
            "to": _decode_h(msg.get("To", "")),
            "cc": _decode_h(msg.get("Cc", "")),
            "reply_to": _decode_h(msg.get("Reply-To", "")),
            "date": msg.get("Date", ""),
            "body": body,
        }
    finally:
        try:
            client.logout()
        except Exception:
            pass


# ----------------- Public async API ------------------------------------------

class EmailClient:
    """高层包装：async 接口；所有 I/O 走线程池避免阻塞事件循环。"""

    def __init__(self, account: EmailAccount) -> None:
        self.account = account

    async def list_inbox(self, limit: int = 20, mailbox: str = "INBOX") -> list[dict[str, Any]]:
        items = await asyncio.to_thread(_list_inbox_sync, self.account, limit, mailbox)
        return [asdict(i) for i in items]

    async def list_folders(self) -> list[str]:
        return await asyncio.to_thread(_list_folders_sync, self.account)

    async def fetch(self, uid: str, mailbox: str = "INBOX") -> Optional[dict[str, Any]]:
        item = await asyncio.to_thread(_fetch_sync, self.account, uid, mailbox)
        return asdict(item) if item else None

    async def search(self, query: str, limit: int = 20, mailbox: str = "INBOX") -> list[dict[str, Any]]:
        items = await asyncio.to_thread(_search_sync, self.account, query, limit, mailbox)
        return [asdict(i) for i in items]

    async def send(
        self,
        to: list[str] | str,
        subject: str,
        body: str,
        cc: Optional[list[str] | str] = None,
        html: Optional[str] = None,
        attachments: Optional[list[OutgoingAttachment]] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> str:
        to_list = [to] if isinstance(to, str) else list(to)
        cc_list = [cc] if isinstance(cc, str) else (list(cc) if cc else None)
        return await asyncio.to_thread(
            _send_sync, self.account, to_list, subject, body, cc_list, html,
            attachments, in_reply_to, references,
        )

    # ----- Attachments -----
    async def list_attachments(self, uid: str, mailbox: str = "INBOX") -> list[dict[str, Any]]:
        items = await asyncio.to_thread(_list_attachments_sync, self.account, uid, mailbox)
        return [asdict(i) for i in items]

    async def fetch_attachment(
        self, uid: str, part_id: str, mailbox: str = "INBOX"
    ) -> Optional[tuple[str, str, bytes]]:
        return await asyncio.to_thread(_fetch_attachment_sync, self.account, uid, part_id, mailbox)

    # ----- Reply / Forward -----
    async def reply(
        self,
        uid: str,
        body: str,
        *,
        mailbox: str = "INBOX",
        reply_all: bool = False,
        extra_cc: Optional[list[str]] = None,
        attachments: Optional[list[OutgoingAttachment]] = None,
        include_quote: bool = True,
    ) -> dict:
        """对某封邮件回复。自动套 In-Reply-To / References / Re: 主题 / 引文。"""
        orig = await asyncio.to_thread(_fetch_for_reply_sync, self.account, uid, mailbox)
        if not orig:
            return {"error": "original message not found", "uid": uid}

        # 收件人：优先 Reply-To，其次 From
        reply_target = orig.get("reply_to") or orig.get("from") or ""
        to = _split_addrs(reply_target) or _split_addrs(orig.get("from", ""))
        if not to:
            return {"error": "cannot determine reply target", "uid": uid}

        cc: list[str] = list(extra_cc or [])
        if reply_all:
            my_addr = (self.account.address or "").lower()
            for addr in _split_addrs(orig.get("to", "")) + _split_addrs(orig.get("cc", "")):
                low = addr.lower()
                if low != my_addr and low not in (a.lower() for a in to) and low not in (c.lower() for c in cc):
                    cc.append(addr)

        subj = orig.get("subject", "") or ""
        if not subj.lower().startswith("re:"):
            subj = "Re: " + subj

        # In-Reply-To / References
        mid = orig.get("message_id", "") or ""
        refs = orig.get("references", "") or ""
        new_refs = (refs + " " + mid).strip() if mid else refs

        # 引文
        full_body = body or ""
        if include_quote:
            full_body = full_body.rstrip() + "\n\n" + _quote_body(
                orig.get("from", ""), orig.get("date", ""), orig.get("body", "")
            )

        sent_id = await self.send(
            to=to,
            subject=subj,
            body=full_body,
            cc=cc or None,
            attachments=attachments,
            in_reply_to=mid or None,
            references=new_refs or None,
        )
        return {
            "sent_message_id": sent_id,
            "in_reply_to": mid,
            "to": to,
            "cc": cc,
            "subject": subj,
        }

    async def forward(
        self,
        uid: str,
        to: list[str] | str,
        *,
        mailbox: str = "INBOX",
        body_prefix: str = "",
        cc: Optional[list[str] | str] = None,
        attachments: Optional[list[OutgoingAttachment]] = None,
        include_quote: bool = True,
    ) -> dict:
        """转发某封邮件。新增 to/cc；主题前缀 Fwd:；原文以引文形式插入。"""
        orig = await asyncio.to_thread(_fetch_for_reply_sync, self.account, uid, mailbox)
        if not orig:
            return {"error": "original message not found", "uid": uid}

        to_list = [to] if isinstance(to, str) else list(to)
        cc_list = [cc] if isinstance(cc, str) else (list(cc) if cc else None)

        subj = orig.get("subject", "") or ""
        if not subj.lower().startswith(("fwd:", "fw:")):
            subj = "Fwd: " + subj

        full_body = body_prefix.rstrip()
        if include_quote:
            quote_header = (
                "---------- 转发邮件 ----------\n"
                f"发件人: {orig.get('from','')}\n"
                f"日期: {orig.get('date','')}\n"
                f"主题: {orig.get('subject','')}\n"
                f"收件人: {orig.get('to','')}\n\n"
            )
            full_body = (full_body + "\n\n" if full_body else "") + quote_header + (orig.get("body") or "")

        sent_id = await self.send(
            to=to_list,
            subject=subj,
            body=full_body,
            cc=cc_list,
            attachments=attachments,
        )
        return {
            "sent_message_id": sent_id,
            "forwarded_uid": uid,
            "to": to_list,
            "cc": cc_list or [],
            "subject": subj,
        }

    # ----- Update -----
    async def mark_seen(self, uid: str, seen: bool = True, mailbox: str = "INBOX") -> dict:
        if seen:
            return await asyncio.to_thread(_update_flags_sync, self.account, uid, ["\\Seen"], None, mailbox)
        return await asyncio.to_thread(_update_flags_sync, self.account, uid, None, ["\\Seen"], mailbox)

    async def mark_flagged(self, uid: str, flagged: bool = True, mailbox: str = "INBOX") -> dict:
        if flagged:
            return await asyncio.to_thread(_update_flags_sync, self.account, uid, ["\\Flagged"], None, mailbox)
        return await asyncio.to_thread(_update_flags_sync, self.account, uid, None, ["\\Flagged"], mailbox)

    async def update_flags(
        self,
        uid: str,
        add: Optional[list[str]] = None,
        remove: Optional[list[str]] = None,
        mailbox: str = "INBOX",
    ) -> dict:
        return await asyncio.to_thread(_update_flags_sync, self.account, uid, add, remove, mailbox)

    async def move(self, uid: str, dest_mailbox: str, mailbox: str = "INBOX") -> dict:
        return await asyncio.to_thread(_move_sync, self.account, uid, dest_mailbox, mailbox)

    # ----- Delete -----
    async def delete(self, uid: str, mailbox: str = "INBOX", hard: bool = False) -> dict:
        return await asyncio.to_thread(_delete_sync, self.account, uid, mailbox, hard)
