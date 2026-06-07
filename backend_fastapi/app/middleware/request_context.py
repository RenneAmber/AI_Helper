import time
import uuid

from fastapi import Request

from ..core.logging_setup import get_logger
from ..core.trace import set_trace_id

logger = get_logger("request")


async def add_trace_and_logs(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
    set_trace_id(trace_id)

    start = time.time()
    response = await call_next(request)
    latency_ms = int((time.time() - start) * 1000)

    response.headers["x-trace-id"] = trace_id
    logger.info(
        "request.completed",
        extra={
            "extra_fields": {
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            }
        },
    )
    return response
