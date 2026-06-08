"""
应用入口：组装 FastAPI、注册路由、初始化 DB / tracing / 后台 worker、挂载静态 UI。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .logging_setup import configure_logging, get_logger
from .memory import init_db
from .middleware import TraceAndMetricsMiddleware
from .routers.inference import router as inference_router
from .routers.memory import router as memory_router
from .routers.ops import router as ops_router
from .routers.workflows import router as workflows_router
from .routers.email import router as email_router
from .routers.agent import router as agent_router
from .routers.incidents import router as incidents_router
from .routers.rag import router as rag_router
from .routers.calendar import router as calendar_router
from .routers.auth_msgraph import router as auth_msgraph_router
from .tracing import setup_tracing
from .workflow import start_workers

configure_logging()
logger = get_logger("main")

app = FastAPI(title=settings.app_name)
app.add_middleware(TraceAndMetricsMiddleware)
app.include_router(ops_router)
app.include_router(inference_router)
app.include_router(workflows_router)
app.include_router(memory_router)
app.include_router(email_router)
app.include_router(agent_router)
app.include_router(incidents_router)
app.include_router(rag_router)
app.include_router(calendar_router)
app.include_router(auth_msgraph_router)

# OpenTelemetry：仅在配置了 OTLP_ENDPOINT 时启用
setup_tracing(app)

# 挂载静态 UI（无需前端构建）
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_static_dir), html=True), name="ui")

_worker_tasks = []


@app.on_event("startup")
async def _startup() -> None:
    await init_db()
    workers = int(os.getenv("WORKFLOW_WORKERS", "1"))
    if workers > 0:
        _worker_tasks.extend(await start_workers(workers))
    logger.info(
        "app_started",
        extra={"provider": settings.provider, "env": settings.app_env, "workers": workers},
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    for t in _worker_tasks:
        t.cancel()
    logger.info("app_stopped")
