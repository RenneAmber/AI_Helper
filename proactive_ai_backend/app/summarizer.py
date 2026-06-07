"""
滚动摘要器：当 session 消息数超过阈值时，把“远端旧消息”压成一段文字摘要。

设计原则：
- 近端 keep_recent 条原样保留，保证近端语义高保真
- 远端通过 provider 推理压缩，避免 prompt 无限增长导致 token 成本和延迟暴涨
- 失败不阻塞主链路：摘要失败仅记录日志，prompt 仍可正常发送
"""

from __future__ import annotations

from .config import settings
from .logging_setup import get_logger
from .memory import count_messages, load_messages_for_summary
from .providers import InferenceRequest, provider
from .semantic_store import load_latest_summary, save_summary

logger = get_logger("summarizer")


async def maybe_summarize(session_id: str, user_id: str) -> None:
    """
    被调用方在每次写入新消息后触发：
    - 如果 session 累计消息数 < trigger_messages，直接返回
    - 否则把 “上次摘要之后 且 不在近端 keep_recent 内” 的消息送去压缩
    """
    total = await count_messages(session_id)
    if total < settings.summary_trigger_messages:
        return

    last = await load_latest_summary(session_id)
    after_id = last["last_message_id"] if last else 0

    pending = await load_messages_for_summary(
        session_id=session_id,
        after_id=after_id,
        keep_recent=settings.summary_keep_recent,
    )
    if not pending:
        return

    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in pending)
    prior = f"已有摘要：\n{last['summary']}\n\n" if last else ""
    prompt = (
        f"{prior}请把下面这段对话用 3-6 行中文概括，保留：用户偏好、未决问题、已承诺事项。"
        f"不要复述模型寒暄。\n\n对话：\n{transcript}"
    )

    try:
        result = await provider.generate(
            InferenceRequest(
                prompt=prompt,
                user_id=user_id,
                session_id=session_id,
                max_tokens=256,
                temperature=0.1,
            )
        )
        await save_summary(
            session_id=session_id,
            last_message_id=pending[-1]["id"],
            summary=result.text.strip(),
        )
        logger.info(
            "summary_saved",
            extra={
                "session_id": session_id,
                "compressed_messages": len(pending),
                "last_message_id": pending[-1]["id"],
            },
        )
    except Exception as exc:  # 摘要失败不阻塞主链路
        logger.warning(
            "summary_failed",
            extra={"session_id": session_id, "error": str(exc)},
        )
