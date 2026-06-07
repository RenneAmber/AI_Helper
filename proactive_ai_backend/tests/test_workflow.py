from __future__ import annotations

import pytest

from app.memory import init_db
from app.workflow import engine


@pytest.mark.asyncio
async def test_workflow_runs_sequential_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "wf.db"))
    await init_db()
    result = await engine.run(
        user_id="u1",
        goal="weekly digest",
        steps=[
            {"tool": "fetch_notes", "args": {"week": "2026-W23"}},
            {"tool": "summarize", "args": {"style": "bullet"}},
            {"tool": "extract_actions", "args": {}},
        ],
    )
    assert result["status"] == "completed"
    assert len(result["results"]) == 3
    last = result["results"][-1]["result"]
    assert "actions" in last


@pytest.mark.asyncio
async def test_workflow_unknown_tool_fails():
    result = await engine.run(
        user_id="u1",
        goal="bad",
        steps=[{"tool": "nope", "args": {}}],
    )
    assert result["status"] == "failed"
