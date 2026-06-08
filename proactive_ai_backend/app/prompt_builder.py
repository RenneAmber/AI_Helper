"""
Prompt 拼装器：把不同层级的记忆合并成一份给模型的最终 prompt。

层级顺序（从稳定到易变）：
1. system：固定指令
2. semantic：跨会话的事实、偏好、待办（来自 semantic_facts）
3. summary：本 session 的滚动摘要（来自 summaries）
4. recent：本 session 近端原始消息（来自 messages，受 memory_window_messages 限制）
5. user：本次请求的新消息

这样模型既能看到稳定的人格化背景，又能看到最新的高保真上下文，
同时避免 prompt 随对话无限增长。
"""

from __future__ import annotations

import logging

from .config import settings
from .memory import load_history
from .semantic_store import load_latest_summary, search_facts

# RAG 是可选模块；导入失败时降级为「无 RAG」，绝不阻断 prompt 拼装
try:
    from .rag import service as _rag_service  # noqa: F401
    _RAG_AVAILABLE = True
except Exception:  # pragma: no cover - 兜底
    _rag_service = None  # type: ignore[assignment]
    _RAG_AVAILABLE = False

_log = logging.getLogger("prompt_builder")


SYSTEM_PROMPT = (
    "你是用户的个人 AI 助理（产品形态类似 Telegram / WhatsApp / 微信里的智能伙伴），"
    "目标是帮助用户处理日常沟通与工作事务，并在多种生产力工具之间协同：\n"
    "- Email（邮件）：起草、回复、归类、摘要、抓取行动项\n"
    "- Chat（即时通讯）：群聊总结、关键信息提取、回复建议\n"
    "- Calendar（日程）：会议安排、冲突检测、议程草拟、提醒\n"
    "- 其他生产力工具（笔记 / 任务 / 文档 / CRM 等）\n\n"
    "工作准则：\n"
    "1. 优先理解意图：先用一句话确认你要做的事，再开始执行。\n"
    "2. 主动协同：若涉及多个工具（例如把邮件里提到的会议加进日程），主动建议链路。\n"
    "3. 信息不足时只问最关键的一两个问题，不要长串审问。\n"
    "4. 输出可执行：邮件草稿、日程对象、清单等用 markdown 结构（标题/列表/代码块）呈现。\n"
    "5. 隐私优先：不要伪造收件人、时间、链接；不确定的信息明确标注「待确认」。\n"
    "6. 语气贴近用户日常使用的语言（默认中文，简洁友好）。"
)


async def build_prompt(*, session_id: str, user_id: str, user_message: str) -> str:
    facts = await search_facts(user_id=user_id, query=user_message, top_k=settings.semantic_top_k)
    summary = await load_latest_summary(session_id)
    history = await load_history(session_id, limit=settings.memory_window_messages)

    parts: list[str] = [f"system: {SYSTEM_PROMPT}"]

    if facts:
        bullet = "\n".join(f"- [{f['kind']}] {f['content']}" for f in facts)
        parts.append(f"semantic_memory:\n{bullet}")

    # ---- RAG：邮件等长文向量检索；默认关闭，开启后单次失败不影响主流程 ----
    if not settings.rag_enabled:
        try:
            from .metrics import rag_prompt_injections_total
            rag_prompt_injections_total.labels(embedder="-", outcome="disabled").inc()
        except Exception:
            pass
    elif _RAG_AVAILABLE and _rag_service is not None:
        from .metrics import rag_prompt_injections_total
        from .rag.embeddings import get_embedder
        embedder_name = "unknown"
        try:
            embedder = await get_embedder()
            embedder_name = embedder.name
            hits = await _rag_service.search(
                user_id=user_id,
                query=user_message,
                top_k=settings.rag_top_k,
            )
            rag_block = _rag_service.format_as_context(hits)
            if rag_block:
                parts.append(f"retrieved_context:\n{rag_block}")
                rag_prompt_injections_total.labels(embedder=embedder_name, outcome="injected").inc()
                # 成功注入也要服务器日志可见，方便以后以 trace_id grep 排查
                _log.info(
                    "rag_injected",
                    extra={
                        "user_id": user_id,
                        "session_id": session_id,
                        "embedder": embedder_name,
                        "hits": len(hits),
                        "top_score": round(hits[0].score, 4) if hits else None,
                        "chars": len(rag_block),
                    },
                )
            else:
                rag_prompt_injections_total.labels(embedder=embedder_name, outcome="empty").inc()
                _log.info(
                    "rag_empty",
                    extra={"user_id": user_id, "session_id": session_id, "embedder": embedder_name},
                )
        except Exception as exc:  # pragma: no cover - 兜底
            rag_prompt_injections_total.labels(embedder=embedder_name, outcome="error").inc()
            _log.warning("rag_inject_failed", extra={"err": str(exc), "embedder": embedder_name})

    if summary:
        parts.append(f"session_summary:\n{summary['summary']}")

    if history:
        recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-5:])
        parts.append(f"recent_messages:\n{recent}")

    parts.append(f"user: {user_message}")
    return "\n\n".join(parts)
