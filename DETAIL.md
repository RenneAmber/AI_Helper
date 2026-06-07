# DETAIL.md - ChatAI Agent Framework 详细设计文档

## 目录
1. [架构总览](#架构总览)
2. [后端 FastAPI 服务](#后端-fastapi-服务)
3. [网关 NestJS 层](#网关-nestjs-层)
4. [前端 React 应用](#前端-react-应用)
5. [数据持久化](#数据持久化)
6. [状态机与 Agent 执行](#状态机与-agent-执行)
7. [请求流程与 Trace](#请求流程与-trace)
8. [错误处理与失败恢复](#错误处理与失败恢复)
9. [测试策略](#测试策略)
10. [本地开发与部署](#本地开发与部署)

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                    浏览器（React 前端）                      │
│              http://localhost:5173                            │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP + JWT Token + trace-id
                     │
┌────────────────────▼────────────────────────────────────────┐
│            NestJS 网关（3000）                               │
│  ├─ JWT 鉴权（Passport）                                     │
│  ├─ 全局限流（Throttler）                                    │
│  ├─ Trace 中间件                                             │
│  └─ /chat 路由（支持 stream + JSON）                        │
└────────────────────┬────────────────────────────────────────┘
                     │ x-internal-token + x-trace-id
                     │
┌────────────────────▼────────────────────────────────────────┐
│          FastAPI 内部服务（8000）                             │
│  ├─ Trace 上下文与结构化日志                                 │
│  ├─ Agent 状态机（State）                                    │
│  ├─ 工具执行与证据收集                                       │
│  ├─ 强制失败分支                                             │
│  └─ /internal/chat, /internal/chat/stream, /internal/chat/replay  │
└────────────────────┬────────────────────────────────────────┘
                     │ SQLAlchemy ORM
                     │
        ┌────────────┴────────────┐
        │                         │
┌───────▼────────────┐   ┌────────▼──────────┐
│  PostgreSQL (5432) │   │  Redis (6379)     │
│  ├─ conversations  │   │  ├─ session:{id}  │
│  └─ audit_logs     │   │  └─ memory:{uid}  │
└────────────────────┘   └───────────────────┘
```

**设计原则：**
- 网关与后端分离：网关负责鉴权/限流，后端负责业务逻辑
- Trace 贯穿全链路：每个请求有唯一 trace-id，便于问题追踪
- 证据可回放：所有状态转移、工具调用、最终答案都持久化到 PostgreSQL
- 失败可解释：强制失败分支让 Agent 能演示失败原因

---

## 后端 FastAPI 服务

### 目录结构

```
backend_fastapi/
├── app/
│   ├── __init__.py
│   ├── main.py                    # 应用启动，路由注册
│   ├── config.py                  # 环境变量加载
│   ├── database.py                # SQLAlchemy 异步引擎
│   ├── models.py                  # ORM 模型（Conversation, AuditLog）
│   ├── schemas_chat.py            # Pydantic 请求/响应体
│   ├── redis_store.py             # Redis 操作（session, memory）
│   ├── core/                       # 核心基础设施
│   │   ├── __init__.py
│   │   ├── trace.py               # trace-id 上下文变量
│   │   └── logging_setup.py       # 结构化日志（JSON 格式）
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── request_context.py     # Trace 中间件
│   ├── agent/                     # Agent 核心逻辑
│   │   ├── __init__.py
│   │   ├── state_machine.py       # 状态定义与转移
│   │   ├── chat_agent.py          # Agent 执行引擎
│   │   └── tools/
│   │       ├── __init__.py
│   │       └── knowledge_tool.py  # 知识库查询工具
│   └── routers/                   # API 路由
│       ├── __init__.py
│       ├── health.py              # /health
│       ├── chat.py                # /internal/chat, /internal/chat/stream, /internal/chat/replay
│       └── medical.py             # /internal/medical/triage（示例医疗路由）
├── Dockerfile
├── requirements.txt
├── pyproject.toml                 # Ruff 与 pytest 配置
└── tests/
    └── test_agent_state_machine.py
```

### 核心模块详解

#### 1. Trace 与日志

**[app/core/trace.py](app/core/trace.py)** - Context Var 管理

```python
trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="-")

def get_trace_id() -> str:
    return trace_id_ctx.get()

def set_trace_id(trace_id: str) -> None:
    trace_id_ctx.set(trace_id)
```

特点：
- 基于 Python 3.7+ 的 ContextVar，自动跨异步任务传播
- 无需显式参数传递，任何地方都能通过 `get_trace_id()` 获取

**[app/core/logging_setup.py](app/core/logging_setup.py)** - JSON 日志格式

```python
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": get_trace_id(),
            "message": record.getMessage(),
        }
        extra_fields = getattr(record, "extra_fields", None)
        if isinstance(extra_fields, dict):
            payload.update(extra_fields)
        return json.dumps(payload, ensure_ascii=False)
```

使用方式：
```python
logger.info("agent.completed", extra={"extra_fields": {"state": "COMPLETED", "tool_count": 1}})
```

输出：
```json
{
  "timestamp": "2026-04-15T10:30:45.123456+00:00",
  "level": "INFO",
  "logger": "agent",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "agent.completed",
  "state": "COMPLETED",
  "tool_count": 1
}
```

**[app/middleware/request_context.py](app/middleware/request_context.py)** - 请求上下文中间件

```python
async def add_trace_and_logs(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or str(uuid.uuid4())
    set_trace_id(trace_id)
    
    start = time.time()
    response = await call_next(request)
    latency_ms = int((time.time() - start) * 1000)
    
    response.headers["x-trace-id"] = trace_id
    # 记录请求完成日志
    return response
```

流程：
1. 从请求头提取 `x-trace-id`（若无则生成 UUID）
2. 写入 ContextVar，后续所有日志自动关联
3. 响应头返回 `x-trace-id`，前端/调用方保留以便查询

#### 2. 状态机与 Agent 执行

**[app/agent/state_machine.py](app/agent/state_machine.py)** - 状态定义

```python
class AgentState(str, Enum):
    RECEIVED = "RECEIVED"          # 收到请求
    PLANNING = "PLANNING"          # 制定计划
    TOOL_RUNNING = "TOOL_RUNNING"  # 执行工具
    TOOL_FAILED = "TOOL_FAILED"    # 工具失败（强制或真实失败）
    ANSWERING = "ANSWERING"        # 生成答案
    COMPLETED = "COMPLETED"        # 完成

@dataclass
class AgentContext:
    message: str
    force_fail: bool = False
    evidence: list[dict] = field(default_factory=list)
    state: AgentState = AgentState.RECEIVED
    
    def transition(self, to_state: AgentState, reason: str) -> None:
        self.state = to_state
        self.evidence.append({
            "type": "state",
            "state": to_state.value,
            "reason": reason,
            "timestamp": datetime.now(UTC).isoformat()
        })
```

**[app/agent/chat_agent.py](app/agent/chat_agent.py)** - 执行引擎

```python
async def execute_agent(message: str, force_fail: bool = False) -> tuple[str, list[dict]]:
    ctx = AgentContext(message=message, force_fail=force_fail)
    ctx.transition(AgentState.PLANNING, "Request accepted")
    
    ctx.transition(AgentState.TOOL_RUNNING, "Run knowledge tool")
    
    # 强制失败或消息中含 FAIL_TOOL 触发失败分支
    if force_fail or "FAIL_TOOL" in message:
        ctx.transition(AgentState.TOOL_FAILED, "Forced failure branch enabled")
        answer = "本次执行失败：工具阶段被强制失败。请重试或关闭 force_fail。"
        logger.warning("agent.tool_failed", extra={"extra_fields": {"reason": "forced_failure"}})
        return answer, ctx.evidence
    
    # 正常执行工具
    tool_result = run_knowledge_lookup(message)
    ctx.evidence.append({"type": "tool", "name": "knowledge_lookup", "output": tool_result})
    
    ctx.transition(AgentState.ANSWERING, "Compose answer from tool evidence")
    topics = tool_result.get("matched_topics") or ["通用咨询"]
    answer = f"已基于工具结果完成回答。识别主题：{', '.join(topics)}。"
    
    ctx.transition(AgentState.COMPLETED, "Execution finished")
    return answer, ctx.evidence
```

执行流程：
1. 状态转移：RECEIVED → PLANNING → TOOL_RUNNING
2. 检查失败条件：`force_fail=true` 或消息含 `FAIL_TOOL`
3. 若失败，转入 TOOL_FAILED 并返回失败原因
4. 若成功，执行工具并收集证据，最后 ANSWERING → COMPLETED

#### 3. 工具执行

**[app/agent/tools/knowledge_tool.py](app/agent/tools/knowledge_tool.py)** - 真实工具示例

```python
def run_knowledge_lookup(message: str) -> dict:
    # 确定性工具：从消息识别医疗主题关键词
    result = {
        "tool": "knowledge_lookup",
        "input": message,
        "matched_topics": [
            topic for topic in ["高血压", "糖尿病", "挂号", "报告"]
            if topic in message
        ],
        "timestamp": datetime.now(UTC).isoformat(),
    }
    return result
```

特点：
- 返回结构化结果（dict）
- 可被追踪和回放：结果写入 evidence 和审计日志
- 失败可解释：若 force_fail=true，工具不执行，返回明确错误原因

#### 4. API 路由

**[app/routers/chat.py](app/routers/chat.py)** - 核心路由

**POST /internal/chat** - 非流式调用

请求：
```json
{
  "message": "我想挂号",
  "session_id": "s-12345",
  "user_id": "u-admin",
  "force_fail": false
}
```

响应：
```json
{
  "session_id": "s-12345",
  "answer": "已基于工具结果完成回答。识别主题：挂号。",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "evidence": [
    {
      "type": "state",
      "state": "PLANNING",
      "reason": "Request accepted",
      "timestamp": "2026-04-15T10:30:45.123456+00:00"
    },
    {
      "type": "state",
      "state": "TOOL_RUNNING",
      "reason": "Run knowledge tool",
      "timestamp": "2026-04-15T10:30:45.124000+00:00"
    },
    {
      "type": "tool",
      "name": "knowledge_lookup",
      "output": {
        "tool": "knowledge_lookup",
        "input": "我想挂号",
        "matched_topics": ["挂号"],
        "timestamp": "2026-04-15T10:30:45.124500+00:00"
      }
    },
    {
      "type": "state",
      "state": "COMPLETED",
      "reason": "Execution finished",
      "timestamp": "2026-04-15T10:30:45.125000+00:00"
    }
  ]
}
```

**POST /internal/chat/stream** - 流式调用

返回 `text/event-stream` 格式：

```
data: {"chunk":"已基于工具结果"}
data: {"chunk":"完成回答。"}
data: {"chunk":"识别主题："}
data: {"chunk":"挂号。"}
event: done
data: [DONE]
```

**GET /internal/chat/replay/:traceId** - 回放查询

从 PostgreSQL 中查询该 trace-id 的所有审计日志和对话记录，返回完整执行流程。

响应：
```json
{
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "audit_logs": [
    {
      "event_type": "chat.completed",
      "user_id": "u-admin",
      "session_id": "s-12345",
      "route": "/internal/chat",
      "details": {
        "trace_id": "550e8400-e29b-41d4-a716-446655440000",
        "force_fail": false,
        "evidence": [...]
      },
      "created_at": "2026-04-15T10:30:45.123456+00:00"
    }
  ],
  "conversations": [
    {
      "session_id": "s-12345",
      "user_id": "u-admin",
      "role": "user",
      "content": "我想挂号",
      "metadata": {"trace_id": "550e8400-e29b-41d4-a716-446655440000"},
      "created_at": "2026-04-15T10:30:45.123456+00:00"
    },
    {
      "session_id": "s-12345",
      "user_id": "u-admin",
      "role": "assistant",
      "content": "已基于工具结果完成回答。识别主题：挂号。",
      "metadata": {
        "trace_id": "550e8400-e29b-41d4-a716-446655440000",
        "evidence_count": 4
      },
      "created_at": "2026-04-15T10:30:45.126000+00:00"
    }
  ]
}
```

---

## 网关 NestJS 层

### 目录结构

```
gateway_nestjs/
├── src/
│   ├── main.ts                              # 启动文件
│   ├── modules/
│   │   ├── app.module.ts                    # 根模块，注册中间件
│   │   ├── auth/
│   │   │   ├── auth.module.ts
│   │   │   ├── auth.controller.ts           # POST /auth/login
│   │   │   ├── auth.service.ts              # JWT 生成与验证
│   │   │   ├── jwt.strategy.ts              # Passport 策略
│   │   │   ├── jwt-auth.guard.ts            # JWT 守卫
│   │   │   └── dto/
│   │   │       └── login.dto.ts
│   │   └── chat/
│   │       ├── chat.module.ts
│   │       ├── chat.controller.ts           # POST /chat, GET /chat/replay/:traceId
│   │       ├── chat.service.ts              # 代理到 FastAPI
│   │       └── dto/
│   │           └── chat.dto.ts
│   └── common/
│       └── middleware/
│           └── trace.middleware.ts          # Trace 中间件
├── Dockerfile
├── package.json
├── tsconfig.json
├── nest-cli.json
└── test/
    └── smoke.test.mjs
```

### 核心模块

#### 1. 鉴权流程

**POST /auth/login** - 登录

请求：
```json
{
  "username": "admin",
  "password": "admin123"
}
```

响应：
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "Bearer"
}
```

JWT Payload：
```json
{
  "sub": "admin",
  "username": "admin",
  "iat": 1713175845,
  "exp": 1713211845
}
```

JWT 配置（环境变量）：
- `JWT_SECRET`：签名密钥（生产必须更改）
- `JWT_EXPIRES_IN`：过期时间（默认 8h）

#### 2. 请求流程

```
客户端请求 POST /chat
  ↓
TraceMiddleware：生成/提取 x-trace-id，记录开始时间
  ↓
JwtAuthGuard：验证 Authorization: Bearer <token>
  ↓
ChatController：接收请求，提取 stream 和 force_fail 标志
  ↓
ChatService：代理到 FastAPI 内部服务，透传 x-trace-id
  ├─ stream=true  → 流式转发（Response.pipe）
  └─ stream=false → JSON 返回
  ↓
TraceMiddleware：记录响应状态和延迟，打印结构化日志
  ↓
响应客户端
```

#### 3. 流式转发机制

**[src/modules/chat/chat.controller.ts](src/modules/chat/chat.controller.ts)**

```typescript
@Post()
async chat(@Body() dto: ChatDto, @Req() req: AuthenticatedRequest, @Res() res: Response): Promise<void> {
  const traceId = req.traceId || 'missing-trace-id';

  if (dto.stream) {
    // 流式模式：直接管道 FastAPI 响应
    const upstream = await this.chatService.chatStream(dto, req.user, traceId);
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('x-trace-id', traceId);
    upstream.pipe(res);
    return;
  }

  // JSON 模式：等待完整响应再返回
  const response = await this.chatService.chat(dto, req.user, traceId);
  res.status(200).json(response);
}
```

特点：
- stream=true 时使用 Node Stream，避免在内存中缓存全量响应
- 流式块逐个转发给客户端，降低延迟和内存占用
- x-trace-id 继承并返回，便于前后端日志关联

#### 4. 限流配置

全局限流（ThrottlerModule）：

```typescript
ThrottlerModule.forRootAsync({
  inject: [ConfigService],
  useFactory: (cfg: ConfigService) => [
    {
      ttl: Number(cfg.get('THROTTLE_TTL', 60)) * 1000,      // 60 秒时间窗口
      limit: Number(cfg.get('THROTTLE_LIMIT', 30)),         // 每窗口最多 30 请求
    },
  ],
})
```

环境变量：
- `THROTTLE_TTL=60`（秒）
- `THROTTLE_LIMIT=30`（请求数）

超限返回 `429 Too Many Requests`。

---

## 前端 React 应用

### 目录结构

```
frontend_react/
├── src/
│   ├── App.jsx                    # 主应用组件
│   ├── main.jsx                   # 入口文件
│   └── styles.css                 # 样式表
├── index.html
├── Dockerfile
├── package.json
├── vite.config.ts
└── tsconfig.json
```

### 功能说明

**[src/App.jsx](src/App.jsx)** - 主组件

功能：
1. 快速登录（默认账号/密码：admin/admin123）
2. 消息输入与发送
3. 支持 `stream` 和 `json` 两种响应模式切换
4. `force_fail` 开关，用于演示失败分支
5. 实时显示 trace-id 和流式输出

调用流程：

```javascript
async function onSubmit(event) {
  // 发起请求
  const response = await fetch(`${API_BASE}/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({
      message,
      session_id: sessionId,
      stream: mode === 'stream',
      force_fail: forceFail,
    }),
  });

  setTraceId(response.headers.get('x-trace-id') || '');

  if (mode === 'json') {
    // JSON 模式：直接显示响应
    const data = await response.json();
    setOutput(JSON.stringify(data, null, 2));
    return;
  }

  // 流式模式：逐块读取并显示
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';
  
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split('\n\n');
    buffer = events.pop() || '';
    
    for (const eventBlock of events) {
      if (eventBlock.includes('event: done')) continue;
      if (!eventBlock.startsWith('data: ')) continue;
      
      const jsonLine = eventBlock.replace(/^data:\s*/, '');
      const parsed = JSON.parse(jsonLine);
      setOutput((prev) => prev + (parsed.chunk || ''));
    }
  }
}
```

UI 布局：
- 左面板：输入区（token / session / message / 开关 / 按钮）
- 右面板：输出区（trace-id + 流式或 JSON 结果）

---

## 数据持久化

### PostgreSQL 表结构

**[postgres/init.sql](postgres/init.sql)**

```sql
CREATE TABLE conversations (
  id SERIAL PRIMARY KEY,
  session_id VARCHAR(128) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  role VARCHAR(32) NOT NULL,
  content TEXT NOT NULL,
  metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE audit_logs (
  id SERIAL PRIMARY KEY,
  event_type VARCHAR(64) NOT NULL,
  user_id VARCHAR(128) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  route VARCHAR(255) NOT NULL,
  client_ip VARCHAR(64) NOT NULL DEFAULT '',
  details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_conversations_session_id ON conversations(session_id);
CREATE INDEX idx_conversations_user_id ON conversations(user_id);
CREATE INDEX idx_audit_logs_event_type ON audit_logs(event_type);
CREATE INDEX idx_audit_logs_user_id ON audit_logs(user_id);
```

**conversations** 表：
- 存储每一条对话消息（user 或 assistant）
- metadata_json 可存储 trace_id、evidence_count 等附加信息
- 用于会话历史与重放

**audit_logs** 表：
- 记录每个请求的事件（chat.completed / chat.failed）
- details_json 存储完整的 evidence 树和请求参数
- 用于审计、合规、问题追踪

### Redis 存储

**[app/redis_store.py](app/redis_store.py)**

**session:{session_id}** - 会话状态

```python
await update_session_state(
    session_id="s-12345",
    payload={
        "user_id": "u-admin",
        "last_message": "我想挂号",
        "trace_id": "550e8400-e29b-41d4-a716-446655440000"
    }
)
# 存储为 JSON，TTL 24 小时
```

**memory:{user_id}** - 用户短期记忆

```python
await append_memory(user_id="u-admin", role="user", content="我想挂号")
await append_memory(user_id="u-admin", role="assistant", content="识别主题：挂号。")
# 每个 item 为 JSON，最多保留 20 条（MEMORY_MAX_ITEMS），TTL 7 天
```

用途：
- session：维持多轮对话上下文
- memory：为 Agent 提供用户交互历史，便于上下文感知

---

## 状态机与 Agent 执行

### 完整执行链路

```
用户消息输入
  ↓
网关 /chat 路由
  ↓
FastAPI /internal/chat 路由
  ↓
execute_agent(message, force_fail)
  ├─ AgentContext 初始化（state=RECEIVED）
  │  └─ evidence = []
  │
  ├─ transition(PLANNING) → evidence 记录状态转移
  │
  ├─ transition(TOOL_RUNNING)
  │
  ├─ [检查失败条件]
  │  ├─ if force_fail or "FAIL_TOOL" in message
  │  │  └─ transition(TOOL_FAILED)
  │  │     └─ return (失败原因, evidence)
  │  │
  │  └─ else [正常执行]
  │     ├─ tool_result = run_knowledge_lookup(message)
  │     ├─ evidence.append({"type": "tool", ...})
  │     ├─ transition(ANSWERING)
  │     ├─ 生成答案
  │     └─ transition(COMPLETED)
  │
  └─ return (answer, evidence)
  
  ↓
写入数据库
  ├─ Conversation(role="user", content=message)
  ├─ Conversation(role="assistant", content=answer, metadata={evidence_count})
  └─ AuditLog(event_type="chat.completed", details={evidence})
  
  ↓
更新 Redis
  ├─ session:{session_id}
  └─ memory:{user_id}
  
  ↓
流式或 JSON 返回到网关
  
  ↓
网关转发到客户端
```

### 强制失败分支演示

场景：用户想测试 Agent 的失败处理

前端发送：
```json
{
  "message": "任意消息",
  "force_fail": true
}
```

Agent 执行：
1. RECEIVED → PLANNING → TOOL_RUNNING（状态流转正常记录）
2. 检查 `force_fail=true`，立即转入 TOOL_FAILED
3. 返回答案："本次执行失败：工具阶段被强制失败。请重试或关闭 force_fail。"
4. evidence 中包含 TOOL_FAILED 状态及原因
5. 审计日志 event_type 为 "chat.failed"

重放查询 GET /chat/replay/{trace_id}，可完整看到：
- 状态流转序列
- 失败原因
- 完整的时间戳

---

## 请求流程与 Trace

### 完整链路示意

```
1. 浏览器（5173）
   POST /chat
   Headers: Authorization: Bearer <token>
   Body: {message, session_id, stream, force_fail}

2. NestJS 网关（3000）
   TraceMiddleware:
     - 提取/生成 x-trace-id
     - set_trace_id(trace_id_ctx)
     - 记录 start_time
   
   JwtAuthGuard:
     - 验证 JWT token
     - 从 payload 提取 username / userId
   
   ChatController:
     - 接收 ChatDto
     - 调用 ChatService.chat() 或 .chatStream()
     - 根据 stream 标志选择响应方式
   
   Response headers:
     - x-trace-id: <原值或新生成>

3. FastAPI 服务（8000）
   middleware/request_context.py:
     - 提取 x-trace-id 头
     - set_trace_id(trace_id_ctx)
   
   routers/chat.py:
     - 验证 x-internal-token
     - 调用 execute_agent()
   
   core/logging_setup.py:
     - 所有日志自动关联 trace_id
   
   Response body:
     - {session_id, answer, trace_id, evidence}

4. 数据持久化
   PostgreSQL:
     - INSERT INTO conversations (trace_id metadata)
     - INSERT INTO audit_logs (details={evidence})
   
   Redis:
     - SET session:{sid} -> state_json
     - LPUSH memory:{uid} -> message_json

5. 响应链路反向
   FastAPI → NestJS → 浏览器
   
   浏览器收到：
     - Headers: x-trace-id
     - Body: {answer, evidence} (JSON 或流式)
   
   前端：
     - 显示 trace_id
     - 显示 answer 或流式块

6. 回放查询
   GET /chat/replay/550e8400-e29b-41d4-a716-446655440000
   
   FastAPI:
     - SELECT * FROM audit_logs WHERE details @> '{"trace_id": "..."}'
     - SELECT * FROM conversations WHERE metadata @> '{"trace_id": "..."}'
   
   返回：
     - 完整审计日志
     - 相关对话记录
     - 状态流转历史
```

### 日志示例

**请求到达网关**
```json
{
  "timestamp": "2026-04-15T10:30:45.100000+00:00",
  "level": "INFO",
  "logger": "request",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "request.completed",
  "method": "POST",
  "path": "/chat",
  "status_code": 200,
  "latency_ms": 156
}
```

**Agent 执行**
```json
{
  "timestamp": "2026-04-15T10:30:45.120000+00:00",
  "level": "INFO",
  "logger": "agent",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "agent.completed",
  "states": [
    {"state": "RECEIVED"},
    {"state": "PLANNING"},
    {"state": "TOOL_RUNNING"},
    {"state": "ANSWERING"},
    {"state": "COMPLETED"}
  ]
}
```

**失败分支**
```json
{
  "timestamp": "2026-04-15T10:30:45.115000+00:00",
  "level": "WARNING",
  "logger": "agent",
  "trace_id": "550e8400-e29b-41d4-a716-446655440000",
  "message": "agent.tool_failed",
  "reason": "forced_failure"
}
```

---

## 错误处理与失败恢复

### 错误分类

| 错误类型 | HTTP 状态 | 处理方式 | 用户感知 |
|---------|---------|--------|--------|
| 无效 JWT | 401 | 返回 Unauthorized，前端需重新登录 | 提示：令牌过期 |
| 超过限流 | 429 | 限流中间件拦截 | 提示：请求过于频繁 |
| FastAPI 不可用 | 502 | 网关捕获异常 | 提示：服务暂时不可用 |
| 强制失败 | 200 + TOOL_FAILED | Agent 明确返回失败原因 | 显示：执行失败原因 |
| 消息过长 | 400 | FastAPI 参数校验 | 提示：消息超过 5000 字 |
| 消息为空 | 400 | NestJS Dto 校验 | 提示：请输入消息 |

### 强制失败恢复流程

```
用户发送 force_fail=true
  ↓
Agent 执行到 TOOL_FAILED 状态
  ↓
返回明确的失败原因
  ↓
审计日志记录 event_type="chat.failed"
  ↓
用户可选择：
  ├─ 查看回放：GET /chat/replay/{trace_id}
  ├─ 重试：设置 force_fail=false 重新发送
  └─ 分析：通过 trace_id 查询完整日志
```

### 数据库失败恢复

FastAPI 在写入 PostgreSQL 失败时（网络中断等）：
- 捕获异常，记录 warning 日志
- 不中断流式响应（已开始流出数据）
- 前端可获得答案，但审计日志可能丢失
- 用户可通过 session_id 在 Redis 中查询状态

---

## 测试策略

### 后端单元测试

**[backend_fastapi/tests/test_agent_state_machine.py](backend_fastapi/tests/test_agent_state_machine.py)**

```python
@pytest.mark.asyncio
async def test_agent_success_has_completed_state() -> None:
    answer, evidence = await execute_agent("我想挂号")
    assert "识别主题" in answer
    states = [item["state"] for item in evidence if item.get("type") == "state"]
    assert "COMPLETED" in states

@pytest.mark.asyncio
async def test_agent_forced_failure_has_failed_state() -> None:
    answer, evidence = await execute_agent("任意消息", force_fail=True)
    assert "执行失败" in answer
    states = [item["state"] for item in evidence if item.get("type") == "state"]
    assert "TOOL_FAILED" in states
```

运行：
```bash
cd backend_fastapi
pytest -q
```

### 网关单元测试

**[gateway_nestjs/test/smoke.test.mjs](gateway_nestjs/test/smoke.test.mjs)**

```javascript
test('boolean coercion for stream flag', () => {
  const toStream = Boolean(true);
  assert.equal(toStream, true);
});
```

运行：
```bash
cd gateway_nestjs
npm run test
```

### 集成测试（本地）

使用 Docker Compose 启动全栈，然后：

```bash
# 1. 登录获取 token
curl -X POST http://localhost:3000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 2. 发送消息（JSON）
curl -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"我想挂号","session_id":"s1","stream":false,"force_fail":false}'

# 3. 发送消息（流式）
curl -N -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"我想挂号","session_id":"s1","stream":true,"force_fail":false}'

# 4. 测试失败分支
curl -X POST http://localhost:3000/chat \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"message":"任意","session_id":"s1","stream":false,"force_fail":true}'

# 5. 回放查询
curl http://localhost:3000/chat/replay/<trace_id> \
  -H "Authorization: Bearer <token>"
```

---

## 本地开发与部署

### 快速启动

```bash
# 1. 复制环境变量
cp .env.stack.example .env

# 2. 启动所有服务（Docker Compose）
docker compose up --build -d

# 3. 查看日志
docker compose logs -f

# 4. 停止服务
docker compose down
```

### 服务健康检查

```bash
# FastAPI
curl http://localhost:8000/health

# NestJS（无认证）
# 注：/health 也应该开放（可修改 JwtAuthGuard 配置）

# 前端
# 直接打开 http://localhost:5173
```

### 本地开发模式

**后端**
```bash
cd backend_fastapi
python -m venv .venv
source .venv/bin/activate  # 或 Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**网关**
```bash
cd gateway_nestjs
npm install
npm run start:dev
```

**前端**
```bash
cd frontend_react
npm install
npm run dev
```

然后访问 http://localhost:5173

### 环境变量详解

**根目录 .env**

```env
# PostgreSQL
POSTGRES_DB=chatdb
POSTGRES_USER=chat
POSTGRES_PASSWORD=chat

# JWT
JWT_SECRET=change-me-in-prod
JWT_EXPIRES_IN=8h

# Demo 用户
DEMO_USER=admin
DEMO_PASSWORD=admin123

# 限流
THROTTLE_TTL=60
THROTTLE_LIMIT=30

# FastAPI
INTERNAL_API_TOKEN=change-me-in-prod
MEMORY_MAX_ITEMS=20
```

生产环境必须修改：
- `POSTGRES_PASSWORD`
- `JWT_SECRET`
- `INTERNAL_API_TOKEN`

### 构建与推送镜像

```bash
# 构建
docker build -t chat-ai-fastapi:latest ./backend_fastapi
docker build -t chat-ai-nestjs:latest ./gateway_nestjs
docker build -t chat-ai-frontend:latest ./frontend_react

# 推送（假设已登录 Docker Hub）
docker tag chat-ai-fastapi:latest <your-registry>/chat-ai-fastapi:latest
docker push <your-registry>/chat-ai-fastapi:latest
# ... 同理 nestjs 和 frontend
```

### Kubernetes 部署（示例）

```yaml
# backend-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chat-ai-fastapi
spec:
  replicas: 2
  template:
    spec:
      containers:
      - name: fastapi
        image: <registry>/chat-ai-fastapi:latest
        ports:
        - containerPort: 8000
        env:
        - name: POSTGRES_DSN
          value: postgresql+asyncpg://chat:password@postgres:5432/chatdb
        - name: REDIS_URL
          value: redis://redis:6379/0
        - name: INTERNAL_API_TOKEN
          valueFrom:
            secretKeyRef:
              name: api-tokens
              key: internal
```

### 监控与日志收集

后端输出 JSON 日志，可直接被 ELK / Datadog / Splunk 等采集：

```bash
# 启用日志收集（示例：docker logs）
docker logs chatai-fastapi | jq '.trace_id, .level, .message'
```

---

## 总结

### 设计亮点

1. **完全可追踪**：trace-id 贯穿全链路，每个请求都有唯一 ID
2. **证据可回放**：所有状态转移、工具调用、答案都持久化，可完整重现执行过程
3. **失败可解释**：强制失败分支让 Agent 能演示"不会乱编"，失败原因清晰
4. **高效流式**：SSE 流式输出，实时响应，无需等待完整答案
5. **模块化架构**：网关 / 后端 / 工具 / 工具完全解耦，易于扩展

### 后续扩展方向

1. **更多工具**：在 `app/agent/tools/` 中添加医疗、查询、解释等工具
2. **多 Agent 编排**：使用 ReAct 或 LLM-based planner 替代当前简单状态机
3. **完整医疗流程**：迁移现有 Flask app.py 的医疗逻辑到新架构
4. **向量数据库**：集成 Weaviate / Milvus 用于 RAG
5. **实时协作**：添加 WebSocket 支持多用户实时编辑
6. **性能优化**：缓存、批量处理、异步任务队列（Celery）

---

## 2026-04-15 增量更新（Decision Agent 专章）

> 本节用于覆盖文档中旧描述与当前代码不一致的部分，尤其是 Decision 相关能力。

### 1) Decision Agent 现在到底在做什么

Decision Agent 是一个“可回放、可审计”的决策流水线，不是闲聊型回答器。

它的标准链路是：

1. Create：写入 `decision_record`（状态 `draft`）
2. Run：执行 LangGraph 节点流（Normalize -> Plan -> PermissionGate -> ToolExecute -> ToolVerify -> EvidenceQualityGate -> BuildDecisionRecord -> Finalize）
3. Replay：查询整条链路事件、工具调用、证据项

当前关键接口：

- Gateway（NestJS，对前端暴露）
  - `POST /decisions`
  - `POST /decisions/:decisionId/run`
  - `GET /decisions/:decisionId/replay`
- FastAPI（后端真实执行）
  - `POST /decisions`
  - `POST /decisions/{decision_id}/run`
  - `GET /decisions/{decision_id}/replay`

### 2) 你问的这个问题，Decision agent 应该怎么回答

问题：`我在悉尼是否值得去 Kiama 玩？`

Decision Agent 的回答不应该只是 `decisionId`，而应该至少包含以下结构：

1. Recommendation：建议结论（如 `PROCEED` / `NEEDS_REVIEW`）
2. Confidence：置信度（如 `0.75`）
3. Rationale：依据要点（可对应证据 ID）
4. Boundary：不确定性边界（缺了哪些关键信息）
5. Next Step：下一步动作（继续补充数据或人工确认）

#### 当前系统的“真实行为（按现有代码）”

在当前 demo 实现下，这个问题通常会输出：

- `recommendation = PROCEED`
- `confidence = 0.75`
- rationale 为通用语句（基于“已检索到证据”）

原因：当前 Tool 层是示例工具（fake retriever），并未真正接入天气、交通、预算、拥挤度、偏好等旅游决策数据源。

#### 理想中的“业务可用回答”示例（建议模板）

```text
结论：建议去（PROCEED）
置信度：中等（约 0.65 ~ 0.8，取决于天气与交通实时数据）
依据：
1) 行程距离与可达性在可接受范围
2) 若天气良好，体验收益高
3) 你的时间成本与预算可承受

不确定性：
- 当日天气（降雨/海况）
- 交通拥堵与往返时间波动
- 你对“海边景观/徒步”的偏好强度

建议动作：
- 出发前检查天气和路况
- 若天气一般，改为半日行程或替代目的地
```

也就是说：

- Decision agent 的正确输出是“可解释决策结论”
- 不是“百科式长回答”
- 更不是“只返回 ID”

### 3) 前端现状更新（已生效）

Decision 页面已经改为“默认模式 + 一键可读结论”：

1. 应用默认进入 Decision Studio（不是 Medical Chat）
2. `Create and Run` 一次点击完成 create + run
3. 右侧新增 `Assistant summary`，展示可读结论（结论、置信度、依据）
4. 保留 `Raw payload` 便于调试和审计

### 4) 最近修复的关键故障（避免再次踩坑）

1. 网关缺少 `/decisions` 路由 -> 前端 `Cannot POST /decisions`（404）
2. LangGraph 异步节点包装错误 -> `InvalidUpdateError`（run 503）
3. `state["gates"]` 可能为 `None` -> `TypeError`（run 503）
4. 证据主键冲突（`E1` 重复）-> `UniqueViolationError`（run 503）

当前实现已做修复：

- Decision 路由完整挂载到 NestJS
- 异步节点用 await-able wrapper
- `gates/errors/tool_queue/tool_results` 增加空值容错
- run 前按 `decision_id` 清理旧 evidence，并使用 `decision_id + evidenceId` 作为唯一键

### 5) 对“Kiama 问题”的落地建议

如果要让这个问题真正“像决策助手”，下一步应把 Tool 层从 demo 升级为真实外部信号：

1. 天气 API（当天与次日）
2. 路况/通勤时长 API
3. 景点开放状态与拥挤度信号
4. 预算参数（油费/通行成本/时间成本）
5. 用户偏好（自然景观/拍照/徒步/亲子）

完成后，Decision 输出就能从“通用 PROCEED”升级为“带条件的可执行建议”。

