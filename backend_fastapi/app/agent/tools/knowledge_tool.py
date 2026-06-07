from datetime import UTC, datetime


def run_knowledge_lookup(message: str) -> dict:
    # Real tool: deterministic knowledge lookup that can be traced and replayed.
    result = {
        "tool": "knowledge_lookup",
        "input": message,
        "matched_topics": [topic for topic in ["高血压", "糖尿病", "挂号", "报告"] if topic in message],
        "timestamp": datetime.now(UTC).isoformat(),
    }
    return result
