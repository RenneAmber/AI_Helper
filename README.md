# 医疗智能助手（新手上手版）

这是一个“工具驱动型”医疗助手原型。
它的目标不是直接给医学结论，而是把用户请求拆成可执行任务，再调用工具服务得到结果，最后做受约束的总结。

## 1. 项目在做什么

支持五类医疗任务：
- 挂号：预约医院/科室/时间
- 查询：医生列表、化验报告、影像、挂号记录、就诊记录
- 解读：对已有报告做信息解释（不做诊断）
- **🆕 病例采集与结构化（EMR_INTAKE）**：初诊信息智能整理 + 严重程度分级 + 科室推荐
- **🆕 慢病管理（CHRONIC_DISEASE_MGMT）**：档案建立、提醒生成、多渠道通知、预警检测

同时支持普通问答（非医疗意图时走 RAG 链路）。

### 🆕 v2.0 新功能亮点

**1. 智能预约问诊** - 缩短初诊时间
- 患者简单描述症状 → AI自动生成结构化电子病历（EMR）
- 系统计算严重程度（⚪ 白、🟡 黄、🟠 橙、🔴 红）
- 红色患者自动优先加号，减少医生问诊时间

**2. AI慢病管家** - 提升患者粘性与自我管理
- 为高血压、糖尿病等患者建立档案
- 自动生成复查、配药、健康教育提醒
- 支持短信、语音电话、APP推送、邮件多渠道通知
- 异常数据实时预警（如血压>180）

详见: [NEW_FEATURES.md](NEW_FEATURES.md) 完整功能说明与使用示例

## 2. 新手建议阅读顺序

1. **本文件** - 了解概述
2. [NEW_FEATURES.md](NEW_FEATURES.md) - 新功能详细说明和场景示例
3. [index.html](index.html) - 前端交互方式
4. [app.py](app.py) - 统一入口与路由
5. [medical_agent.py](medical_agent.py) - 医疗任务编排逻辑
6. [tool_client.py](tool_client.py) - 工具服务调用

## 3. 总体架构

```text
浏览器(index.html)
  -> POST /chat (app.py)
      -> 医疗意图: medical_agent_step(...) in medical_agent.py
           -> Planner(输出JSON计划，支持新的EMR和慢病任务)
           -> 缺槽追问(多轮对话)
           -> run_plan_sync(校验后执行工具链)
                ├─ EMR Service (端口5001) 【新增】- 病例结构化
                ├─ Chronic Disease Service (端口5002) 【新增】- 慢病管理
                ├─ Registration Service - 挂号
                ├─ Query Service - 查询
                └─ Interpret Service - 解读
           -> Summary(基于工具结果汇总)
      -> 非医疗意图: 通用RAG链路
```

## 4. 快速开始（新用户）

### 第一次运行

```bash
# 1. 安装microservice依赖
pip install fastapi uvicorn pydantic httpx

# 2. 执行数据库迁移（创建慢病管理相关表）
python db_migrate.py

# 3. 一键启动所有服务
python start_all_services.py

# 或手动启动：
# 终端1: python medical-agent-proto/services/emr_service/main.py
# 终端2: python medical-agent-proto/services/chronic_disease_service/main.py
# 终端3: python app.py
```

### 访问前端

打开浏览器 `http://localhost:5000` 尝试新功能

### 试试新功能

在输入框输入：
```
- 智能预约问诊：
  "我最近3天胸闷呼吸困难，血压160/95，有高血压史5年，想挂心内科"
  
- 建立慢病档案：
  "我有糖尿病10年，要建立档案并设置定期提醒"
  
- 慢病提醒：
  "提醒我做高血压的月度复查"
```

页面位于 [index.html](index.html)，操作语义如下：

1. 输入需求
- 含义：用户提交当前意图。
- 建议：尽量一次性给出医院/科室/时间或报告类型，减少追问次数。
- 限制：单次输入默认不超过 5000 字符（后端 `MAX_INPUT_CHARS` 控制）。

2. 点击发送
- 含义：前端调用 `POST /chat`。
- 限制：网络失败会返回“请求失败”；发送中按钮会禁用，避免重复提交。

3. 观察响应颜色
- 黄色：`clarification`，说明参数不完整，需要补充。
- 绿色：`final`，工具已执行并返回汇总。
- 蓝色：普通问答（未走医疗工具链）。

4. 继续补充信息
- 含义：前端自动携带 `session_id`，后端会继续同一任务上下文。
- 限制：如果点“重置”，`session_id` 会清空，后续消息会被当作新会话。

5. 点击重置
- 含义：清空页面并放弃当前会话状态。
- 限制：仅影响前端会话关联，不会删除数据库中历史记录。

## 5. 关键模块职责

### 5.1 app.py

- 提供主路由：`/chat`、`/medical/chat`、`/ingest`。
- 在 `/chat` 中自动判断是否走医疗 Agent。
- 统一处理日志、错误、Markdown 安全渲染、数据库写入。
- 内置 `/svc/*` Mock 微服务，便于本地联调。

### 5.2 medical_agent.py

按四阶段工作：

1. Planner：只产出结构化计划（任务、参数、依赖）。
2. Slot Filling：缺参数时只提取指定槽位，不重跑全量规划。
3. Executor：执行前做计划校验/参数校验，执行后记录每步状态。
4. Summary：只允许引用成功工具步骤，引用不合法会触发兜底总结。

这样做的核心收益：降低幻觉，提升执行可追踪性。

### 5.3 tool_client.py

- 负责与工具服务通信。
- 封装超时、重试、错误分类。
- 上层只需要调用 `register/query/interpret`，无需关心 HTTP 细节。

## 6. API 快速参考

### POST /chat

请求：

