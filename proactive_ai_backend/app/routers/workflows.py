"""
工作流路由：
- POST /v1/workflows           同步执行整个流程，返回最终结果（短任务）
- POST /v1/workflows/async     异步入队，立即返回 workflow_id（长任务）
- GET  /v1/workflows/{id}      读取持久化状态
- GET  /v1/workflows/tools     列出已注册的工具名
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..memory import load_workflow
from ..tools import registry
from ..workflow import WorkflowError, engine, enqueue_workflow, get_workflow_state

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])


class WorkflowStep(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    args: dict = Field(default_factory=dict)


class WorkflowCreate(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=512)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=20)


@router.get("/tools")
async def list_tools() -> dict:
    return {"tools": registry.names()}


@router.post("")
async def create_workflow(payload: WorkflowCreate) -> dict:
    try:
        return await engine.run(
            user_id=payload.user_id,
            goal=payload.goal,
            steps=[s.model_dump() for s in payload.steps],
        )
    except WorkflowError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/async")
async def create_workflow_async(payload: WorkflowCreate) -> dict:
    workflow_id = await enqueue_workflow(
        user_id=payload.user_id,
        goal=payload.goal,
        steps=[s.model_dump() for s in payload.steps],
    )
    return {"workflow_id": workflow_id, "status": "queued"}


@router.get("/{workflow_id}")
async def get_workflow(workflow_id: str) -> dict:
    # 先查分布式状态（增量进度），再回退到 SQLite 完整记录
    state = await get_workflow_state(workflow_id)
    record = await load_workflow(workflow_id)
    if record is None and state is None:
        raise HTTPException(status_code=404, detail="workflow_not_found")
    return {"state": state, "record": record}
