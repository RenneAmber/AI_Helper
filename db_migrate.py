"""
医疗系统数据库迁移脚本
支持旧表保留 + 新功能表创建
"""

import sqlite3
import os
import json
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "chat_history.db")

def migrate_db():
    """创建或升级所有必要的表"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 1. 保留现有表 (如果已存在)
    # chat_history, agent_sessions 等保持不变
    
    # 2. 创建新表：patient_profiles（患者基本档案）
    c.execute("""
    CREATE TABLE IF NOT EXISTS patient_profiles (
        patient_id TEXT PRIMARY KEY,
        name TEXT,
        age INTEGER,
        gender TEXT,
        phone TEXT,
        email TEXT,
        insurance_id TEXT,
        chronic_diseases TEXT,  -- JSON: ["高血压", "糖尿病"]
        allergies TEXT,         -- JSON: ["青霉素"]
        last_visit_date TEXT,   -- ISO 8601
        next_followup_date TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)
    
    # 3. 创建新表：emr_records（电子病历）
    c.execute("""
    CREATE TABLE IF NOT EXISTS emr_records (
        emr_id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        chief_complaint TEXT,
        symptoms TEXT,         -- JSON: ["胸闷", "呼吸困难"]
        medical_history TEXT,  -- JSON: ["高血压5年", "糖尿病"]
        current_medications TEXT, -- JSON: [{"name": "阿司匹林", "dose": "75mg"}]
        vital_signs TEXT,       -- JSON: {"bp": "120/80", "hr": 75, "temp": 37.0}
        structured_data TEXT,   -- JSON: {"icd_codes": [...], "risk_factors": [...], "assessment": "..."}
        severity_level TEXT,    -- RED|ORANGE|YELLOW|WHITE
        severity_score INTEGER, -- 0-100
        severity_reason TEXT,   -- JSON: ["reason1", "reason2"]
        recommended_dept TEXT,  -- 如 "心内科", "内分泌科"
        recommended_doctor_level TEXT, -- PRIMARY|SPECIALIST|EXPERT
        linked_registration_id TEXT,  -- 关联的挂号ID
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (patient_id) REFERENCES patient_profiles(patient_id)
    )
    """)
    
    # 创建索引
    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_emr_patient 
    ON emr_records(patient_id)
    """)
    
    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_emr_severity 
    ON emr_records(severity_level)
    """)
    
    # 4. 创建新表：chronic_diseases_config（慢病配置）
    c.execute("""
    CREATE TABLE IF NOT EXISTS chronic_diseases_config (
        disease_id TEXT PRIMARY KEY,
        disease_name TEXT,
        check_interval_days INTEGER,  -- 建议复查间隔
        key_tests TEXT,  -- JSON: ["血压", "血糖", "HbA1c"]
        warning_signs TEXT,  -- JSON: ["突然头晕", "视力模糊"]
        typical_medications TEXT,  -- JSON: ["二甲双胍", "格列美脲"]
        description TEXT
    )
    """)
    
    # 5. 创建新表：chronic_disease_records（患者慢病记录）
    c.execute("""
    CREATE TABLE IF NOT EXISTS chronic_disease_records (
        record_id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        disease_id TEXT NOT NULL,
        diagnosis_date TEXT,  -- ISO 8601
        last_checkup_date TEXT,
        next_checkup_date TEXT,
        current_status_json TEXT,  -- 存放disease-specific数据
        -- 对高血压
        systolic INTEGER, -- 收缩压
        diastolic INTEGER, -- 舒张压
        -- 对糖尿病
        blood_glucose REAL,
        hba1c REAL,
        -- 
        last_medication_date TEXT,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (patient_id) REFERENCES patient_profiles(patient_id),
        FOREIGN KEY (disease_id) REFERENCES chronic_diseases_config(disease_id)
    )
    """)
    
    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_chronic_patient 
    ON chronic_disease_records(patient_id)
    """)
    
    # 6. 创建新表：chronic_disease_reminders（提醒任务）
    c.execute("""
    CREATE TABLE IF NOT EXISTS chronic_disease_reminders (
        reminder_id TEXT PRIMARY KEY,
        patient_id TEXT NOT NULL,
        disease_id TEXT NOT NULL,
        record_id TEXT,
        reminder_type TEXT,  -- CHECKUP|MEDICATION|FOLLOWUP|WARNING
        title TEXT,
        description TEXT,
        due_date TEXT,  -- ISO 8601
        status TEXT,  -- PENDING|SENT|ACKNOWLEDGED|SKIPPED|COMPLETED
        delivery_channel TEXT,  -- SMS|VOICE|APP|EMAIL|WECHAT
        sent_at TEXT,
        acknowledged_at TEXT,
        delivery_status TEXT,  -- NULL|SENT|FAILED|BOUNCED
        retry_count INTEGER DEFAULT 0,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY (patient_id) REFERENCES patient_profiles(patient_id),
        FOREIGN KEY (disease_id) REFERENCES chronic_diseases_config(disease_id)
    )
    """)
    
    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_reminder_patient 
    ON chronic_disease_reminders(patient_id)
    """)
    
    c.execute("""
    CREATE INDEX IF NOT EXISTS idx_reminder_status 
    ON chronic_disease_reminders(status)
    """)
    
    # 7. 创建新表：reminder_logs（提醒发送日志）
    c.execute("""
    CREATE TABLE IF NOT EXISTS reminder_logs (
        log_id TEXT PRIMARY KEY,
        reminder_id TEXT NOT NULL,
        patient_id TEXT NOT NULL,
        delivery_channel TEXT,
        message TEXT,
        recipient TEXT,
        status TEXT,  -- SENT|FAILED|BOUNCED
        error_message TEXT,
        created_at TEXT,
        FOREIGN KEY (reminder_id) REFERENCES chronic_disease_reminders(reminder_id),
        FOREIGN KEY (patient_id) REFERENCES patient_profiles(patient_id)
    )
    """)
    
    # 8. 初始化常见慢病配置
    c.execute("SELECT COUNT(*) FROM chronic_diseases_config")
    if c.fetchone()[0] == 0:
        diseases = [
            {
                "disease_id": "HYPERTENSION",
                "disease_name": "高血压",
                "check_interval_days": 30,
                "key_tests": json.dumps(["血压", "血清肌酐", "尿蛋白"]),
                "warning_signs": json.dumps(["突然头晕目眩", "胸闷", "视力模糊", "鼻出血"]),
                "typical_medications": json.dumps(["缬沙坦", "硝苯地平", "阿司匹林"]),
                "description": "收缩压≥140 mmHg 和/或 舒张压≥90 mmHg"
            },
            {
                "disease_id": "DIABETES",
                "disease_name": "糖尿病",
                "check_interval_days": 90,
                "key_tests": json.dumps(["空腹血糖", "HbA1c", "血脂全项", "尿微量白蛋白"]),
                "warning_signs": json.dumps(["口渴", "多尿", "疲劳", "视物模糊", "手脚麻木"]),
                "typical_medications": json.dumps(["二甲双胍", "格列美脲", "胰岛素"]),
                "description": "FPG≥7.0 mmol/L 或 2h PG≥11.1 mmol/L"
            },
            {
                "disease_id": "CHD",
                "disease_name": "冠心病",
                "check_interval_days": 60,
                "key_tests": json.dumps(["ECG", "肌钙蛋白", "冠状动脉CT", "运动试验"]),
                "warning_signs": json.dumps(["胸痛", "呼吸困难", "心悸", "乏力"]),
                "typical_medications": json.dumps(["阿司匹林", "硝酸甘油", "普萘洛尔", "阿托伐他汀"]),
                "description": "冠状动脉粥样硬化性心脏病"
            }
        ]
        for disease in diseases:
            c.execute("""
            INSERT INTO chronic_diseases_config 
            (disease_id, disease_name, check_interval_days, key_tests, warning_signs, typical_medications, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                disease["disease_id"],
                disease["disease_name"],
                disease["check_interval_days"],
                disease["key_tests"],
                disease["warning_signs"],
                disease["typical_medications"],
                disease["description"]
            ))
    
    conn.commit()
    conn.close()
    print(f"✓ Database migrated: {DB_PATH}")

if __name__ == "__main__":
    migrate_db()
