from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..batcher import Batcher
from ..cache import make_key, response_cache
from ..logging_setup import get_logger, get_trace_id
from ..memory import append_message
from ..metrics import metrics
from ..prompt_builder import build_prompt
from ..providers import InferenceRequest, InferenceResponse, provider
from ..reliability import Reliability
from ..streaming import to_sse
from ..summarizer import maybe_summarize

router = APIRouter(prefix="/v1", tags=["inference"])
logger = get_logger("router.inference")
reliability = Reliability(name="provider.generate")


async def _generate_batch(items: list[InferenceRequest]) -> list[InferenceResponse]:
    # 真实场景下应调用 provider.generate_batch()；此处先逐个调用，
    # 主要价值是把上游并发收敛到一个调度点，便于未来切换真正的 batch API。
    return [await provider.generate(item) for item in items]


inference_batcher: Batcher[InferenceRequest, InferenceResponse] = Batcher(_generate_batch)


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=8000)
    use_cache: bool = True
    max_tokens: int = Field(default=1024, ge=16, le=4096)


class ChatResponse(BaseModel):
    trace_id: str
    cached: bool
    text: str
    model: str
    usage: dict


async def _persist_and_summarize(
    session_id: str, user_id: str, user_msg: str, assistant_msg: str
) -> None:
    await append_message(session_id, user_id, "user", user_msg)
    await append_message(session_id, user_id, "assistant", assistant_msg)
    # 摘要是后台压缩动作，失败不影响主链路
    asyncio.create_task(maybe_summarize(session_id=session_id, user_id=user_id))


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest) -> ChatResponse:
    trace_id = get_trace_id()

    prompt = await build_prompt(
        session_id=payload.session_id,
        user_id=payload.user_id,
        user_message=payload.message,
    )

    cache_key = make_key(prompt, payload.user_id)
    if payload.use_cache:
        cached = response_cache.get(cache_key)
        if cached is not None:
            metrics.inc("inference.cache_hit")
            return ChatResponse(
                trace_id=trace_id,
                cached=True,
                text=cached.text,
                model=cached.model,
                usage=cached.usage,
            )

    req = InferenceRequest(
        prompt=prompt,
        user_id=payload.user_id,
        session_id=payload.session_id,
        max_tokens=payload.max_tokens,
    )

    async def _do_call() -> InferenceResponse:
        return await inference_batcher.submit(req)

    async def _fallback(exc: Exception) -> InferenceResponse:
        metrics.inc("inference.fallback")
        return InferenceResponse(
            text="抱歉，当前服务繁忙，已为你保留本次对话，请稍后再试。",
            model="fallback",
            usage={"prompt_tokens": len(prompt), "completion_tokens": 0},
        )

    result = await reliability.call(_do_call, fallback=_fallback)

    if payload.use_cache and result.model != "fallback":
        response_cache.set(cache_key, result)

    await _persist_and_summarize(
        payload.session_id, payload.user_id, payload.message, result.text
    )

    metrics.inc("inference.completed")
    return ChatResponse(
        trace_id=trace_id,
        cached=False,
        text=result.text,
        model=result.model,
        usage=result.usage,
    )


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, request: Request) -> StreamingResponse:
    trace_id = get_trace_id()
    prompt = await build_prompt(
        session_id=payload.session_id,
        user_id=payload.user_id,
        user_message=payload.message,
    )

    req = InferenceRequest(
        prompt=prompt,
        user_id=payload.user_id,
        session_id=payload.session_id,
        max_tokens=payload.max_tokens,
    )

    async def token_stream():
        collected: list[str] = []
        async for token in provider.stream(req):
            collected.append(token)
            yield token
        full = "".join(collected).strip()
        await _persist_and_summarize(payload.session_id, payload.user_id, payload.message, full)
        metrics.inc("inference.stream_completed")

    return StreamingResponse(to_sse(token_stream(), trace_id=trace_id), media_type="text/event-stream")
