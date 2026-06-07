from __future__ import annotations

from typing import AsyncIterator


def sse_format(event: str, data: str) -> bytes:
    """Format a Server-Sent Event frame."""
    lines = [f"event: {event}"]
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    lines.append("")
    return ("\n".join(lines) + "\n").encode("utf-8")


async def to_sse(
    token_stream: AsyncIterator[str],
    *,
    trace_id: str,
) -> AsyncIterator[bytes]:
    yield sse_format("meta", f'{{"trace_id":"{trace_id}"}}')
    async for token in token_stream:
        yield sse_format("token", token)
    yield sse_format("done", "{}")
