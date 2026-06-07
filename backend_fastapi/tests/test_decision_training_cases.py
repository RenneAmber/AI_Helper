import pytest

from app.decision.tools import fake_retriever
from app.decision.nodes import build_decision_record_node


class DummyRepo:
    async def add_event(self, **kwargs):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,expected_rec",
    [
        ("一个人适合住宾馆吗？", "PROCEED"),
        ("我住在悉尼，是否值得去 New zealand 玩？", "PROCEED_WITH_GUARDRAILS"),
        ("Should we enable feature flag X for 10% users this week?", "PROCEED_WITH_GUARDRAILS"),
        ("是否应该重构旧系统的菜单层？", "NEEDS_REVIEW"),
    ],
)
async def test_training_case_driven_decision_output(question: str, expected_rec: str) -> None:
    evidence = await fake_retriever(question)

    state = {
        "decision_id": "D-test-001",
        "normalized": {
            "question": question,
            "domain": "engineering",
            "intentMode": "decision",
        },
        "gates": {"evidence_quality": {"result": "pass"}},
        "evidence_pack": {
            "items": [
                {
                    "evidenceId": "E1",
                    "source": {"title": evidence["title"]},
                    "quote": evidence["quote"],
                    "tags": evidence["tags"],
                }
            ],
            "conflicts": [],
        },
    }

    out = await build_decision_record_node(state, DummyRepo())
    decision = out["decision_out"]

    assert decision["recommendation"] == expected_rec
    assert 0 <= decision["confidence"] <= 1
    assert len(decision["rationale"]) >= 2
    assert "训练样例" in decision["rationale"][1]["text"]
