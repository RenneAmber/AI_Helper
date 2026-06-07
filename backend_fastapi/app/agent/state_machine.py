from dataclasses import dataclass, field
from enum import Enum


class AgentState(str, Enum):
    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    TOOL_RUNNING = "TOOL_RUNNING"
    TOOL_FAILED = "TOOL_FAILED"
    ANSWERING = "ANSWERING"
    COMPLETED = "COMPLETED"


@dataclass
class AgentContext:
    message: str
    force_fail: bool = False
    evidence: list[dict] = field(default_factory=list)
    state: AgentState = AgentState.RECEIVED

    def transition(self, to_state: AgentState, reason: str) -> None:
        self.state = to_state
        self.evidence.append({"type": "state", "state": to_state.value, "reason": reason})
