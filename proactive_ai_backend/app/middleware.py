"""
请求级中间件（纯 ASGI 实现）：
- 注入 / 透传 x-trace-id，写到 contextvar 供日志使用
- 记录 Prometheus 指标（计数 + 直方图，带 method/path/status label）
- 响应头回写 x-trace-id 便于客户端排障

注意：
不能用 starlette.middleware.base.BaseHTTPMiddleware，因为它会
把整个响应体缓冲到内存再一次性发出，会破坏 SSE / 大文件等流式响应。
所以这里直接实现 ASGI 三元组接口。
"""

from __future__ import annotations

import time
import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

from .logging_setup import get_logger, set_trace_id
from .metrics import http_request_duration_seconds, http_requests_total

_logger = get_logger("middleware.trace")


class TraceAndMetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 取 / 生成 trace_id
        headers = dict(scope.get("headers") or [])
        incoming_tid = headers.get(b"x-trace-id")
        trace_id = (incoming_tid.decode() if incoming_tid else None) or str(uuid.uuid4())
        set_trace_id(trace_id)

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        start = time.perf_counter()
        status_code = 500

        async def _send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
                # 注入 x-trace-id 响应头
                hdrs = list(message.get("headers") or [])
                hdrs.append((b"x-trace-id", trace_id.encode()))
                message = {**message, "headers": hdrs}
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            latency = time.perf_counter() - start
            try:
                http_request_duration_seconds.labels(method, path).observe(latency)
                http_requests_total.labels(method, path, str(status_code)).inc()
            except Exception:  # pragma: no cover
                pass
            _logger.info(
                "http_request",
                extra={
                    "path": path,
                    "method": method,
                    "status": status_code,
                    "latency_ms": round(latency * 1000.0, 2),
                },
            )
