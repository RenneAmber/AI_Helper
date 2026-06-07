# Decision Making System - 完整架构

## 概览

这是一个"可回放、可追责、可审计"的 LangGraph 决策制定系统。

**核心 MVP 三件事：**
1. `POST /decisions` - 创建决策请求
2. `POST /decisions/{id}/run` - 运行 LangGraph，每个节点都写事件日志
3. `GET /decisions/{id}/replay` - 完整回放决策执行过程

## 关键设计

### 1. 不会"假装调用工具"

**分离 ToolExecute 和 ToolVerify：**

```
ToolExecute 节点：
  ├─ 真实调用工具（fake_retriever / fake_log_query）
  ├─ 写 tool_runs 表（开始时 status=running）
  ├─ 捕获输出或错误
  └─ 标记 status=success/failure，记录输出 hash

ToolVerify 节点：
  ├─ 验证工具输出结构
  ├─ 转换成 evidence_pack 格式
  ├─ 检测冲突和缺陷
  └─ 通过则继续，失败则标记 errors
```

每个工具执行都在 `tool_runs` 表中记录完整的：
- run_id（唯一标识）
- input_hash（SHA256，防篡改）
- output_hash（SHA256，防篡改）
- 执行时间
- 错误信息（若失败）

### 2. 事件日志贯穿全流程

**每个节点的进出都写 decision_events 表：**

```python
await write_event(
    repo,
    decision_id,
    "NODE_START",      # 或 NODE_END / TOOL_RUN
    "ToolExecute",     # 节点名称
    "success",         # 状态
    {"tool_queue": [...], ...}  # 完整 payload
)
```

事件流示例：

```
RECEIVED → PLANNING → TOOL_RUNNING
↓ (ToolExecute)
  tool_runs[1]: run_id=xxx, status=success, output_hash=sha256:...
↓ (ToolVerify)
  evidence_pack = {...}
↓ (EvidenceQualityGate)
  result=pass/fail
↓ (BuildDecisionRecord)
  decision_out = {recommendation: "PROCEED", confidence: 0.75}
↓ (Finalize)
  final_status = "final"
```

### 3. 完整的回放能力

**GET /decisions/{id}/replay 返回：**

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
    {
      "time": "2026-04-15T10:30:45.110000+00:00",
      "type": "NODE_END",
      "node": "Normalize",
      "status": "success",
      "payload": {...}
    },
    ...
  ],
  "toolRuns": [
    {
      "run_id": "xxx",
      "tool_name": "fake_retriever",
      "status": "success",
      "input_hash": "sha256:...",
      "output_hash": "sha256:...",
      "started_at": "2026-04-15T10:30:45.150000+00:00",
      "ended_at": "2026-04-15T10:30:45.160000+00:00"
    }
  ],
  "evidenceItems": [...]
}
```

用户/审计人员可以：
- 查看每一步发生了什么
- 验证工具调用的真实性（通过 hash）
- 追踪证据如何转换成最终决策
- 复现完整执行

## 数据库架构

### 表结构

#### decisions
```sql
decision_id (PK)      -- D-20260415-abc12345
schema_version        -- decision_record.v1
title, question, domain
status                -- draft, running, final, aborted
requester_user_id
context_json          -- 结构化输入（JSON）
criteria_json         -- 评估标准列表
plan_json             -- 执行计划
analysis_json         -- 分析结果
decision_json         -- 最终决策
followup_json         -- 后续行动
created_at, updated_at
```

#### decision_events
```sql
event_id (PK)         -- UUID
decision_id (FK)      -- 关联决策
event_type            -- NODE_START / NODE_END / TOOL_RUN
node_name             -- Normalize / ToolExecute / etc
status                -- success / failure
payload_json          -- 完整上下文（JSON）
created_at            -- 时间戳
```

**索引：**
- idx_events_decision_id（快速查询某决策的所有事件）
- idx_events_created_at（时间序列查询）
- idx_events_node_name（按节点过滤）

#### tool_runs
```sql
run_id (PK)           -- UUID
decision_id (FK)
tool_name             -- fake_retriever / fake_log_query / etc
status                -- running / success / failure
started_at, ended_at
input_hash            -- SHA256(input)
output_hash           -- SHA256(output)
error_code, error_message
```

**索引：**
- idx_tool_runs_decision_id（快速查询某决策的所有工具执行）
- idx_tool_runs_status（过滤失败的工具）

#### evidence_items
```sql
evidence_id (PK)      -- E1, E2, ...
decision_id (FK)
kind                  -- doc / log / metric / etc
source_type           -- internal_doc / audit_log / db / etc
source_uri            -- 数据源地址
quote                 -- 关键引用
signals_json          -- {recencyDays, reliability, relevance}
tags_json             -- 标签列表
content_hash          -- SHA256(full_evidence)
retrieved_at
```

## LangGraph 流程

### 节点顺序（MVP - 线性）

```
1. Normalize
   ├─ 输入：raw request
   ├─ 操作：补齐缺失字段、验证类型
   └─ 输出：normalized structure

2. Plan
   ├─ 输入：normalized structure
   ├─ 操作：制定执行计划（MVP：固定三步）
   └─ 输出：plan steps + tool_queue

3. PermissionGate
   ├─ 输入：constraints
   ├─ 操作：权限检查（MVP：直接通过）
   └─ 输出：gates.permission = {result: "pass"}