```json
{
  "message": "帮我预约心内科，明天上午",
  "session_id": "可选",
  "patient_id": "可选",
  "context": "可选"
}
```

医疗路由响应（示例）：

```json
{
  "type": "clarification | final",
  "session_id": "xxx",
  "routed_to": "medical_agent",
  "json": {
    "answer": "...",
    "sources": ["tool:S1:QUERY"],
    "confidence": 0.8
  }
}
```

### POST /medical/chat

语义与 `/chat` 的医疗分支一致，适合服务侧直连医疗链路。

### POST /ingest

用于把外部文档切片并写入向量库，支持后续 RAG 检索。

## 7. 本地启动

1. 安装依赖

```bash
pip install flask flask-cors openai httpx numpy markdown bleach python-dotenv
```

2. 配置 `.env`

```env
AZURE_OPENAI_API_KEY=your_key_here
AZURE_OPENAI_API_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-41_milky
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-large
AZURE_OPENAI_API_VERSION=2025-01-01-preview
PORT=5000
```

3. 启动

```bash
python app.py
```

4. 打开页面

```text
http://localhost:5000
```

## 8. 运行限制与已知边界

- 这是演示原型，不提供医疗诊断或治疗建议。
- 默认使用 Mock 微服务，结果不代表真实医院系统数据。
- 未内置鉴权，生产环境必须补充认证和审计。
- RAG 切片目前按字符切分，生产建议替换为 token/语义切分。

## 9. 后续建议

1. 替换 `/svc/*` 为真实 HIS/LIS 接口。
2. 为 `medical_agent.py` 增加单元测试（计划校验、依赖执行、summary 兜底）。
3. 为前端增加“当前会话状态可视化”（显示 session_id、缺失槽位）。

## 文档索引

- 详细用户手册：`USER_MANUAL_DETAILED.md`
- 技术文档：`TECHNICAL_DOCUMENTATION.md`

## 10. 免责声明

本项目仅用于技术演示，不构成任何医疗诊断或治疗建议。所有医疗决策请以执业医生意见为准。

## 11. 🆕 v3 架构升级（FastAPI + NestJS + PostgreSQL + Redis）

已新增一套可并行运行的新框架，满足以下能力：
- 使用 FastAPI 替换 Flask（作为内部业务服务）
- 使用 NestJS 作为外部 API Gateway
- 提供 JWT 鉴权、全局限流、SSE 流式输出
- 使用 PostgreSQL 记录审计日志与对话内容
- 使用 Redis 作为 session 与 memory store
- 使用 Docker Compose 本地一键启动

### 新目录

- `backend_fastapi/`：内部业务服务（落库、session/memory、SSE 源头）
- `gateway_nestjs/`：网关层（鉴权、限流、对外路由、SSE 转发）
- `postgres/init.sql`：审计表与对话表初始化脚本
- `docker-compose.yml`：本地一键启动编排
- `.env.stack.example`：整套服务环境变量模板

### 一键启动

```bash
# 1) 复制环境变量
cp .env.stack.example .env

# 2) 启动所有服务
docker compose up --build -d

# 3) 查看服务状态
docker compose ps
```

默认端口：
- NestJS 网关：`http://localhost:3000`
- FastAPI 内部服务：`http://localhost:8000`
- PostgreSQL：`localhost:5432`
- Redis：`localhost:6379`

### 调用示例

1. 登录获取 token

```bash
curl -X POST http://localhost:3000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'
```

2. 普通对话

```bash
curl -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"帮我总结一下今天重点","session_id":"s1"}'
```

3. SSE 流式对话（统一 /chat）

```bash
curl -N -X POST "http://localhost:3000/chat" \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"你好","session_id":"s1","stream":true}'
```

### 说明

- v2 Flask 版本仍保留，方便对比与渐进迁移。
- 新网关默认使用 demo 账号密码（`DEMO_USER` / `DEMO_PASSWORD`），上线前请替换。

## 12. 验收映射（不会乱编、能失败、可解释）

### /chat 流式输出

- 对外统一接口：`POST /chat`
- 请求体中 `stream=true` 时，返回 `text/event-stream` 流式分片
- `stream=false` 或不传时，返回 JSON

### trace-id + 结构化日志

- 网关和 FastAPI 均支持 `x-trace-id`
- 未传时自动生成 UUID
- 每次请求返回 `x-trace-id` 响应头
- 日志为 JSON 结构（method/path/status/latency/trace_id）

### 明确状态机（State）

Agent 关键状态：
- `RECEIVED`
- `PLANNING`
- `TOOL_RUNNING`
- `TOOL_FAILED`
- `ANSWERING`
- `COMPLETED`

### 至少一个真实工具

- `knowledge_lookup` 工具负责从输入中识别医疗主题关键词并产出结构化工具结果
- 工具输出作为 evidence 写入审计日志和回放接口

### 强制失败分支

- 请求参数支持 `force_fail=true`
- 或消息中包含 `FAIL_TOOL`
- 会触发 `TOOL_FAILED`，并返回可解释失败原因

### Evidence / Log 可回放

- 回放接口：`GET /chat/replay/:traceId`（网关）
- 数据来源：PostgreSQL 中的 `audit_logs` 与 `conversations`
- 返回状态流转、工具输出、对话内容

### 最小 CI

- 工作流文件：`.github/workflows/ci.yml`
- 包含：
  - backend lint（ruff）
  - backend unit test（pytest）
  - gateway lint（tsc --noEmit）
  - gateway unit test（node --test）
  - build image（FastAPI/NestJS/React）

### React 前端

- 目录：`frontend_react/`
- 支持：
  - 登录获取 JWT
  - 调用 `/chat`（json / stream 两种模式）
  - `force_fail` 失败分支切换
  - 显示 trace-id 与流式输出
