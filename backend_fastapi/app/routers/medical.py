from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/internal/medical", tags=["medical"])


class MedicalTriageRequest(BaseModel):
    complaint: str = Field(min_length=1, max_length=2000)


@router.post("/triage")
async def medical_triage(payload: MedicalTriageRequest) -> dict:
    # Medical-specific entrypoint in dedicated file.
    level = "YELLOW"
    if any(word in payload.complaint for word in ["胸痛", "呼吸困难", "晕厥"]):
        level = "RED"
    return {
        "triage_level": level,
        "reason": "症状关键词匹配",
    }
