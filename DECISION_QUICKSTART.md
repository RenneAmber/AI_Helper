# Decision Making System - 快速开始

## 安装依赖

```bash
# 后端
cd backend_fastapi
pip install -r requirements.txt

# 前端
cd frontend_react
npm install
```

## 启动系统

### 方式 1：Docker Compose（推荐）

```bash
# 从项目根目录
docker compose up --build -d

# 等待所有服务就绪（检查 health checks）
docker compose ps

# 查看日志
docker compose logs -f fastapi
```

### 方式 2：本地开发

**终端 1 - PostgreSQL + Redis**
```bash
docker run --rm -d --name postgres-dev \
  -e POSTGRES_DB=chatdb \
  -e POSTGRES_USER=chat \
  -e POSTGRES_PASSWORD=chat \
  -p 5432:5432 \
  postgres:16

docker run --rm -d --name redis-dev \
  -p 6379:6379 \
  redis:7
```

**终端 2 - FastAPI**
```bash
cd backend_fastapi
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.db.init_db
uvicorn app.main:app --reload --port 8000
```

**终端 3 - NestJS**
```bash
cd gateway_nestjs
npm install
npm run start:dev
```

**终端 4 - React Frontend**
```bash
cd frontend_react
npm run dev
```

## 使用决策系统

### 1. 打开前端

浏览器访问 `http://localhost:5173`

### 2. 选择 "Decision Making" 标签

### 3. 快速登录

点击 "Quick Login" 按钮

### 4. 创建决策

**步骤 1：选择 "Create Decision" 模式**

填入：
```
Question: Should we adopt LangGraph for decision-making?
Domain: engineering
Risk Posture: low
Constraints: audit_required, low_hallucination
```

点击 "Create Decision"

返回结果：
```json
{
  "decisionId": "D-20260415-abc12345",
  "status": "draft"
}
```

复制 decision_id

**步骤 2：选择 "Run Decision" 模式**

粘贴 decision_id，点击 "Run LangGraph"

观察完整的执行结果：
```json
{
  "decisionId": "D-20260415-abc12345",
  "status": "final",
  "decision": {
    "recommendation": "PROCEED",
    "confidence": 0.75,
    "rationale": [...],
    "safetyNotes": []
  }
}
```

**步骤 3：选择 "Replay Decision" 模式**

粘贴 decision_id，点击 "Replay"

查看完整的事件流和工具执行：
```json
{
  "decisionId": "D-20260415-abc12345",
  "events": [
    {
      "time": "2026-04-15T10:30:45.100000+00:00",
      "type": "NODE_START",
      "node": "Normalize",
      "status": "success",
      "payload": {...}
    },
    ...
  ],
  "toolRuns": [
    {
      "run_id": "...",
      "tool_name": "fake_retriever",
      "status": "success",
      "input_hash": "sha256:...",
      "output_hash": "sha256:...",
      ...
    }
  ],
  "evidenceItems": [...]
}
```

## 数据库

### PostgreSQL 表

所有决策数据存储在 PostgreSQL 中：

```bash
# 连接数据库（假设运行 Docker）
psql -h localhost -U chat -d chatdb

# 查看决策
SELECT decision_id, title, status, created_at FROM decisions;

# 查看事件流
SELECT event_id, event_type, node_name, created_at 
FROM decision_events 
WHERE decision_id = 'D-20260415-abc12345'
ORDER BY created_at;

# 查看工具执行记录
SELECT run_id, tool_name, status, started_at, ended_at 
FROM tool_runs 
WHERE decision_id = 'D-20260415-abc12345';

# 查看证据
SELECT evidence_id, kind, source_type, quote 
FROM evidence_items 
WHERE decision_id = 'D-20260415-abc12345';
```

## API 端点

### 创建决策

```bash
curl -X POST http://localhost:3000/decisions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Should we migrate to LangGraph?",
    "domain": "engineering",
    "requester": {"userId":"u_123","displayName":"Admin"},
    "context": {
      "system": "medical-agent",
      "constraints": ["audit_required"],
      "riskPosture": "low",
      "timeHorizonDays": 90
    },
    "criteria": [
      {"key": "reliability", "weight": 0.35},
      {"key": "cost", "weight": 0.25},
      {"key": "adoption", "weight": 0.40}
    ]
  }'
```

