# 医疗智能助手 - 新功能指南

## 📋 概述

本次更新为医疗智能助手添加了两项核心功能：

1. **智能预约问诊** - 病例采集 → 自动结构化 → 严重程度分级 → 优先加号
2. **AI慢病管家** - 患者档案 → 随访提醒 → 多渠道通知（短信/语音/APP）

---

## 🎯 新功能详解

### 1. 智能预约问诊（EMR_INTAKE）

#### 场景
用户有初诊需求，但初诊表填写繁琐，医生问诊重复问题。

#### 解决方案
- **患者端**：简单描述症状即可，无需填表
- **系统端**：AI自动整理成结构化电子病历（EMR）
- **医生端**：获得结构化病历，快速了解患者情况，缩短问诊时间
- **挂号系统**：根据严重程度（白黄橙红）自动分级排队

#### 使用示例

**用户输入：**
```
我最近3天一直胸闷、呼吸困难，有时候还心悸。
我有高血压史5年，在吃阿司匹林。
血压最近160/95，心率85。
```

**系统输出：**
```
✓ 病例采集成功 (EMR-ABC123)

【结构化病历】
主诉：胸闷、呼吸困难3天，可伴心悸
症状：胸闷、呼吸困难、心悸
既往史：高血压5年
当前用药：阿司匹林 100mg
生命体征：BP 160/95, HR 85

【严重程度】: 🔴 红色 (85/100)
原因：
  - 红色症状：胸闷、心梗征兆
  - 异常血压：160/95

【建议】
- 推荐科室：心内科
- 医生级别：专家
- 建议检查：ECG、胸部X光、肌钙蛋白

此结构化病历可直接作为挂号时的初诊信息补充
```

#### 接口详情

**POST /chat**
```json
{
  "message": "我胸闷呼吸困难，血压160/95，有高血压史，在吃阿司匹林。想挂号。",
  "patient_id": "P123"
}
```

**系统流程**
1. Planner 识别：`EMR_INTAKE` + `REGISTRATION` 两个任务
2. EMR_INTAKE 执行：
   - 调用 `/emr/intake` 生成结构化病历
   - 返回 `severity_level`（RED/ORANGE/YELLOW/WHITE）
   - 返回 `recommended_dept`（心内科）
3. REGISTRATION 执行：
   - 使用 EMR 结果中的科室和严重程度优先级信息
   - 生成挂号预约

#### 严重程度分级规则

| 等级 | 分数 | 特征 | 处理 |
|-----|------|------|------|
| 🔴 RED | 81-100 | 胸痛、呼吸困难、意识模糊、心率<40或>120 | 立即就医、自动加号 |
| 🟠 ORANGE | 51-80 | 严重腹泻、持续呕吐、高热(>38.5) | 当天加号 |
| 🟡 YELLOW | 21-50 | 轻度腹泻、低烧、轻度皮疹 | 常规排队 |
| ⚪ WHITE | 0-20 | 无症状或轻微症状 | 预约非急诊 |

---

### 2. AI慢病管家（CHRONIC_DISEASE_MGMT）

#### 场景
高血压、糖尿病等慢性病患者需要定期复查、按时配药、健康管理提醒。

#### 解决方案
- **建档**：患者初诊时建立慢病档案
- **提醒**：系统自动生成复查、配药、健康教育提醒
- **多渠道**：支持短信、语音电话、APP推送、邮件
- **预警**：异常数据实时预警（如血压>180）

#### 使用示例

**场景1：建立慢病档案**
```
用户: "我有高血压5年了，最近要正式建立档案做定期随访"

系统:
✓ 已建立档案 (CDR-XYZ789)
  疾病：高血压
  诊断日期：2026-04-06
  
已自动生成以下提醒：
  1. 【复查提醒】每30天一次，提前7天通知
  2. 【配药提醒】每周一次
  3. 【健康教育】全面管理建议
```

**场景2：生成后续提醒**
```
用户: "提醒我做高血压的定期复查"

系统:
✓ 已生成高血压复查计划

【复查】2026-05-06（提前7天于2026-04-29提醒）
  推荐检查：血压、血清肌酐、尿蛋白

【配药】2026-04-13
  联系医院或药房取药

【生活管理】
  1. 低盐饮食（<6g/天）
  2. 每周运动150分钟
  3. 控制体重
  4. 定期检测血压
  5. 戒烟限酒

【发送渠道】短信 + APP推送 + 邮件
```

**场景3：危险预警**
```
用户: "我最近血压160/95，有没有风险？"

系统:
⚠️ 检测到异常数据

血压：160/95（高于正常）
风险等级：中等
建议：
  - 立即测量血压确认
  - 联系医生调整用药
  - 加强饮食和运动管理

如果血压>180/110，会自动发出紧急预警
```

#### 支持的慢性病

| 疾病 | 复查间隔 | 主要检查 | 预警指标 |
|-----|---------|--------|--------|
| 高血压 | 30天 | 血压、血清肌酐、尿蛋白 | SBP>180 或 DBP>110 |
| 糖尿病 | 90天 | 空腹血糖、HbA1c、血脂、尿微量白蛋白 | 血糖>11.1 mmol/L 或 HbA1c>8% |
| 冠心病 | 60天 | ECG、肌钙蛋白、冠状动脉CT、血脂 | 发作胸痛症状 |

#### API示例

**建立档案**
```json
POST /chronic/intake
{
  "patient_id": "P123",
  "disease_name": "高血压",
  "diagnosis_date": "2026-04-06"
}
```

