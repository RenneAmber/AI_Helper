"""
邮件感知的文本分块。

策略
----
1. 先做"邮件特有"清洗：剥离引用块（`>` 行 / "On <date> ... wrote:" 段）、
   常见签名（"-- " 之后 / "发自我的 iPhone" 之类）。
2. 再按段落分割；段落超过 chunk_chars 才进一步按句号/换行二次切分。
3. 输出加上 overlap，便于跨段落语义连续。
4. **第 0 块强制包含 subject + sender + date 头部**：检索时哪怕只命中后面段
   落，metadata 也能给模型完整上下文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import settings


# 常见签名/引用标记
_QUOTE_LINE = re.compile(r"^\s*>")
_REPLY_HEADER = re.compile(
    r"^\s*(在|On)\b.{0,80}(写道|wrote)[:：]?\s*$",
    re.IGNORECASE,
)
_SIG_DELIM = re.compile(r"^\s*--\s*$")
_FROM_IPHONE = re.compile(r"发自我的\s*\S+|Sent from my\s+\S+", re.IGNORECASE)


def strip_quotes_and_signature(body: str) -> str:
    lines = body.splitlines()
    out: list[str] = []
    for ln in lines:
        if _QUOTE_LINE.match(ln):
            # 抵达回复历史，停止收割
            break
        if _REPLY_HEADER.match(ln):
            break
        if _SIG_DELIM.match(ln):
            break
        if _FROM_IPHONE.search(ln):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _split_by_paragraph(text: str) -> list[str]:
    # 双换行优先；单换行作 fallback
    parts = re.split(r"\n\s*\n+", text)
    return [p.strip() for p in parts if p.strip()]


def _split_long(p: str, max_chars: int) -> list[str]:
    """段落太长时按句号/换行/空格次第切。"""
    if len(p) <= max_chars:
        return [p]
    # 按中英句号 / 换行 / 半角逗号兜底
    sentences = re.split(r"(?<=[。！？!?；;\n])\s+", p)
    out: list[str] = []
    buf = ""
    for s in sentences:
        if len(buf) + len(s) + 1 <= max_chars:
            buf = (buf + " " + s).strip() if buf else s
        else:
            if buf:
                out.append(buf)
            if len(s) <= max_chars:
                buf = s
            else:
                # 超长单句：按定长硬切
                for i in range(0, len(s), max_chars):
                    out.append(s[i:i + max_chars])
                buf = ""
    if buf:
        out.append(buf)
    return out


def _with_overlap(chunks: list[str], overlap: int) -> list[str]:
    if overlap <= 0 or len(chunks) <= 1:
        return chunks
    out = [chunks[0]]
    for i in range(1, len(chunks)):
        tail = chunks[i - 1][-overlap:] if len(chunks[i - 1]) > overlap else chunks[i - 1]
        out.append(f"{tail}\n\n{chunks[i]}")
    return out


@dataclass
class EmailDoc:
    """归一化的邮件输入。所有字段缺省都安全。"""
    uid: str
    subject: str = ""
    sender: str = ""
    date: str = ""
    body: str = ""
    folder: str = ""

    def header_block(self) -> str:
        """放在第一个 chunk 顶部的元数据 header。"""
        bits = []
        if self.subject:
            bits.append(f"[Subject] {self.subject}")
        if self.sender:
            bits.append(f"[From] {self.sender}")
        if self.date:
            bits.append(f"[Date] {self.date}")
        if self.folder:
            bits.append(f"[Folder] {self.folder}")
        return "\n".join(bits)


def chunk_email(email: EmailDoc, *, chunk_chars: int | None = None, overlap: int | None = None) -> list[str]:
    """返回 chunk 文本列表；保证至少返回 1 个（哪怕 body 为空，也保留 header）。"""
    chunk_chars = chunk_chars or settings.rag_chunk_chars
    overlap = overlap if overlap is not None else settings.rag_chunk_overlap

    body = strip_quotes_and_signature(email.body or "")
    paragraphs = _split_by_paragraph(body)

    raw_chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        # 把段落拆细（处理超长段落）
        for piece in _split_long(p, chunk_chars):
            if not buf:
                buf = piece
            elif len(buf) + len(piece) + 2 <= chunk_chars:
                buf = f"{buf}\n\n{piece}"
            else:
                raw_chunks.append(buf)
                buf = piece
    if buf:
        raw_chunks.append(buf)

    if not raw_chunks:
        raw_chunks = [""]

    raw_chunks = _with_overlap(raw_chunks, overlap)

    # 第 0 个 chunk 顶部一定带 header
    header = email.header_block()
    if header:
        raw_chunks[0] = f"{header}\n\n{raw_chunks[0]}".strip()
    return raw_chunks
