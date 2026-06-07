"""
运维相关路由：
- /healthz  存活探针，仅判断进程是否能响应
- /readyz   就绪探针，附带 provider 与缓存大小
- /metrics  Prometheus 文本格式指标
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..cache import response_cache
from ..metrics import render_prometheus
from ..providers import provider

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    return {
        "status": "ready",
        "provider": provider.name,
        "cache_size": response_cache.size(),
    }


@router.get("/metrics")
async def get_metrics() -> Response:
    payload, content_type = render_prometheus()
    return Response(content=payload, media_type=content_type)
