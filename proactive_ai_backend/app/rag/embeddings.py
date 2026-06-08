"""
Embedding Provider 抽象 —— Mock / OpenAI / Azure OpenAI 三选一。

Provider 选择优先级（与 chat provider 完全一致的回落策略，便于运维记忆）：
- settings.rag_embedder 显式指定 → 用它
- 否则：检测到 AZURE_OPENAI_API_KEY → azure
       检测到 OPENAI_API_KEY → openai
       否则 → mock（零依赖，确定性输出，用于本地开发与单测）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Protocol

import numpy as np

from ..config import settings

logger = logging.getLogger("rag.embeddings")


class Embedder(Protocol):
    """统一接口：name 用于落库审计，dim 用于向量校验，embed 一次吃 batch。"""
    name: str
    dim: int

    async def embed(self, texts: list[str]) -> np.ndarray: ...  # shape: (n, dim), float32


# ---------- Mock：确定性、无网络、单测友好 ----------

class MockEmbedder:
    """以 SHA-256 哈希做种子的伪随机投影 —— 同样输入永远同样向量。

    用 (i, j) 双坐标哈希避免维度间相关，保证内积分布大致均匀。
    """

    name = "mock"
    dim = 256

    async def embed(self, texts: list[str]) -> np.ndarray:
        # 异步签名仅为统一接口；实际为 CPU 操作
        vecs = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            base = hashlib.sha256((t or "").encode("utf-8")).digest()
            # 把 32 字节扩展成 dim 长度的 float32
            for j in range(self.dim):
                b = base[j % 32]
                # [-1, 1] 区间均匀映射
                vecs[i, j] = (b - 128.0) / 128.0
            n = np.linalg.norm(vecs[i])
            if n > 0:
                vecs[i] /= n
        return vecs


# ---------- OpenAI / Azure OpenAI 共享实现 ----------

class _OpenAILikeEmbedder:
    """对接 OpenAI Python SDK 的 embeddings.create；Azure / OpenAI 共用，差别仅在 client 构造。"""

    def __init__(self, *, name: str, client, model: str, dim: int) -> None:
        self.name = name
        self._client = client
        self._model = model
        self.dim = dim

    async def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # OpenAI Python SDK 同步 → 用 to_thread
        def _call():
            return self._client.embeddings.create(model=self._model, input=texts)
        # AsyncOpenAI 也有 async embeddings.create；同步包装是为了 Mock/真实切换无感
        resp = await asyncio.to_thread(_call)
        arr = np.array([d.embedding for d in resp.data], dtype=np.float32)
        # 归一化以便后续直接用内积当 cosine
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


def _build_openai_embedder() -> _OpenAILikeEmbedder:
    from openai import OpenAI  # sync client；embeddings 没有强制 async 需求
    api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY missing")
    client = OpenAI(api_key=api_key, base_url=settings.openai_base_url or None)
    model = settings.rag_embedding_model or "text-embedding-3-small"
    # text-embedding-3-small 默认 1536 维
    dim = 1536 if "small" in model else 3072
    return _OpenAILikeEmbedder(name=f"openai:{model}", client=client, model=model, dim=dim)


def _build_azure_embedder() -> _OpenAILikeEmbedder:
    from openai import AzureOpenAI
    api_key = settings.azure_openai_api_key or os.getenv("AZURE_OPENAI_API_KEY")
    endpoint = (
        settings.azure_openai_endpoint
        or settings.azure_openai_api_endpoint
        or os.getenv("AZURE_OPENAI_ENDPOINT")
        or os.getenv("AZURE_OPENAI_API_ENDPOINT")
    )
    api_version = settings.azure_openai_api_version or os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
    if not api_key or not endpoint:
        raise RuntimeError("AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT missing")
    client = AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=endpoint)
    # rag_embedding_model 在 Azure 上是 deployment 名（默认 text-embedding-3-small）
    deployment = settings.rag_embedding_model or "text-embedding-3-small"
    # 维度按底层模型推断；deployment 名不一定带 small/large 字眼时按 small 兜底
    dim = 3072 if "large" in deployment.lower() else 1536
    return _OpenAILikeEmbedder(name=f"azure:{deployment}", client=client, model=deployment, dim=dim)


# ---------- 工厂 + 单例 ----------

_embedder_singleton: Embedder | None = None
_embedder_lock = asyncio.Lock()


async def get_embedder() -> Embedder:
    global _embedder_singleton
    if _embedder_singleton is not None:
        return _embedder_singleton
    async with _embedder_lock:
        if _embedder_singleton is not None:
            return _embedder_singleton

        kind = (settings.rag_embedder or "").strip().lower()
        if not kind:
            # 自动回落
            if (settings.azure_openai_api_key or os.getenv("AZURE_OPENAI_API_KEY")):
                kind = "azure"
            elif (settings.openai_api_key or os.getenv("OPENAI_API_KEY")):
                kind = "openai"
            else:
                kind = "mock"

        try:
            if kind == "azure":
                _embedder_singleton = _build_azure_embedder()
            elif kind == "openai":
                _embedder_singleton = _build_openai_embedder()
            else:
                _embedder_singleton = MockEmbedder()
        except Exception as exc:
            logger.warning("embedder_build_failed_fallback_mock", extra={"kind": kind, "err": str(exc)})
            _embedder_singleton = MockEmbedder()

        logger.info("embedder_ready", extra={"name": _embedder_singleton.name, "dim": _embedder_singleton.dim})
        return _embedder_singleton


def _reset_for_tests() -> None:
    """测试钩子：重置单例，下一次 get_embedder() 会按当前 settings 重新构造。"""
    global _embedder_singleton
    _embedder_singleton = None
