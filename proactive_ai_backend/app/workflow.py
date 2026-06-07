"""
工作流引擎：把“目标 -> 一串工具调用 -> 持久化进度”串起来。

模块职责：
- WorkflowEngine.run() 同步串行执行所有 step，并把每步进度落库 + 写分布式状态
- 兼容两种调度模式：
  1) 同步执行：HTTP 直接调 run()，等待完整结果（短任务场景）
  2) 异步执行：HTTP 把任务塞进 workflow_queue，后台 worker 取出执行（长任务场景）
- 每一步包了 Reliability：超时 / 重试 / 熔断 / fallback，单步失败不会让整个进程崩
- 任何失败都会写 incidents 表，trace_id 可在 /v1/memory/... 类似回放接口里追溯
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .config import settings
from .logging_setup import get_logger, get_trace_id
from .memory import record_incident, save_workflow
from .metrics import metrics
from .reliability import Reliability
from .tools import registry
from .workflow_queue import backend as queue_backend

logger = get_logger("workflow")


class WorkflowError(Exception):
    """请求层错误：参数非法、step 数超限等。"""


class WorkflowEngine:
    """串行、持久化、可观测的多步工作流执行器。"""

    def __init__(self) -> None:
        # 单步执行的可靠性策略；与 provider 调用解耦，单独配置
        self._reliability = Reliability(
            name="workflow.tool",
            retry_max=2,
            timeout_ms=settings.workflow_step_timeout_ms,
        )

    async def run(self, user_id: str, goal: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
        """同步执行整个工作流；返回最终聚合结果。"""
        if not steps:
            raise WorkflowError("steps must not be empty")
        if len(steps) > settings.workflow_max_steps:
            raise WorkflowError("too many steps")

        workflow_id = str(uuid.uuid4())
        context: dict[str, Any] = {}   # 跨 step 共享的中间状态
        results: list[dict[str, Any]] = []  # 每个 step 的结构化结果

        # 先把“正在运行”状态写入 SQLite + 分布式 KV，方便外部立刻能查到
        await save_workflow(workflow_id, user_id, goal, "running", steps, results)
        await queue_backend.set_state(workflow_id, {"status": "running", "results": results})
        metrics.inc("workflow.started")
        trace_id = get_trace_id()

        for index, step in enumerate(steps):
            tool_name = step.get("tool")
            args = step.get("args", {}) or {}
            tool = registry.get(tool_name) if tool_name else None

            # 工具不存在：立刻终止整个流程，并把失败信息留痕
            if tool is None:
                err = {"step": index, "tool": tool_name, "error": "unknown_tool"}
                results.append(err)
                await self._finalize(workflow_id, user_id, goal, "failed", steps, results)
                await record_incident(trace_id, "workflow.unknown_tool", err)
                metrics.inc("workflow.failed")
                return self._summarize(workflow_id, goal, "failed", results)

            try:
                # Reliability.call 会自动应用超时 / 重试 / 熔断 / fallback
                # lambda 默认参数 t/a/c 是为了避免 Python 闭包共享变量的坑
                result = await self._reliability.call(
                    lambda t=tool, a=args, c=context: t(a, c),
                    fallback=self._build_fallback(index, tool_name),
                )
            except asyncio.TimeoutError:
                err = {"step": index, "tool": tool_name, "error": "timeout"}
                results.append(err)
                await self._finalize(workflow_id, user_id, goal, "failed", steps, results)
                await record_incident(trace_id, "workflow.timeout", err)
                metrics.inc("workflow.failed")
                return self._summarize(workflow_id, goal, "failed", results)
            except Exception as exc:  # pragma: no cover
                err = {"step": index, "tool": tool_name, "error": str(exc)}
                results.append(err)
                await self._finalize(workflow_id, user_id, goal, "failed", steps, results)
                await record_incident(trace_id, "workflow.exception", err)
                metrics.inc("workflow.failed")
                return self._summarize(workflow_id, goal, "failed", results)

            # 把本步结果合并到 context，供后续 step 使用（典型链式编排模式）
            context.update(result if isinstance(result, dict) else {"result": result})
            results.append({"step": index, "tool": tool_name, "result": result})

            # 中间态也实时同步，外部可以增量观察执行进度
            await save_workflow(workflow_id, user_id, goal, "running", steps, results)
            await queue_backend.set_state(workflow_id, {"status": "running", "results": results})
            logger.info("workflow_step_done", extra={"workflow_id": workflow_id, "step": index, "tool": tool_name})

        await self._finalize(workflow_id, user_id, goal, "completed", steps, results)
        metrics.inc("workflow.completed")
        return self._summarize(workflow_id, goal, "completed", results)

    async def _finalize(self, workflow_id, user_id, goal, status, steps, results) -> None:
        await save_workflow(workflow_id, user_id, goal, status, steps, results)
        await queue_backend.set_state(workflow_id, {"status": status, "results": results})

    def _build_fallback(self, index: int, tool_name: str | None):
        """生成一个 fallback 协程：熔断 / 重试用尽时返回降级结果，让整个流程继续。"""
        async def _fb(exc: Exception) -> dict[str, Any]:
            metrics.inc("workflow.fallback")
            return {"fallback": True, "step": index, "tool": tool_name, "error": str(exc)}
        return _fb

    @staticmethod
    def _summarize(workflow_id: str, goal: str, status: str, results: list[dict]) -> dict[str, Any]:
        return {
            "workflow_id": workflow_id,
            "goal": goal,
            "status": status,
            "results": results,
        }


engine = WorkflowEngine()


# ----- 异步 worker --------------------------------------------------------- #

async def enqueue_workflow(user_id: str, goal: str, steps: list[dict[str, Any]]) -> str:
    """异步入口：把任务塞队列，立刻返回 workflow_id。"""
    workflow_id = str(uuid.uuid4())
    payload = {"workflow_id": workflow_id, "user_id": user_id, "goal": goal, "steps": steps}
    await queue_backend.set_state(workflow_id, {"status": "queued"})
    await queue_backend.enqueue(payload)
    metrics.inc("workflow.enqueued")
    return workflow_id


async def get_workflow_state(workflow_id: str) -> dict[str, Any] | None:
    """优先取分布式状态；为兼容旧调用方，调用方也可以再查 SQLite。"""
    return await queue_backend.get_state(workflow_id)


async def _worker_loop(worker_id: int) -> None:
    logger.info("worker_started", extra={"worker_id": worker_id})
    while True:
        try:
            task = await queue_backend.dequeue()
            await engine.run(
                user_id=task["user_id"],
                goal=task["goal"],
                steps=task["steps"],
            )
        except asyncio.CancelledError:
            logger.info("worker_cancelled", extra={"worker_id": worker_id})
            raise
        except Exception as exc:  # pragma: no cover
            logger.warning("worker_error", extra={"worker_id": worker_id, "error": str(exc)})
            await asyncio.sleep(0.5)


async def start_workers(n: int = 1) -> list[asyncio.Task]:
    """在 FastAPI startup 中调用：起 n 个后台 worker。"""
    return [asyncio.create_task(_worker_loop(i)) for i in range(n)]
