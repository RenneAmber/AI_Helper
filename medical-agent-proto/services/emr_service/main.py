"""
EMR Service - 电子病历获取与结构化
支持：
- 病例intake：收集初诊信息
- 自动生成结构化电子病历
- 智能计算严重程度（白黄橙红四级）
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
from datetime import datetime
import json
import uuid
from enum import Enum

app = FastAPI(title="emr-service")

# ============ 数据模型 ============

class SeverityLevel(str, Enum):
    WHITE = "WHITE"    # 正常、轻微
    YELLOW = "YELLOW"  # 中等
    ORANGE = "ORANGE"  # 严重
    RED = "RED"        # 紧急

class VitalSigns(BaseModel):
    bp: Optional[str] = None        # "120/80"
    hr: Optional[int] = None        # 心率 bpm
    temp: Optional[float] = None    # 体温 °C
    rr: Optional[int] = None        # 呼吸频率
    o2_sat: Optional[float] = None  # 血氧饱和度 %

class EMRIntakeRequest(BaseModel):
    patient_id: str
    chief_complaint: str           # 主诉，如"胸闷、呼吸困难3天"
    symptoms: Optional[List[str]] = None      # ["胸闷", "呼吸困难"]
    medical_history: Optional[List[str]] = None  # ["高血压5年", "糖尿病"]
    current_medications: Optional[List[str]] = None  # ["阿司匹林100mg qd"]
    vital_signs: Optional[VitalSigns] = None
    allergy_history: Optional[str] = None
    recent_travel: Optional[str] = None
    exposure_history: Optional[str] = None

class EMRIntakeResponse(BaseModel):
    status: str  # STRUCTURED
    emr_id: str
    structured_data: Dict[str, Any]
    severity: Dict[str, Any]
    recommended_dept: str
    recommended_doctor_level: str
    suggested_tests: List[str]

# ============ 严重程度评估引擎 ============

class SeverityAssessor:
    """
    根据症状、体征、病史综合评估患者严重程度
    规则：
    - WHITE (0-20)：无症状或轻微症状，无生命危险
    - YELLOW (21-50)：中等症状，需要一般医学处理
    - ORANGE (51-80)：严重症状或异常体征，需要急医学处理
    - RED (81-100)：紧急、危及生命，需要立即处理
    """
    
    # 红色（紧急）指标
    RED_KEYWORDS = {
        "symptoms": [
            "胸痛", "胸闷", "呼吸困难", "心梗征兆",
            "脑卒中", "意识模糊", "昏迷",
            "大量出血", "腹部撕裂样疼痛",
            "严重头痛", "颈项强直"
        ],
        "vital_ranges": {
            "HR_low": 40,          # 心率 < 40
            "HR_high": 120,        # 心率 > 120
            "BP_systolic_high": 180,  # 收缩压 > 180
            "BP_systolic_low": 90,    # 收缩压 < 90
            "temp_high": 39.5,     # 体温 > 39.5°C
            "o2_sat_low": 93,      # 血氧 < 93%
        }
    }
    
    # 橙色（严重）指标
    ORANGE_KEYWORDS = {
        "symptoms": [
            "严重腹泻", "持续呕吐", "重度头晕目眩",
            "严重咳嗽、咳血", "持续高烧",
            "明显脂肪肝症状", "腹部肿胀压痛"
        ],
        "vital_ranges": {
            "HR": (100, 119),
            "temp": (38.5, 39.4),
        }
    }
    
    # 黄色（中等）指标
    YELLOW_KEYWORDS = {
        "symptoms": [
            "轻度腹泻", "偶发呕吐", "轻度头晕",
            "干咳", "低烧", "轻度皮疹"
        ]
    }
    
    @staticmethod
    def calculate_severity(
        chief_complaint: str,
        symptoms: List[str],
        medical_history: List[str],
        vital_signs: Optional[VitalSigns]
    ) -> Dict[str, Any]:
        """计算严重程度，返回 {level, score, reason}"""
        
        score = 0
        reasons = []
        
        # 1. 症状分析
        all_symptoms = (symptoms or []) + [chief_complaint]
        symptoms_lower = " ".join(s.lower() for s in all_symptoms)
        
        # 检查红色症状
        for keyword in SeverityAssessor.RED_KEYWORDS["symptoms"]:
            if keyword in symptoms_lower:
                score = max(score, 85)
                reasons.append(f"红色症状: {keyword}")
        
        # 检查橙色症状
        if score < 85:
            for keyword in SeverityAssessor.ORANGE_KEYWORDS["symptoms"]:
                if keyword in symptoms_lower:
                    score = max(score, 65)
                    reasons.append(f"橙色症状: {keyword}")
        
        # 检查黄色症状
        if score < 65:
            for keyword in SeverityAssessor.YELLOW_KEYWORDS["symptoms"]:
                if keyword in symptoms_lower:
                    score = max(score, 35)
                    reasons.append(f"黄色症状: {keyword}")
        
        # 2. 体征分析
        if vital_signs:
            vs = vital_signs
            
            # 危险的生命体征
            if vs.hr is not None:
                if vs.hr < 40 or vs.hr > 120:
                    score = max(score, 75)
                    reasons.append(f"异常心率: {vs.hr} bpm")
            
            if vs.bp:  # "120/80"
                try:
                    systolic, diastolic = map(int, vs.bp.split("/"))
                    if systolic > 180 or systolic < 90:
                        score = max(score, 70)
                        reasons.append(f"异常血压: {vs.bp}")
                    elif systolic > 160 or systolic < 100:
                        score = max(score, 40)
                        reasons.append(f"血压偏离正常: {vs.bp}")
                except:
                    pass
            
            if vs.temp is not None:
                if vs.temp > 39.5:
                    score = max(score, 70)
                    reasons.append(f"高热: {vs.temp}°C")
                elif vs.temp > 38.5:
                    score = max(score, 45)
                    reasons.append(f"发热: {vs.temp}°C")
            
            if vs.o2_sat is not None:
                if vs.o2_sat < 93:
                    score = max(score, 80)
                    reasons.append(f"血氧偏低: {vs.o2_sat}%")
                elif vs.o2_sat < 95:
                    score = max(score, 50)
                    reasons.append(f"血氧略低: {vs.o2_sat}%")
        
        # 3. 病史风险分析
        history_lower = " ".join(h.lower() for h in (medical_history or []))
        high_risk_conditions = ["心梗", "脑卒中", "癌症", "重症肺炎", "败血症"]
        for condition in high_risk_conditions:
            if condition in history_lower:
                score = min(score + 20, 100)
                reasons.append(f"高危病史: {condition}")
        
        # 4. 确定级别
        if score >= 81:
            level = SeverityLevel.RED
        elif score >= 51:
            level = SeverityLevel.ORANGE
        elif score >= 21:
            level = SeverityLevel.YELLOW
        else:
            level = SeverityLevel.WHITE
        
        # 如果没有任何指标，默认黄色（谨慎起见）
        if not reasons and level == SeverityLevel.WHITE:
            level = SeverityLevel.YELLOW
            score = 25
            reasons.append("首次就诊，采用谨保守评分")
        
        return {
            "level": level.value,
            "score": min(score, 100),
            "reasons": reasons
        }

# ============ ICD 编码推荐引擎 ============

class ICDRecommender:
    """根据症状和病史推荐ICD-10编码"""
    
    SYMPTOM_TO_ICD = {
        # 心血管
        "胸痛": "I10",  # 胸痛通常关联高血压或冠心病
        "心梗": "I21",
        "心律不齐": "I49",
        # 呼吸系统
        "咳嗽": "R05",
        "呼吸困难": "R06",
        "咳血": "R04",
        "肺炎": "J18",
        # 消化系统
        "腹泻": "K59",
        "呕吐": "K21",
        "腹痛": "K08",
        # 代谢
        "糖尿病": "E11",
        "高血压": "I10",
        # 其他
        "发热": "R50",
        "头晕": "R42",
    }
    
    @staticmethod
    def recommend_icd(symptoms: List[str], medical_history: List[str]) -> List[str]:
        """推荐ICD编码"""
        codes = set()
        
        for symptom in (symptoms or []):
            for keyword, code in ICDRecommender.SYMPTOM_TO_ICD.items():
                if keyword in symptom:
                    codes.add(code)
        
        for history in (medical_history or []):
            for keyword, code in ICDRecommender.SYMPTOM_TO_ICD.items():
                if keyword in history:
                    codes.add(code)
        
        return sorted(list(codes)) or ["R00"]  # 默认 R00 (一般症状)

# ============ 科室推荐 ============

class DepartmentRecommender:
    """根据症状推荐挂号科室"""
    
    SYMPTOM_TO_DEPT = {
        # 心血管
        "胸痛": ("心内科", "SPECIALIST"),
        "心梗": ("心内科", "EXPERT"),
        "心律不齐": ("心内科", "SPECIALIST"),
        "高血压": ("心内科", "PRIMARY"),
        # 呼吸系统
        "咳嗽": ("呼吸科", "PRIMARY"),
        "呼吸困难": ("呼吸科", "SPECIALIST"),
        "咳血": ("呼吸科", "EXPERT"),
        "肺炎": ("呼吸科", "SPECIALIST"),
        # 消化系统
        "腹泻": ("消化科", "PRIMARY"),
        "呕吐": ("消化科", "PRIMARY"),
        "腹痛": ("消化科", "SPECIALIST"),
        # 内分泌
        "糖尿病": ("内分泌科", "PRIMARY"),
        # 神经
        "头晕": ("神经内科", "PRIMARY"),
        "脑卒中": ("神经内科", "EXPERT"),
        # 通用
        "发热": ("普通内科", "PRIMARY"),
    }
    
    @staticmethod
    def recommend_dept(chief_complaint: str, symptoms: List[str]) -> tuple:
        """返回 (科室, doctor_level)"""
        all_symptoms = [chief_complaint] + (symptoms or [])
        
        # 优先级：专科 > 通用
        candidates = []
        for symptom in all_symptoms:
            if symptom in DepartmentRecommender.SYMPTOM_TO_DEPT:
                dept, level = DepartmentRecommender.SYMPTOM_TO_DEPT[symptom]
                candidates.append((dept, level))
        
        # 选择专科程度最高的
        if candidates:
            level_priority = {"EXPERT": 3, "SPECIALIST": 2, "PRIMARY": 1}
            candidates.sort(key=lambda x: level_priority.get(x[1], 0), reverse=True)
            return candidates[0]
        
        return ("普通内科", "PRIMARY")

# ============ API 端点 ============

@app.post("/intake", response_model=EMRIntakeResponse)
def intake_epr(req: EMRIntakeRequest):
    """
    病例intake端点
    接收患者初诊信息，生成结构化电子病历
    """
    emr_id = f"EMR-{uuid.uuid4().hex[:12].upper()}"
    
    # 1. 计算严重程度
    severity = SeverityAssessor.calculate_severity(
        req.chief_complaint,
        req.symptoms or [],
        req.medical_history or [],
        req.vital_signs
    )
    
    # 2. 推荐ICD编码
    icd_codes = ICDRecommender.recommend_icd(
        req.symptoms or [],
        req.medical_history or []
    )
    
    # 3. 推荐科室
    recommended_dept, doctor_level = DepartmentRecommender.recommend_dept(
        req.chief_complaint,
        req.symptoms or []
    )
    
    # 4. 推荐检查
    suggested_tests = _suggest_tests(req.symptoms or [])
    
    # 5. 组织结构化数据
    structured_data = {
        "chief_complaint": req.chief_complaint,
        "symptoms": req.symptoms or [],
        "medical_history": req.medical_history or [],
        "current_medications": req.current_medications or [],
        "vital_signs": req.vital_signs.dict() if req.vital_signs else None,
        "allergy_history": req.allergy_history,
        "icd_codes": icd_codes,
        "risk_assessment": _assess_risk_factors(
            req.medical_history or [],
            req.vital_signs
        ),
        "preliminary_assessment": _generate_assessment(
            req.symptoms or [],
            req.medical_history or []
        )
    }
    
    return EMRIntakeResponse(
        status="STRUCTURED",
        emr_id=emr_id,
        structured_data=structured_data,
        severity=severity,
        recommended_dept=recommended_dept,
        recommended_doctor_level=doctor_level,
        suggested_tests=suggested_tests
    )

def _suggest_tests(symptoms: List[str]) -> List[str]:
    """根据症状推荐检查项"""
    tests = set()
    tests.add("血常规")  # 基础
    tests.add("生化全项")
    
    symptoms_lower = " ".join(s.lower() for s in symptoms)
    
    if any(kw in symptoms_lower for kw in ["心", "胸", "血压"]):
        tests.update(["ECG", "胸部X光", "肌钙蛋白"])
    
    if any(kw in symptoms_lower for kw in ["咳", "呼吸", "肺"]):
        tests.update(["胸部X光", "CT"])
    
    if any(kw in symptoms_lower for kw in ["腹", "消化"]):
        tests.update(["腹部超声", "胃镜"])
    
    if any(kw in symptoms_lower for kw in ["血糖", "糖尿"]):
        tests.update(["空腹血糖", "HbA1c"])
    
    return sorted(list(tests))

def _assess_risk_factors(medical_history: List[str], vital_signs: Optional[VitalSigns]) -> List[str]:
    """列举风险因素"""
    risks = []
    
    for condition in medical_history:
        if any(kw in condition for kw in ["高血压", "糖尿病", "心梗", "脑卒中"]):
            risks.append(f"心血管高危因素: {condition}")
        if "吸烟" in condition:
            risks.append("吸烟史")
    
    if vital_signs:
        if vital_signs.hr and vital_signs.hr > 100:
            risks.append("静息心率升高")
        if vital_signs.bp:
            try:
                systolic, _ = map(int, vital_signs.bp.split("/"))
                if systolic > 140:
                    risks.append("血压升高")
            except:
                pass
    
    return risks

def _generate_assessment(symptoms: List[str], medical_history: List[str]) -> str:
    """生成初步评估"""
    parts = []
    
    if symptoms:
        parts.append(f"患者主诉{len(symptoms)}个症状: {', '.join(symptoms)}")
    
    if medical_history:
        parts.append(f"既往病史: {', '.join(medical_history)}")
    
    if len(medical_history) > 2:
        parts.append("建议进行全面体检评估")
    
    return "；".join(parts) if parts else "需要进一步问诊"

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5001)
