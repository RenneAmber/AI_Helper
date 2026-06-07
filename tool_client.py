# tool_client.py
from __future__ import annotations

from datetime import datetime
import time
from typing import Any, Dict, Optional

import httpx

class ToolClient:
    """统一封装五个医疗工具服务的 HTTP 调用。

    设计目的：
    - 把重试、超时、错误映射集中在一处，避免业务代码重复写。
    - 对上层暴露稳定方法，降低编排层复杂度。
    
    支持的服务：
    1. register/query/interpret - 原有的三个基础服务
    2. intake_emr - 病例采集与结构化（新）
    3. chronic_disease_* - 慢病管理相关（新）
    """

    def __init__(
        self,
        registration_url: str,
        query_url: str,
        interpret_url: str,
        emr_url: str = "http://localhost:5000/svc",
        chronic_disease_url: str = "http://localhost:5000/svc",
        timeout: float = 10.0,
        retry_max: int = 2,
    ):
        self.registration_url = registration_url.rstrip("/")
        self.query_url = query_url.rstrip("/")
        self.interpret_url = interpret_url.rstrip("/")
        self.emr_url = emr_url.rstrip("/")
        self.chronic_disease_url = chronic_disease_url.rstrip("/")
        self.timeout = timeout
        self.retry_max = retry_max

    def _post_json(self, url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发送 JSON POST 并返回 JSON 对象。

        重试策略：
        - 429/408/5xx：指数退避后重试。
        - 网络异常/超时：指数退避后重试。
        - 其它 4xx：直接抛错（通常是请求参数问题，重试无意义）。
        """
        last_err: Optional[str] = None
        for attempt in range(self.retry_max + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.post(url, json=payload)
                # 这些状态码大概率是临时故障，允许重试。
                if r.status_code in (429, 408) or (500 <= r.status_code < 600):
                    last_err = f"{r.status_code} {r.text}"
                    time.sleep(min(2 ** attempt, 8))
                    continue
                r.raise_for_status()
                # 工具协议约定返回 JSON 对象，供上游做结构化处理。
                return r.json()
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                last_err = f"Timeout/Network: {e}"
                time.sleep(min(2 ** attempt, 8))
            except httpx.HTTPStatusError as e:
                # 非 5xx/429 的 http error 直接抛
                raise RuntimeError(f"HTTP error: {e.response.status_code} {e.response.text}") from e
            except Exception as e:
                raise RuntimeError(f"Tool call failed: {e}") from e
        raise RuntimeError(last_err or "Tool call failed")

    def register(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用挂号服务。"""
        return self._post_json(f"{self.registration_url}/register", payload)

    def query(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用查询服务（医生列表/报告/记录）。"""
        return self._post_json(f"{self.query_url}/query", payload)

    def interpret(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """调用报告解读服务（仅做信息解释，不做诊断）。"""
        return self._post_json(f"{self.interpret_url}/interpret", payload)
    
    # ============ 新增：EMR 相关服务 ============
    
    def intake_emr(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        调用EMR服务进行病例采集与结构化。
        
        输入：
        {
            "patient_id": "P123",
            "chief_complaint": "胸闷、呼吸困难3天",
            "symptoms": ["胸闷", "呼吸困难"],
            "medical_history": ["高血压5年"],
            "current_medications": ["阿司匹林100mg"],
            "vital_signs": {"bp": "160/90", "hr": 85, "temp": 37.0}
        }
        
        输出：
        {
            "status": "STRUCTURED",
            "emr_id": "EMR-xxx",
            "structured_data": {...},
            "severity": {"level": "RED|ORANGE|YELLOW|WHITE", "score": 0-100},
            "recommended_dept": "心内科",
            "recommended_doctor_level": "SPECIALIST"
        }
        """
        return self._post_json(f"{self.emr_url}/intake", payload)
    
    # ============ 新增：慢病管理相关服务 ============
    
    def record_chronic_disease(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        记录患者慢性病诊断（建档）。
        
        输入：
        {
            "patient_id": "P123",
            "disease_name": "高血压",
            "diagnosis_date": "2026-04-06"
        }
        """
        normalized = dict(payload)
        normalized.setdefault("diagnosis_date", datetime.now().date().isoformat())
        return self._post_json(f"{self.chronic_disease_url}/chronic/intake", normalized)
    
    def generate_chronic_reminders(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成患者慢病后续提醒。
        
        输入：
        {
            "patient_id": "P123",
            "disease_name": "高血压",
            "check_interval_days": 30,
            "last_checkup_date": "2026-03-01"
        }
        
        输出：
        {
            "reminders": [
                {
                    "reminder_id": "REM-xxx",
                    "reminder_type": "CHECKUP",
                    "title": "高血压定期复查",
                    "due_date": "2026-04-06"
                },
                ...
            ]
        }
        """
        resp = self._post_json(f"{self.chronic_disease_url}/chronic/generate-reminders", payload)
        if isinstance(resp, list):
            return {"reminders": resp}
        return resp
    
    def check_urgent_warning(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        检查患者的慢病数据是否触发紧急预警。
        返回 None 或预警信息。
        """
        return self._post_json(f"{self.chronic_disease_url}/chronic/check-urgent-warning", payload)
    
    def generate_voice_reminder(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成语音提醒脚本。
        
        输入：
        {
            "reminder_id": "REM-xxx",
            "patient_id": "P123",
            "patient_name": "张三",
            "patient_phone": "13800138000",
            "disease_name": "高血压",
            "title": "定期复查提醒",
            "message_text": "您高血压已30天未复查..."
        }
        """
        return self._post_json(f"{self.chronic_disease_url}/chronic/voice-reminder", payload)