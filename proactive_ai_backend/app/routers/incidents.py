"""
事故 / 事件查询路由：把 SQLite `incidents` 表暴露为只读 API，
配合 Grafana / 前端运维 Tab 做故障排查。

- GET /v1/incidents              最近 N 条（可按 kind 过滤、可按时间起点过滤）
- GET /v1/incidents/summary      按 kind 聚合的事故数量分布
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from ..memory import incident_counts_by_kind, list_incidents

router = APIRouter(prefix="/v1/incidents", tags=["ops"])


@router.get("")
async def get_incidents(
    limit: int = Query(default=50, ge=1, le=500),
    kind: str | None = Query(default=None, description="如 workflow.timeout / workflow.unknown_tool"),
    since: str | None = Query(default=None, description="ISO 时间 yyyy-mm-ddTHH:MM:SS"),
) -> dict:
    items = await list_incidents(limit=limit, kind=kind, since_iso=since)
    return {"count": len(items), "items": items}


@router.get("/summary")
async def get_incident_summary(
    since: str | None = Query(default=None, description="ISO 时间 yyyy-mm-ddTHH:MM:SS"),
) -> dict:
    return {"groups": await incident_counts_by_kind(since_iso=since)}
