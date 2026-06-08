"""
模型 Provider 抽象层。

- Provider 通过 PROVIDER 环境变量选择：mock | openai | anthropic
- 所有 Provider 调用都包了 TokenBucket 限速，避免突发把账号配额打爆
- mock 不需要任何依赖；openai/anthropic 需要对应 SDK 与 API Key
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from .config import settings
from .logging_setup import get_logger

logger = get_logger("providers")


# ----- 限速：令牌桶 --------------------------------------------------------- #

class TokenBucket:
    """
    经典令牌桶：
      - 容量 capacity（瞬时可借走的最大令牌数）
      - 以 refill_per_sec 的速率匀速补充
      - acquire(n) 等到桶里有足够令牌再返回
    """

    def __init__(self, capacity: int, refill_per_sec: float) -> None:
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int) -> None:
        if n <= 0:
            return
        # 不允许单次请求大于桶容量，否则会永远等不到
        n = min(n, self.capacity)
        while True:
            async with self._lock:
                now = time.monotonic()
                delta = now - self._last
                self._tokens = min(self.capacity, self._tokens + delta * self.refill_per_sec)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                missing = n - self._tokens
                wait = missing / self.refill_per_sec
            await asyncio.sleep(wait)


# ----- Provider 协议 -------------------------------------------------------- #

@dataclass
class InferenceRequest:
    prompt: str
    user_id: str
    session_id: str
    max_tokens: int = 1024
    # 可选；GPT-5 等模型已废弃此参数，传 None 时调用方不会附带
    temperature: float | None = None


def _maybe_temperature(req: "InferenceRequest") -> dict:
    """根据 InferenceRequest.temperature 是否为 None 决定要不要传给 SDK。"""
    return {"temperature": req.temperature} if req.temperature is not None else {}


# GPT-5 / o1 / o3 / o4 等 "reasoning" 家族在 chat.completions 接口里
# 只接受 max_completion_tokens；老的 gpt-4 / gpt-4o 仍只认 max_tokens。
# 同时传两个会被服务端拒。这里按 deployment / model 名字段路由一下。
_NEW_TOKEN_PARAM_PATTERNS = ("gpt-5", "gpt5", "o1", "o3", "o4")


def _token_kwargs(model_or_deployment: str, n: int) -> dict:
    """根据模型/部署名挑选正确的 token 上限参数名。"""
    name = (model_or_deployment or "").lower()
    if any(p in name for p in _NEW_TOKEN_PARAM_PATTERNS):
        return {"max_completion_tokens": n}
    return {"max_tokens": n}


@dataclass
class InferenceResponse:
    text: str
    model: str
    usage: dict


class ModelProvider(Protocol):
    name: str

    async def generate(self, req: InferenceRequest) -> InferenceResponse: ...
    async def stream(self, req: InferenceRequest) -> AsyncIterator[str]: ...


def _estimate_tokens(text: str) -> int:
    # 简化估算：1 token ≈ 4 字符，足以用于限速近似
    return max(1, len(text) // 4)


# ----- Mock Provider -------------------------------------------------------- #

class MockProvider:
    name = "mock"

    async def generate(self, req: InferenceRequest) -> InferenceResponse:
        await asyncio.sleep(random.uniform(0.02, 0.08))
        text = f"[mock:{req.user_id}] {req.prompt[::-1][:64]}"
        return InferenceResponse(
            text=text,
            model="mock-1",
            usage={"prompt_tokens": _estimate_tokens(req.prompt),
                   "completion_tokens": _estimate_tokens(text)},
        )

    async def stream(self, req: InferenceRequest) -> AsyncIterator[str]:
        full = (await self.generate(req)).text
        for token in full.split():
            await asyncio.sleep(0.01)
            yield token + " "


# ----- OpenAI Provider ------------------------------------------------------ #

class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        from openai import AsyncOpenAI  # 延迟导入，避免无 key 环境启动失败
        api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY missing")
        kwargs = {"api_key": api_key}
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        self._client = AsyncOpenAI(**kwargs)
        self._model = settings.openai_model
        self._bucket = TokenBucket(
            capacity=settings.provider_burst_tokens,
            refill_per_sec=settings.provider_tokens_per_minute / 60.0,
        )

    async def generate(self, req: InferenceRequest) -> InferenceResponse:
        await self._bucket.acquire(_estimate_tokens(req.prompt) + req.max_tokens)
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": req.prompt}],
            **_token_kwargs(self._model, req.max_tokens),
            **_maybe_temperature(req),
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = getattr(resp, "usage", None)
        return InferenceResponse(
            text=text,
            model=resp.model,
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            },
        )

    async def stream(self, req: InferenceRequest) -> AsyncIterator[str]:
        await self._bucket.acquire(_estimate_tokens(req.prompt) + req.max_tokens)
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": req.prompt}],
            **_token_kwargs(self._model, req.max_tokens),
            stream=True,
            **_maybe_temperature(req),
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
    ) -> Any:
        """一次同步调用，返回 OpenAI ChatCompletion 原始响应，
        供上层 agent 循环看是 tool_calls 还是 finish。"""
        await self._bucket.acquire(max_tokens)
        return await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            **_token_kwargs(self._model, max_tokens),
        )

    async def chat_with_tools_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
    ) -> AsyncIterator[Any]:
        """流式 tool-calling：直接吐 OpenAI 原始 chunk 给上层组装。"""
        await self._bucket.acquire(max_tokens)
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            **_token_kwargs(self._model, max_tokens),
            stream=True,
        )
        async for chunk in stream:
            yield chunk


# ----- Azure OpenAI Provider ------------------------------------------------ #
# 与根目录 app.py 共用同一套环境变量：
#   AZURE_OPENAI_API_KEY
#   AZURE_OPENAI_ENDPOINT  (或 AZURE_OPENAI_API_ENDPOINT，二者任一即可)
#   AZURE_OPENAI_API_VERSION  (默认 2025-01-01-preview)
#   AZURE_OPENAI_DEPLOYMENT   (chat 部署名，例如 gpt-5.4-mini)
# 实际向 Azure 发请求时 `model` 字段填的是部署名 (deployment)，不是模型名。

class AzureOpenAIProvider:
    name = "azure"

    def __init__(self) -> None:
        from openai import AsyncAzureOpenAI  # 延迟导入

        api_key = settings.azure_openai_api_key or os.getenv("AZURE_OPENAI_API_KEY")
        endpoint = (
            settings.azure_openai_endpoint
            or settings.azure_openai_api_endpoint
            or os.getenv("AZURE_OPENAI_ENDPOINT")
            or os.getenv("AZURE_OPENAI_API_ENDPOINT")
        )
        api_version = settings.azure_openai_api_version or os.getenv(
            "AZURE_OPENAI_API_VERSION", "2025-01-01-preview"
        )
        deployment = settings.azure_openai_deployment or os.getenv(
            "AZURE_OPENAI_DEPLOYMENT", "gpt-5.4-mini"
        )

        if not api_key or not endpoint:
            raise RuntimeError("AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT missing")

        self._client = AsyncAzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint,
        )
        self._deployment = deployment
        self._bucket = TokenBucket(
            capacity=settings.provider_burst_tokens,
            refill_per_sec=settings.provider_tokens_per_minute / 60.0,
        )
        logger.info(
            "azure_openai_provider_ready",
            extra={"endpoint": endpoint, "deployment": deployment, "api_version": api_version},
        )

    async def generate(self, req: InferenceRequest) -> InferenceResponse:
        await self._bucket.acquire(_estimate_tokens(req.prompt) + req.max_tokens)
        resp = await self._client.chat.completions.create(
            model=self._deployment,  # Azure 这里传 deployment 名
            messages=[{"role": "user", "content": req.prompt}],
            **_token_kwargs(self._deployment, req.max_tokens),
            **_maybe_temperature(req),
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        usage = getattr(resp, "usage", None)
        return InferenceResponse(
            text=text,
            model=getattr(resp, "model", self._deployment),
            usage={
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            },
        )

    async def stream(self, req: InferenceRequest) -> AsyncIterator[str]:
        await self._bucket.acquire(_estimate_tokens(req.prompt) + req.max_tokens)
        stream = await self._client.chat.completions.create(
            model=self._deployment,
            messages=[{"role": "user", "content": req.prompt}],
            **_token_kwargs(self._deployment, req.max_tokens),
            stream=True,
            **_maybe_temperature(req),
        )
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
    ) -> Any:
        await self._bucket.acquire(max_tokens)
        return await self._client.chat.completions.create(
            model=self._deployment,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            **_token_kwargs(self._deployment, max_tokens),
        )

    async def chat_with_tools_stream(
        self,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 1024,
    ) -> AsyncIterator[Any]:
        await self._bucket.acquire(max_tokens)
        stream = await self._client.chat.completions.create(
            model=self._deployment,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
            **_token_kwargs(self._deployment, max_tokens),
            stream=True,
        )
        async for chunk in stream:
            yield chunk


# ----- Anthropic Provider --------------------------------------------------- #

class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY"),
        )
        self._model = settings.anthropic_model
        self._bucket = TokenBucket(
            capacity=settings.provider_burst_tokens,
            refill_per_sec=settings.provider_tokens_per_minute / 60.0,
        )

    async def generate(self, req: InferenceRequest) -> InferenceResponse:
        await self._bucket.acquire(_estimate_tokens(req.prompt) + req.max_tokens)
        msg = await self._client.messages.create(
            model=self._model,
            max_tokens=req.max_tokens,
            messages=[{"role": "user", "content": req.prompt}],
            **_maybe_temperature(req),
        )
        text = "".join(block.text for block in msg.content if getattr(block, "type", None) == "text")
        usage = getattr(msg, "usage", None)
        return InferenceResponse(
            text=text,
            model=msg.model,
            usage={
                "prompt_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
            },
        )

    async def stream(self, req: InferenceRequest) -> AsyncIterator[str]:
        await self._bucket.acquire(_estimate_tokens(req.prompt) + req.max_tokens)
        async with self._client.messages.stream(
            model=self._model,
            max_tokens=req.max_tokens,
            messages=[{"role": "user", "content": req.prompt}],
            **_maybe_temperature(req),
        ) as stream:
            async for text in stream.text_stream:
                yield text


# ----- Factory -------------------------------------------------------------- #

def build_provider() -> ModelProvider:
    # 1) 显式 PROVIDER 环境变量优先
    name = (os.getenv("PROVIDER") or settings.provider).lower()
    # 2) 未显式指定时，若检测到 Azure key 自动选 azure
    if name == "mock":
        if settings.azure_openai_api_key or os.getenv("AZURE_OPENAI_API_KEY"):
            name = "azure"
        elif settings.openai_api_key or os.getenv("OPENAI_API_KEY"):
            name = "openai"
        elif settings.anthropic_api_key or os.getenv("ANTHROPIC_API_KEY"):
            name = "anthropic"

    try:
        if name == "azure":
            return AzureOpenAIProvider()
        if name == "openai":
            return OpenAIProvider()
        if name == "anthropic":
            return AnthropicProvider()
    except Exception as exc:
        # 任意 provider 初始化失败都回退到 mock，保证服务能起来
        logger.warning("provider_init_failed_fallback_mock", extra={"provider": name, "error": str(exc)})
    return MockProvider()


provider: ModelProvider = build_provider()
