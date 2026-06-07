"""
Chronic Disease Management Service - AI慢病管家
支持：
- 患者慢病档案管理
- 自动生成后续随访提醒
- 支持多渠道提醒（短信、语音、APP、邮件）
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import json
import uuid
from enum import Enum

app = FastAPI(title="chronic-disease-service")

# ============ 数据模型 ============

class ReminderType(str, Enum):
    CHECKUP = "CHECKUP"
    MEDICATION = "MEDICATION"
    FOLLOWUP = "FOLLOWUP"
    WARNING = "WARNING"

class DeliveryChannel(str, Enum):
    SMS = "SMS"
    VOICE = "VOICE"
    APP = "APP"
    EMAIL = "EMAIL"
    WECHAT = "WECHAT"

class ChronicDiseaseRecord(BaseModel):
    patient_id: str
    disease_name: str
    diagnosis_date: str
    last_checkup_date: Optional[str] = None
    status_data: Optional[Dict[str, Any]] = None
    systolic: Optional[int] = None
    diastolic: Optional[int] = None
    blood_glucose: Optional[float] = None
    hba1c: Optional[float] = None

class ReminderGenerateRequest(BaseModel):
    patient_id: str
    disease_name: str
    check_interval_days: int = 30
    last_checkup_date: Optional[str] = None
    preferred_channels: Optional[List[DeliveryChannel]] = None

class ReminderResponse(BaseModel):
    reminder_id: str
    patient_id: str
    reminder_type: str
    title: str
    description: str
    due_date: str
    delivery_channels: List[str]

class VoiceReminderRequest(BaseModel):
    reminder_id: str
    patient_id: str
    patient_name: str
    patient_phone: str
    disease_name: str
    title: str
    message_text: str

class VoiceReminderResponse(BaseModel):
    status: str
    voice_id: str
    estimated_duration: int
    message: str

# ============ 提醒生成引擎 ============

class ReminderGenerator:
    """根据患者慢病情况生成智能提醒"""
    
    DISEASE_CONFIG = {
        "高血压": {
            "default_interval_days": 30,
            "key_checks": ["血压", "血清肌酐", "尿蛋白"],
            "warning_threshold": {"systolic": 180, "diastolic": 110},
            "medication_reminder_days": 7,
            "urgent_warning": "收缩压>180或舒张压>110，请立即就医",
        },
        "糖尿病": {
            "default_interval_days": 90,
            "key_checks": ["空腹血糖", "HbA1c", "血脂全项", "尿微量白蛋白"],
            "warning_threshold": {"blood_glucose": 11.1, "hba1c": 8.0},
            "medication_reminder_days": 7,
            "urgent_warning": "血糖>11.1 mmol/L, 请立即就医",
        },
        "冠心病": {
            "default_interval_days": 60,
            "key_checks": ["ECG", "肌钙蛋白", "冠状动脉CT", "血脂全项"],
            "warning_threshold": {},
            "medication_reminder_days": 3,
            "urgent_warning": "感到胸痛、胸闷、呼吸困难，请立即拨打120",
        }
    }
    
    @staticmethod
    def generate_reminders(
        patient_id: str,
        disease_name: str,
        last_checkup_date: Optional[str],
        today: str = None
    ) -> List[Dict[str, Any]]:
        """生成提醒列表"""
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")
        
        today_dt = datetime.fromisoformat(today)
        reminders = []
        
        config = ReminderGenerator.DISEASE_CONFIG.get(disease_name, {})
        if not config:
            return []
        
        interval = config.get("default_interval_days", 30)
        
        if last_checkup_date:
            last_checkup_dt = datetime.fromisoformat(last_checkup_date)
            next_checkup_dt = last_checkup_dt + timedelta(days=interval)
        else:
            next_checkup_dt = today_dt + timedelta(days=interval)
        
        reminder_date = next_checkup_dt - timedelta(days=7)
        
        reminders.append({
            "reminder_type": ReminderType.CHECKUP.value,
            "title": f"{disease_name}定期复查",
            "description": f"亲，您{disease_name}已{interval}天未复查，建议进行以下检查: {', '.join(config['key_checks'])}",
            "due_date": reminder_date.strftime("%Y-%m-%d"),
            "key_checks": config['key_checks']
        })
        
        medication_reminder_date = today_dt + timedelta(days=config["medication_reminder_days"])
        reminders.append({
            "reminder_type": ReminderType.MEDICATION.value,
            "title": "提醒取药",
            "description": f"您{disease_name}的长期用药即将耗尽，请及时到医院或药房取药",
            "due_date": medication_reminder_date.strftime("%Y-%m-%d")
        })
        
        education_date = today_dt + timedelta(days=14)
        reminders.append({
            "reminder_type": ReminderType.FOLLOWUP.value,
            "title": f"{disease_name}管理建议",
            "description": _get_disease_management_tips(disease_name),
            "due_date": education_date.strftime("%Y-%m-%d")
        })
        
        return reminders
    
    @staticmethod
    def generate_urgent_warning(
        patient_id: str,
        disease_name: str,
        status_data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """检查是否需要紧急预警"""
        
        config = ReminderGenerator.DISEASE_CONFIG.get(disease_name, {})
        if not config:
            return None
        
        warning = None
        
        if disease_name == "高血压":
            systolic = status_data.get("systolic")
            diastolic = status_data.get("diastolic")
            threshold = config["warning_threshold"]
            
            if systolic and systolic > threshold.get("systolic", 180):
                warning = {
                    "level": "URGENT",
                    "message": f"收缩压{systolic}，{config['urgent_warning']}",
                    "actions": ["立即就医", "拨打120", "测量再次确认"]
                }
            elif diastolic and diastolic > threshold.get("diastolic", 110):
                warning = {
                    "level": "URGENT",
                    "message": f"舒张压{diastolic}，{config['urgent_warning']}",
                    "actions": ["立即就医", "拨打120"]
                }
        
        elif disease_name == "糖尿病":
            blood_glucose = status_data.get("blood_glucose")
            threshold = config["warning_threshold"]
            
            if blood_glucose and blood_glucose > threshold.get("blood_glucose", 11.1):
                warning = {
                    "level": "URGENT",
                    "message": f"血糖{blood_glucose} mmol/L，{config['urgent_warning']}",
                    "actions": ["立即测量血糖", "联系医生", "不要驾车前往医院"]
                }
        
        return warning

# ============ 语音提醒引擎 ============

class VoiceReminderEngine:
    """语音电话提醒处理"""
    
    @staticmethod
    def text_to_voice_script(
        patient_name: str,
        disease_name: str,
        reminder_type: str,
        description: str
    ) -> str:
        """将文字提醒转换为语音脚本"""
        scripts = {
            "CHECKUP": f"您好，{patient_name}患者。这是来自医疗助手的提醒。您{disease_name}已经一段时间未复查。为了更好地管理您的健康，建议您在本周或下周挂号，进行相关检查。医生会根据结果调整治疗方案。",
            
            "MEDICATION": f"您好，{patient_name}患者。这是来自医疗助手的提醒。您{disease_name}的长期用药即将耗尽。建议您预约医疗机构进行配药或前往常用药房取药。不要自行停药。",
            
            "FOLLOWUP": f"您好，{patient_name}患者。为了帮助您更好地管理{disease_name}，我们提供了一些生活方式建议。详情已通过短信发送。请查看APP或短信了解详情。",
            
            "WARNING": f"紧急提醒: {patient_name}患者！您的{disease_name}数据出现异常。{description}。这需要您立即关注。"
        }
        
        return scripts.get(reminder_type, description)

# ============ 辅助函数 ============

def _get_disease_management_tips(disease_name: str) -> str:
    """返回疾病特定的管理和生活方式建议"""
    tips = {
        "高血压": "1. 坚持低盐饮食 2. 适度运动(每周150分钟) 3. 控制体重 4. 定期检测血压 5. 戒烟限酒",
        "糖尿病": "1. 控制碳水化合物摄入 2. 定期监测血糖 3. 适度运动 4. 定期检查眼睛和肾功能 5. 保持足部护理",
        "冠心病": "1. 低脂饮食 2. 避免过度劳累 3. 控制情绪压力 4. 坚持服用心血管用药 5. 定期复查ECG"
    }
    return tips.get(disease_name, "请遵医嘱管理您的慢性病")

# ============ API 端点 ============

@app.post("/chronic/intake", response_model=Dict[str, Any])
def record_chronic_disease(req: ChronicDiseaseRecord):
    """记录患者慢病诊断"""
    record_id = f"CDR-{uuid.uuid4().hex[:12].upper()}"
    
    return {
        "status": "RECORDED",
        "record_id": record_id,
        "patient_id": req.patient_id,
        "disease_name": req.disease_name,
        "diagnosis_date": req.diagnosis_date,
        "message": f"已记录{req.disease_name}的慢病档案，系统将定期提醒复查"
    }

@app.post("/chronic/generate-reminders", response_model=List[ReminderResponse])
def generate_reminders(req: ReminderGenerateRequest):
    """生成患者的后续提醒任务"""
    reminders_data = ReminderGenerator.generate_reminders(
        req.patient_id,
        req.disease_name,
        req.last_checkup_date
    )
    
    responses = []
    for reminder_data in reminders_data:
        reminder_id = f"REM-{uuid.uuid4().hex[:12].upper()}"
        channels = req.preferred_channels or [
            DeliveryChannel.SMS,
            DeliveryChannel.APP,
            DeliveryChannel.EMAIL
        ]
        
        responses.append(ReminderResponse(
            reminder_id=reminder_id,
            patient_id=req.patient_id,
            reminder_type=reminder_data["reminder_type"],
            title=reminder_data["title"],
            description=reminder_data["description"],
            due_date=reminder_data["due_date"],
            delivery_channels=[ch.value for ch in channels]
        ))
    
    return responses

@app.post("/chronic/check-urgent-warning", response_model=Optional[Dict[str, Any]])
def check_urgent_warning(req: ChronicDiseaseRecord):
    """检查患者数据是否需要紧急预警"""
    status_data = req.status_data or {}
    
    if req.systolic:
        status_data["systolic"] = req.systolic
    if req.diastolic:
        status_data["diastolic"] = req.diastolic
    if req.blood_glucose:
        status_data["blood_glucose"] = req.blood_glucose
    if req.hba1c:
        status_data["hba1c"] = req.hba1c
    
    warning = ReminderGenerator.generate_urgent_warning(
        req.patient_id,
        req.disease_name,
        status_data
    )
    
    if warning:
        warning["reminder_id"] = f"URG-{uuid.uuid4().hex[:12].upper()}"
        warning["patient_id"] = req.patient_id
        warning["timestamp"] = datetime.now().isoformat()
    
    return warning

@app.post("/chronic/voice-reminder", response_model=VoiceReminderResponse)
def generate_voice_reminder(req: VoiceReminderRequest):
    """生成语音提醒脚本"""
    
    voice_script = VoiceReminderEngine.text_to_voice_script(
        req.patient_name,
        req.disease_name,
        req.title,
        req.message_text
    )
    
    voice_id = f"VOICE-{uuid.uuid4().hex[:12].upper()}"
    
    return VoiceReminderResponse(
        status="READY_TO_SEND",
        voice_id=voice_id,
        estimated_duration=len(voice_script) // 5,
        message=voice_script
    )

@app.post("/chronic/send-sms", response_model=Dict[str, Any])
def send_sms_reminder(reminder_id: str, patient_phone: str, message: str):
    """发送短信提醒"""
    
    return {
        "status": "SENT",
        "reminder_id": reminder_id,
        "channel": "SMS",
        "recipient": patient_phone,
        "sent_at": datetime.now().isoformat(),
        "mock_message": f"[医疗助手] {message[:50]}..."
    }

@app.post("/chronic/send-app-push", response_model=Dict[str, Any])
def send_app_push(reminder_id: str, patient_id: str, title: str, body: str):
    """发送APP推送通知"""
    
    return {
        "status": "SENT",
        "reminder_id": reminder_id,
        "channel": "APP",
        "patient_id": patient_id,
        "sent_at": datetime.now().isoformat(),
        "notification": {"title": title, "body": body}
    }

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5002)
