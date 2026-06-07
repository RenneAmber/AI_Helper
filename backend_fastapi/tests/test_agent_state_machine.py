import pytest

from app.agent.chat_agent import execute_agent


@pytest.mark.asyncio
async def test_agent_success_has_completed_state() -> None:
    answer, evidence = await execute_agent("我想挂号")
    assert "识别主题" in answer
    states = [item["state"] for item in evidence if item.get("type") == "state"]
    assert "COMPLETED" in states


@pytest.mark.asyncio
async def test_agent_forced_failure_has_failed_state() -> None:
    answer, evidence = await execute_agent("任意消息", force_fail=True)
    assert "执行失败" in answer
    states = [item["state"] for item in evidence if item.get("type") == "state"]
    assert "TOOL_FAILED" in states
