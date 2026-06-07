from fastapi import FastAPI
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

app = FastAPI(title="registration-service")

class RegisterReq(BaseModel):
    patient_id: str
    hospital: str
    department: str
    doctor: Optional[str] = None
    preferred_time: str
    time_window_hours: int = 4

class RegisterResp(BaseModel):
    status: str
    registration_id: str
    scheduled_time: str
    location: str
    notes: str

@app.post("/register", response_model=RegisterResp)
def register(req: RegisterReq):
    # mock: 实际可接排班/号源系统
    return RegisterResp(
        status="CONFIRMED",
        registration_id="R202604020001",
        scheduled_time=req.preferred_time,
        location="门诊楼2层A区",
        notes="请提前30分钟到院取号"
    )