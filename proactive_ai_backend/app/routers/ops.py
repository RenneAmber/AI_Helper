"""
运维相关路由：
- /healthz  存活探针，仅判断进程是否能响应
- /readyz   就绪探针，附带 provider 与缓存大小
- /metrics  Prometheus 文本格式指标
"""

from __future__ import annotations

from fastapi import APIRouter, Response

from ..cache import response_cache
from ..config import settings
from ..metrics import render_prometheus
from ..providers import provider

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    rag_info: dict = {"enabled": settings.rag_enabled, "top_k": settings.rag_top_k}
    # 只在启用时才加载 embedder（避免探针请求意外初始化远程 SDK）
    if settings.rag_enabled:
        try:
            from ..rag.embeddings import get_embedder
            from ..rag.store import count as rag_count
            embedder = await get_embedder()
            stats = await rag_count(None)
            rag_info.update(
                embedder=embedder.name,
                dim=embedder.dim,
                total_chunks=stats["total"],
                by_source_type=stats["by_source_type"],
            )
        except Exception as exc:
            rag_info["error"] = str(exc)

    # Calendar 后端 + 当前库里事件总数（sqlite/memory 后端直接查；msgraph 不查云端避免认证开销）
    calendar_info: dict = {"backend": settings.calendar_backend}
    try:
        from ..integrations.calendar_factory import backend as cal_backend
        calendar_info["backend_class"] = type(cal_backend).__name__
        if settings.calendar_backend == "sqlite":
            import aiosqlite
            async with aiosqlite.connect(settings.sqlite_path) as db:
                async with db.execute("SELECT COUNT(*) FROM calendar_events") as cur:
                    row = await cur.fetchone()
                    calendar_info["total_events"] = int(row[0]) if row else 0
        elif settings.calendar_backend == "memory":
            calendar_info["total_events"] = sum(len(v) for v in getattr(cal_backend, "_events", {}).values())
    except Exception as exc:
        calendar_info["error"] = str(exc)

    return {
        "status": "ready",
        "provider": provider.name,
        "cache_size": response_cache.size(),
        "rag": rag_info,
        "calendar": calendar_info,
    }


@router.get("/metrics")
async def get_metrics() -> Response:
    payload, content_type = render_prometheus()
    return Response(content=payload, media_type=content_type)
