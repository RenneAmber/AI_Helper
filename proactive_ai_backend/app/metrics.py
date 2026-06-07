"""
Prometheus 指标暴露层。

- 全部指标采用 prometheus_client 提供的 Counter / Histogram / Gauge
- 通过 /metrics 接口以 text exposition 格式暴露，可被 Prometheus 抓取
- 为了兼容旧代码里的 `metrics.inc("xxx")` / `metrics.observe("xxx", v)` 调用，
  这里提供一个适配层：未注册过的指标按命名规则懒注册成对应类型。
"""

from __future__ import annotations

import re
import threading
from typing import Dict

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

_NAME_RE = re.compile(r"[^a-zA-Z0-9_]")


def _normalize(name: str) -> str:
    return _NAME_RE.sub("_", name)


# 显式声明几个高频核心指标，命名贴合 Prom 习惯
http_requests_total = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds", "HTTP request latency in seconds", ["method", "path"]
)
inference_requests_total = Counter(
    "inference_requests_total", "Inference requests", ["kind"]
)


class _MetricsFacade:
    """兼容旧 API: inc / observe / set_gauge。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Counter] = {}
        self._histograms: Dict[str, Histogram] = {}
        self._gauges: Dict[str, Gauge] = {}

    def inc(self, name: str, value: float = 1.0) -> None:
        key = _normalize(name)
        with self._lock:
            counter = self._counters.get(key)
            if counter is None:
                counter = Counter(key, f"Counter {name}")
                self._counters[key] = counter
        counter.inc(value)

    def observe(self, name: str, value: float) -> None:
        key = _normalize(name)
        with self._lock:
            hist = self._histograms.get(key)
            if hist is None:
                hist = Histogram(key, f"Histogram {name}")
                self._histograms[key] = hist
        hist.observe(value)

    def set_gauge(self, name: str, value: float) -> None:
        key = _normalize(name)
        with self._lock:
            gauge = self._gauges.get(key)
            if gauge is None:
                gauge = Gauge(key, f"Gauge {name}")
                self._gauges[key] = gauge
        gauge.set(value)


metrics = _MetricsFacade()


def render_prometheus() -> tuple[bytes, str]:
    """返回 (payload, content_type)，供 /metrics 接口直接使用。"""
    return generate_latest(), CONTENT_TYPE_LATEST