4. ToolExecute ⭐ 关键
   ├─ 输入：tool_queue
   ├─ 操作：
   │   ├─ 为每个工具创建 tool_runs 记录
   │   ├─ 真实调用工具
   │   └─ 写入执行结果和 hash
   └─ 输出：tool_results[]

5. ToolVerify ⭐ 关键
   ├─ 输入：tool_results
   ├─ 操作：
   │   ├─ 验证输出结构
   │   ├─ 转换成 EvidencePack
   │   └─ 记录任何问题到 errors
   └─ 输出：evidence_pack, errors[]

6. EvidenceQualityGate
   ├─ 输入：evidence_pack
   ├─ 操作：检查是否有足够证据
   └─ 输出：gates.evidence_quality

7. BuildDecisionRecord
   ├─ 输入：evidence_pack, gates
   ├─ 操作：生成建议和置信度
   └─ 输出：decision_out

8. Finalize
   ├─ 输入：decision_out
   ├─ 操作：确定最终状态
   └─ 输出：final_status (final / draft)
```

### 后续可扩展的节点

```
ConflictDetection
  ├─ 检查证据之间的冲突
  └─ 输出：conflicts[]

HumanReview（条件边）
  ├─ 若 confidence < 0.6，路由到人工审核
  ├─ 获取审核结果
  └─ 返回 BuildDecisionRecord 重新生成

FallbackRetrieve（失败恢复）
  ├─ 若 ToolExecute 失败
  ├─ 尝试备用数据源
  └─ 重新进入 ToolVerify

ParallelToolExecution
  ├─ 并行调用多个工具
  ├─ 使用 gather() 等待所有完成
  └─ 收集结果
```

## API 使用示例

### 1. 创建决策

```bash
curl -X POST http://localhost:8000/decisions \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Should we migrate our RAG orchestration to LangGraph?",
    "domain": "engineering",
    "requester": {
      "userId": "u_123",
      "displayName": "Litian"
    },
    "context": {
      "system": "medical-agent",
      "background": "Current system using basic orchestration",
      "constraints": ["audit_required", "low_hallucination"],
      "riskPosture": "low",
      "timeHorizonDays": 90
    },
    "criteria": [
      {"key": "reliability", "weight": 0.35},
      {"key": "cost", "weight": 0.25},
      {"key": "adoption", "weight": 0.40}
    ]
  }'

# 返回：
# {
#   "decisionId": "D-20260415-abc12345",
#   "status": "draft"
# }
```

### 2. 运行决策

```bash
curl -X POST http://localhost:8000/decisions/D-20260415-abc12345/run \
  -H "Authorization: Bearer <token>"

# 返回：
# {
#   "decisionId": "D-20260415-abc12345",
#   "status": "final",
#   "decision": {
#     "recommendation": "PROCEED",
#     "confidence": 0.75,
#     "rationale": [...],
#     "safetyNotes": [...]
#   }
# }
```

### 3. 回放决策

```bash
curl http://localhost:8000/decisions/D-20260415-abc12345/replay \
  -H "Authorization: Bearer <token>"

# 返回：
# {
#   "decisionId": "D-20260415-abc12345",
#   "events": [...],
#   "toolRuns": [...],
#   "evidenceItems": [...]
# }
```

## 前端界面

### 三个模式

1. **Create Decision**
   - 输入：question, domain, constraints, risk_posture
   - 输出：decision_id
   - 前进到 "Run Decision" 模式

2. **Run Decision**
   - 输入：decision_id
   - 输出：最终决策 + 置信度
   - 观察 JSON 结构

3. **Replay Decision**
   - 输入：decision_id
   - 输出：完整事件流 + 工具运行 + 证据项
   - 用于审计和调试

## 生产推广清单

- [ ] 替换 fake_retriever 为真实 RAG（向量数据库）
- [ ] 替换 fake_log_query 为真实日志系统（ELK / Splunk）
- [ ] 添加 ConflictDetection 节点
- [ ] 添加 HumanReview 节点和前端审核 UI
- [ ] 集成企业权限系统到 PermissionGate
- [ ] 添加更多工具（比如成本计算、性能基准）
- [ ] 实现决策版本控制（可对比历史决策）
- [ ] 决策执行后评估（记录实际结果 vs 预测）
- [ ] 仪表板（显示决策趋势、工具可靠性等）
- [ ] WebSocket 流式更新（实时看图执行过程）

## 与 Chat 系统的对比

| 功能 | Chat Agent | Decision Making |
|------|-----------|-----------------|
| 可回放 | ✓ | ✓✓✓ 完整事件流 |
| 工具验证 | 基本 | ✓✓ 分离 Execute/Verify |
| 证据审计 | ✓ | ✓✓✓ 完整 Evidence Pack |
| 失败解释 | ✓ | ✓✓ 强制关卡检查 |
| 责任链 | user → message | requester → question → criteria → evidence → decision |
| 后续追踪 | 无 | ✓ FollowUp tasks |
| 冲突检测 | 无 | ✓ ConflictDetection |
| 人工审核 | 无 | ✓ HumanReview gate |

---

**核心护城河：** 普通 AI Chat 没有"可回放/可追责/可审计"的能力，只能说"我生成了一个答案"。而这个决策系统能说"我用这些证据，通过这些关卡，用这个逻辑生成了建议，你可以完整重现整个过程"。
