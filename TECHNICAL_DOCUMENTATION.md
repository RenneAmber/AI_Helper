# ChatAI 项目技术文档

## 1. 文档范围
本文档面向后端工程师、架构师与运维同学，覆盖：
- 系统架构与模块职责
- 关键数据模型与接口协议
- Agent 与检索策略
- 可观测性、故障定位与质量保障
- 新增岗位职责导向能力规划模块设计

## 2. 架构分层

### 2.1 v3 目标架构
- Gateway（NestJS）
  - 对外 API
  - JWT 鉴权
  - 限流
  - SSE 转发
- Backend（FastAPI）
  - Chat/Medical/Decision/Copilot 业务
  - 审计日志与对话存储
  - Agent 执行与证据输出
- Storage
  - Postgres：conversations、audit_logs、decision 相关数据
  - Redis：session state、memory cache
- Frontend（React）
  - 登录、对话、流式展示

### 2.2 v2 兼容链路
- Flask 单体应用保留作为兼容演示路径

## 3. 关键模块说明

### 3.1 FastAPI 入口
文件：backend_fastapi/app/main.py
- 注册 health/chat/medical/decisions/copilot/capability-planning 路由
- startup 阶段初始化数据库模型

### 3.2 Chat 路由
文件：backend_fastapi/app/routers/chat.py
- 内部对话入口
- 支持非流式和流式
- 对话和审计日志落库
- replay 接口支持 trace 维度回放

### 3.3 决策模块
文件：backend_fastapi/app/routers/decisions.py
- 对接 Copilot 决策流
- 旧 run/replay 接口已标记废弃（410）

### 3.4 新增能力规划模块
文件：backend_fastapi/app/routers/capability_planning.py
文件：backend_fastapi/app/schemas_capability_planning.py

设计目标：
- 直接映射岗位职责，给出从需求到交付的执行蓝图
- 输出覆盖步骤 1-8，前 7 步自动生成，第 8 步等待业务优先级补充

核心接口：
- GET /internal/capability-planning/bootstrap/template
- POST /internal/capability-planning/bootstrap

安全机制：
- 复用 x-internal-token 校验

审计机制：
- 事件名 capability.plan.generated
- details_json 记录 trace_id、audience、focus、timeline_days、scenario_count

## 4. 数据模型与存储

### 4.1 conversations
用途：
- 存储用户与助手消息
关键字段：
- session_id, user_id, role, content, metadata_json, created_at

### 4.2 audit_logs
用途：
- 存储执行事件与上下文，支持链路排障
关键字段：
- event_type, user_id, session_id, route, details_json, created_at

### 4.3 能力规划模块输出模型
关键对象：
- StepSummary
- UserStory
- ArchitectureBlock
- DataFlowStep
- SearchStrategy
- ApiContract
- OpsChecklist

## 5. API 协议设计要点

### 5.1 统一约束
- 输入参数长度限制与必填校验
- 输出结构化 JSON，避免纯文本不可解析结果
- trace_id 贯穿日志与回放

### 5.2 能力规划请求示例
```json
{
  "project_name": "Data Intelligence Platform Capability Plan",
  "audience": "interviewer",
  "focus": "balanced",
  "timeline_days": 7,
  "scenarios": [
    {
      "name": "数据智能抽取",
      "summary": "从复杂文本抽取结构化数据并落库",
      "priority": 1
    }
  ],
  "constraints": [
    "优先可演示"
  ]
}
```

## 6. Agent 与检索策略

### 6.1 策略分层
- L1 关键词检索：保证可解释精确召回
- L2 向量检索：提高语义覆盖
- L3 规则过滤：去重、裁剪低质量结果

### 6.2 重排策略
- 相关性、时效性、可信度加权
- 低分结果触发澄清提问，降低幻觉

### 6.3 Prompt 原则
- 仅依据证据输出
- 固定结构输出，便于后处理
- 显式标注不确定性

## 7. 可观测性与故障定位

### 7.1 建议指标
- 接口 5xx 比例
- p95 延迟
- 工具调用成功率
- evidence 命中率

### 7.2 告警建议
- 5xx_rate > 1%（5 分钟窗口）
- p95_latency_ms > 1500（连续 10 分钟）
- tool_success_rate < 98%（15 分钟窗口）

### 7.3 故障定位流程
1. 从网关响应头或日志获取 trace_id
2. 调用 replay 接口回放事件与对话
3. 对照 audit_logs 查看工具执行细节
4. 结合 metrics 判断是依赖故障、策略故障还是数据质量问题

## 8. 工程质量保障

### 8.1 代码质量
- Python: ruff + pytest
- TypeScript: tsc --noEmit + node --test

### 8.2 交付基线
- 本地可一键启动
- 有健康检查与回放能力
- 有最小测试用例
- 有详细用户手册与技术文档

## 9. 云原生演进建议
1. 将工具调用异步化，引入消息队列削峰
2. 增加工作流编排与重试补偿
3. 建立多环境发布流水线和灰度策略
4. 增加向量库分层索引与冷热数据治理

## 10. 变更记录
- 新增岗位职责导向能力规划模块
- 接口命名从 demo 语义升级为 capability-planning 语义
- 文档体系新增用户手册和技术文档
