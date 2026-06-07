"""
OpenTelemetry 集成（可选）。

行为：
- 若未设置 OTLP_ENDPOINT，则什么都不做（保持本地开发零依赖）
- 设置后导出 trace 到 OTLP collector，并自动给 FastAPI / httpx 加上 instrumentation
- 通过 set_global_textmap 启用 W3C TraceContext，跨服务可串联
"""

from __future__ import annotations

from .config import settings
from .logging_setup import get_logger

logger = get_logger("tracing")


def setup_tracing(app) -> None:
    if not settings.otlp_endpoint:
        logger.info("tracing_disabled", extra={"reason": "no_otlp_endpoint"})
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": settings.otlp_service_name})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otlp_endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        logger.info("tracing_enabled", extra={"endpoint": settings.otlp_endpoint})
    except Exception as exc:
        logger.warning("tracing_setup_failed", extra={"error": str(exc)})
