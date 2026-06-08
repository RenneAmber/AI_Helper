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

# ----- Agent 工具调用维度（让 Grafana 能按 tool 名拆分成功率 / 延迟）-----------
# 没有按 user 做 label，避免 label 基数爆炸；user 维度走日志/Tempo 关联。
agent_tool_calls_total = Counter(
    "agent_tool_calls_total",
    "EmailAgent tool invocations",
    ["tool", "status"],  # status: ok | error | blocked
)
agent_tool_duration_seconds = Histogram(
    "agent_tool_duration_seconds",
    "EmailAgent tool execution latency in seconds",
    ["tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
agent_iterations = Histogram(
    "agent_iterations",
    "EmailAgent tool-loop iterations per request",
    buckets=(1, 2, 3, 4, 5, 6, 8, 10),
)

# ----- Workflow 维度（队列深度 / 单步延迟 / 终态分布）-------------------------
workflow_runs_total = Counter(
    "workflow_runs_total",
    "Workflow runs by terminal status",
    ["status"],  # completed | failed | fallback
)
workflow_step_duration_seconds = Histogram(
    "workflow_step_duration_seconds",
    "Workflow per-step execution latency in seconds",
    ["tool", "status"],  # status: ok | error | fallback
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
workflow_queue_depth = Gauge(
    "workflow_queue_depth",
    "Current pending workflow tasks in queue",
)

# ----- Reliability 套件维度（熔断器状态 + 调用结局）---------------------------
reliability_calls_total = Counter(
    "reliability_calls_total",
    "Reliability-wrapped call outcomes",
    ["name", "outcome"],  # outcome: success | failure | fallback
)
reliability_call_attempts = Histogram(
    "reliability_call_attempts",
    "Attempts used per reliability call (1 = success on first try)",
    ["name"],
    buckets=(1, 2, 3, 4, 5),
)
circuit_breaker_state = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state per protected call (0=closed, 1=half_open, 2=open)",
    ["name"],
)

# ----- RAG 检索维度 -----------------------------------------------------
# embedder 标签才是重点：排查问题时一眼能看出 "这走的是 mock 还是 openai"
rag_search_total = Counter(
    "rag_search_total",
    "RAG search invocations",
    ["embedder", "outcome"],            # outcome: hit | miss | error
)
rag_search_hits = Histogram(
    "rag_search_hits",
    "Number of hits returned per RAG search call",
    ["embedder"],
    buckets=(0, 1, 2, 3, 5, 8, 13, 20),
)
rag_ingest_chunks_total = Counter(
    "rag_ingest_chunks_total",
    "RAG chunks ingested",
    ["embedder", "source_type"],
)
rag_prompt_injections_total = Counter(
    "rag_prompt_injections_total",
    "Prompt-builder RAG injection outcomes",
    ["embedder", "outcome"],            # outcome: injected | empty | disabled | error
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
