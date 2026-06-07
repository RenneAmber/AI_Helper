from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime
from typing import Any

trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "trace_id": trace_id_var.get(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in {"args", "msg", "levelname", "name", "exc_info", "exc_text",
                      "stack_info", "created", "msecs", "relativeCreated", "levelno",
                      "pathname", "filename", "module", "lineno", "funcName",
                      "processName", "process", "threadName", "thread",
                      "message", "taskName"}:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_trace_id(value: str) -> None:
    trace_id_var.set(value)


def get_trace_id() -> str:
    return trace_id_var.get()
