from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    stream: bool = False
    force_fail: bool = False


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    trace_id: str
    evidence: list[dict]


class ReplayResponse(BaseModel):
    trace_id: str
    audit_logs: list[dict]
    conversations: list[dict]


class HealthResponse(BaseModel):
    status: str