### 运行决策

```bash
curl -X POST http://localhost:3000/decisions/D-20260415-abc12345/run \
  -H "Authorization: Bearer <token>"
```

### 回放决策

```bash
curl http://localhost:3000/decisions/D-20260415-abc12345/replay \
  -H "Authorization: Bearer <token>"
```

## 关键文件

| 文件 | 用途 |
|------|------|
| `backend_fastapi/app/decision/models.py` | 数据库模型（Decision, EvidenceItem, ToolRun, DecisionEvent） |
| `backend_fastapi/app/decision/schemas.py` | Pydantic 请求/响应 |
| `backend_fastapi/app/decision/state.py` | LangGraph 状态定义 |
| `backend_fastapi/app/decision/nodes.py` | 8 个节点实现 |
| `backend_fastapi/app/decision/graph.py` | LangGraph 编译 |
| `backend_fastapi/app/decision/repo.py` | 数据库操作封装 |
| `backend_fastapi/app/decision/hashing.py` | SHA256 哈希（防篡改） |
| `backend_fastapi/app/decision/tools.py` | 示例工具（retriever, log_query） |
| `backend_fastapi/app/routers/decisions.py` | FastAPI 路由 |
| `frontend_react/src/DecisionMaking.jsx` | 前端决策 UI |
| `frontend_react/src/App.jsx` | 应用主组件（Chat + Decision 切换） |
| `DECISION_ARCHITECTURE.md` | 完整设计文档 |

## 常见问题

### Q: 如何添加自己的工具？

A: 在 `app/decision/tools.py` 中添加新函数，然后在 `app/decision/nodes.py` 的 `tool_execute_node` 中添加条件分支。

```python
# tools.py
async def my_custom_tool(input_data: str) -> Dict[str, Any]:
    return {
        "kind": "custom",
        "sourceType": "my_system",
        ...
    }

# nodes.py - in tool_execute_node
elif tool_name == "my_custom_tool":
    out = await my_custom_tool(tool_input["data"])
```

### Q: 如何在生产环境运行？

A: 
1. 修改 `.env` 文件：更改 JWT_SECRET, INTERNAL_API_TOKEN, 数据库密码
2. 使用 PostgreSQL（而不是 SQLite）
3. 配置 HTTPS 和反向代理（Nginx / Traefik）
4. 启用日志收集（ELK / Datadog）

### Q: 工具调用失败会怎样？

A: 
1. ToolExecute 捕获异常，标记 status="failure"
2. ToolVerify 检测到失败，将其添加到 errors 列表
3. EvidenceQualityGate 检查是否有足够的成功工具输出
4. 若不足，BuildDecisionRecord 生成 NEEDS_REVIEW 建议
5. 全过程都可回放，审计人员知道具体哪个工具失败了

### Q: 能否并行运行多个工具？

A: 可以，在 `plan_node` 中添加多个工具到 `tool_queue`，然后在 `tool_execute_node` 中使用 `asyncio.gather()` 并行调用。

## 监控和调试

### 后端日志

```bash
# 如果使用 Docker
docker compose logs -f fastapi

# 本地开发
# Uvicorn 会自动输出结构化 JSON 日志
```

### 前端调试

打开浏览器开发者工具（F12），查看：
- Network：观察 API 请求/响应
- Console：查看 JavaScript 错误
- Application → Local Storage：检查 token 存储

### 数据库查询

```bash
# 查看最近的决策
SELECT * FROM decisions ORDER BY created_at DESC LIMIT 5;

# 查看特定决策的全部事件
SELECT * FROM decision_events 
WHERE decision_id = 'D-20260415-abc12345'
ORDER BY created_at;

# 查看失败的工具
SELECT * FROM tool_runs WHERE status = 'failure';
```

## 下一步

- [ ] 实现 ConflictDetection 节点
- [ ] 添加 HumanReview 决策关卡
- [ ] 集成真实 RAG 系统
- [ ] 添加决策评估（比较预测 vs 实际）
- [ ] 构建仪表板查看决策趋势
- [ ] WebSocket 流式更新

---

**提示：** 这个系统的价值在于"可回放、可追责、可审计"。确保每个决策都有完整的事件链，即使决策后有问题，也能追踪根本原因。
