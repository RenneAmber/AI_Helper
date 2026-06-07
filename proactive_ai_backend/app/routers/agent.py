"""
Aido EmailAgent 端点。

POST /v1/chat/agent
  {
    "user_id": "u1",
    "session_id": "s-xxx",
    "message": "帮我看下今天最新的5封邮件",
    "allow_send": false   // 默认 false；当用户在 UI 上确认发送时再传 true
  }

返回：
  {
    "trace_id": "...",
    "text": "...",          // 助手最终回复
    "actions": [             // 本轮执行了哪些工具
      {"name":"email_list_inbox","args":{"limit":5},"result":{"emails":[...]}},
      ...
    ],
    "iterations": 2
  }

POST /v1/chat/agent/stream
  同样的入参，但走 SSE，事件类型见 email_agent.run_email_agent_stream。
"""

from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..agent.email_agent import run_email_agent, run_email_agent_stream
from ..logging_setup import get_logger, get_trace_id
from ..memory import append_message, load_history
from ..streaming import sse_format

router = APIRouter(prefix="/v1/chat", tags=["agent"])
logger = get_logger("router.agent")


class AgentRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8000)
    allow_send: bool = False  # 默认禁止真发邮件，UI 二次确认后再开启
    max_tokens: int = Field(default=1024, ge=64, le=4096)
    history_limit: int = Field(default=12, ge=0, le=50)


class AgentAction(BaseModel):
    name: str
    args: dict
    result: dict | None = None
    error: str | None = None


class AgentResponse(BaseModel):
    trace_id: str
    text: str
    iterations: int
    actions: list[AgentAction]


def _history_to_openai(rows: list[dict]) -> list[dict]:
    """把存到 SQLite 的 messages 转成 OpenAI 协议格式。"""
    out: list[dict] = []
    for r in rows:
        role = r.get("role") or "user"
        if role not in {"user", "assistant", "system"}:
            continue
        content = (r.get("content") or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content})
    return out


@router.post("/agent", response_model=AgentResponse)
async def chat_agent(payload: AgentRequest) -> AgentResponse:
    trace_id = get_trace_id()
    history_rows = await load_history(payload.session_id, limit=payload.history_limit) if payload.history_limit else []
    history = _history_to_openai(history_rows)

    result = await run_email_agent(
        user_message=payload.message,
        history=history,
        allow_send=payload.allow_send,
        max_tokens=payload.max_tokens,
    )

    # 持久化对话（带工具调用摘要标记）
    await append_message(payload.session_id, payload.user_id, "user", payload.message)
    summary_suffix = ""
    if result.actions:
        tools_used = ", ".join(a.name for a in result.actions)
        summary_suffix = f"\n\n[agent.tools_used: {tools_used}]"
    await append_message(payload.session_id, payload.user_id, "assistant", result.text + summary_suffix)

    return AgentResponse(
        trace_id=trace_id,
        text=result.text,
        iterations=result.iterations,
        actions=[AgentAction(**asdict(a)) for a in result.actions],
    )


@router.post("/agent/stream")
async def chat_agent_stream(payload: AgentRequest) -> StreamingResponse:
    """流式 EmailAgent：SSE 事件流，让前端能实时看到 tool_start/tool_end/token。"""
    trace_id = get_trace_id()
    history_rows = await load_history(payload.session_id, limit=payload.history_limit) if payload.history_limit else []
    history = _history_to_openai(history_rows)

    async def event_stream():
        # 头部 meta
        yield sse_format("meta", json.dumps({"trace_id": trace_id}, ensure_ascii=False))

        final_text = ""
        actions_list: list[dict] = []
        iterations = 0
        try:
            async for ev in run_email_agent_stream(
                user_message=payload.message,
                history=history,
                allow_send=payload.allow_send,
                max_tokens=payload.max_tokens,
            ):
                ev_type = ev.pop("type", "msg")
                if ev_type == "done":
                    final_text = ev.get("text", "")
                    actions_list = ev.get("actions", [])
                    iterations = ev.get("iterations", 0)
                yield sse_format(ev_type, json.dumps(ev, ensure_ascii=False, default=str))
        except Exception as exc:
            logger.exception("agent_stream_failed")
            yield sse_format("error", json.dumps({"message": str(exc)}, ensure_ascii=False))
            return

        # —— 持久化对话（流结束后做一次）——
        try:
            await append_message(payload.session_id, payload.user_id, "user", payload.message)
            summary_suffix = ""
            if actions_list:
                tools_used = ", ".join(a.get("name", "") for a in actions_list)
                summary_suffix = f"\n\n[agent.tools_used: {tools_used}]"
            await append_message(
                payload.session_id, payload.user_id, "assistant", (final_text or "") + summary_suffix
            )
        except Exception:
            logger.exception("agent_stream_persist_failed")

        yield sse_format(
            "end",
            json.dumps({"iterations": iterations, "actions_count": len(actions_list)}, ensure_ascii=False),
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 防 nginx 缓冲
        },
    )