**生成提醒**
```json
POST /chronic/generate-reminders
{
  "patient_id": "P123",
  "disease_name": "高血压",
  "check_interval_days": 30,
  "last_checkup_date": "2026-03-06",
  "preferred_channels": ["SMS", "APP", "EMAIL"]
}
```

**检查预警**
```json
POST /chronic/check-urgent-warning
{
  "patient_id": "P123",
  "disease_name": "高血压",
  "systolic": 185,
  "diastolic": 115
}
→ Response: 紧急预警！需立即就医
```

**发送语音提醒**
```json
POST /chronic/voice-reminder
{
  "reminder_id": "REM-123",
  "patient_id": "P123",
  "patient_name": "张三",
  "patient_phone": "13800138000",
  "disease_name": "高血压",
  "title": "定期复查提醒",
  "message_text": "亲，您高血压已30天未复查..."
}
```

---

## 🏗️ 系统架构

### 新增微服务

```
┌─────────────────────┐
│   Flask 主应用      │
│   (app.py)          │
└──────────┬──────────┘
     │
     ├──→ EMR Service (5001) - 病例采集&结构化
     │    /intake
     │
     ├──→ Chronic Disease Service (5002) - 慢病管理
     │    /chronic/intake
     │    /chronic/generate-reminders
     │    /chronic/check-urgent-warning
     │    /chronic/voice-reminder
     │
     └──→ 原有服务
          Registration (5000)
          Query (5000)
          Interpret (5000)
```

### 数据库扩展

**新增6个关键表**：
- `patient_profiles` - 患者基本档案
- `emr_records` - 电子病历记录
- `chronic_diseases_config` - 慢病配置（可维护）
- `chronic_disease_records` - 患者慢病档案
- `chronic_disease_reminders` - 提醒任务
- `reminder_logs` - 提醒发送日志

---

## 🚀 快速开始

### 1. 安装依赖
```bash
pip install fastapi uvicorn pydantic httpx
```

### 2. 运行数据库迁移
```bash
python db_migrate.py
```

### 3. 启动所有服务
```bash
# 方式1：使用启动脚本（推荐）
python start_all_services.py

# 方式2：手动启动各服务
# 终端1: EMR Service
python medical-agent-proto/services/emr_service/main.py

# 终端2: Chronic Disease Service
python medical-agent-proto/services/chronic_disease_service/main.py

# 终端3: 主应用
python app.py
```

### 4. 测试新功能
```bash
# 智能预约问诊
curl -X POST http://localhost:5000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "我胸闷呼吸困难3天，血压160/95，想挂号",
    "patient_id": "P001"
  }'

# 建立慢病档案
curl -X POST http://localhost:5000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "我有高血压5年，要建立定期随访档案",
    "patient_id": "P001"
  }'
```

---

## 📊 预期效果

### 对患者的价值
✅ 初诊无需繁琐填表
✅ 自动优先加号（严重患者）
✅ 定期提醒复查、配药
✅ 多渠道便利提醒（短信/语音/APP）
✅ 异常数据实时预警

### 对医生的价值
✅ 获得结构化初诊信息，快速了解患者
✅ 减少重复问诊时间（从5-10分钟→1-2分钟）
✅ 自动化的患者随访管理
✅ 慢病患者的持续关注

### 对医院的价值
✅ 提升患者体验度和满意度
✅ 优化号源配置（按严重程度分级）
✅ 降低医生工作负荷
✅ 患者粘性提高（定期提醒+管理）

---

## 🔧 配置和扩展

### 添加自定义慢病类型
编辑 `db_migrate.py` 中的 `DISEASE_CONFIG` 配置：
```python
{
    "disease_id": "YOUR_DISEASE",
    "disease_name": "你的慢性病",
    "check_interval_days": 30,
    "key_tests": json.dumps(["检查1", "检查2"]),
    "warning_signs": json.dumps(["预警症状1"]),
    "typical_medications": json.dumps(["常用药1"]),
    "description": "疾病描述"
}
```

### 集成第三方语音服务
在 `chronic_disease_service/main.py` 的 `send_voice_reminder` 中集成：
- 阿里云语音服务
- 腾讯云语音服务
- Twilio 等国际服务

### 集成短信服务
类似地集成第三方短信API（阿里云、腾讯云等）

---

## 📝 API 完整参考

详见各服务的源码注释和 FastAPI 自动文档：
- EMR Service: http://localhost:5001/docs
- Chronic Disease Service: http://localhost:5002/docs

---

## 🐛 故障排除

**问题1：EMR Service 返回 404**
→ 确保服务启动在 5001 端口，检查 URL 中的 `/intake` 端点

**问题2：数据库表不存在**
→ 运行 `python db_migrate.py` 创建新表

**问题3：慢病提醒没有发送**
→ 检查 `chronic_disease_reminders` 表中提醒状态，确认发送渠道配置

**问题4：严重程度分级不准确**
→ 调整 `emr_service/main.py` 中 `SeverityAssessor` 的阈值

---

## 📚 相关文档

- [病例采集 API](medical-agent-proto/services/emr_service/main.py)
- [慢病管家 API](medical-agent-proto/services/chronic_disease_service/main.py)
- [医疗Agent编排](medical_agent.py)
- [工具客户端](tool_client.py)

---

## ✨ 后续改进方向

1. **集成HIS/EMR系统** - 与医院的电子病历系统对接
2. **机器学习模型** - 优化严重程度评估和提醒时机
3. **多语言支持** - 扩展至其他语言
4. **IoT设备集成** - 接入血压仪、血糖仪等可穿戴设备
5. **患者APP** - 原生移动应用搴以增强用户体验
6. **医生工作台** - 为医生提供患者管理仪表板

---

**最后更新**: 2026-04-06
**版本**: 2.0 (新功能版)
