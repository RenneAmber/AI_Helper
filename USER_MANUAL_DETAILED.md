# ChatAI 项目详细用户手册

## 1. 文档目标
本手册面向产品同学、演示人员和测试人员，帮助你在最短时间内完成以下目标：
- 启动系统并验证服务状态
- 使用核心功能进行对话、医疗分流、决策 Copilot 和岗位职责导向能力规划
- 回放请求链路并定位常见问题
- 按步骤完成演示流程

## 2. 系统入口总览
本项目存在两条可运行路径：
- 路径 A（推荐）：v3 架构，FastAPI + NestJS + Postgres + Redis + React
- 路径 B（兼容）：v2 Flask 单体演示链路

常用访问地址（默认）：
- 网关（NestJS）：http://localhost:3000
- 内部服务（FastAPI）：http://localhost:8000
- 前端（React）：http://localhost:5173
- Flask 演示页：http://localhost:5000

## 3. 快速启动

### 3.1 Docker Compose 启动（推荐）
1. 在项目根目录复制环境变量模板：
   - Windows PowerShell:
     - Copy-Item .env.stack.example .env
2. 启动所有服务：
   - docker compose up --build -d
3. 查看状态：
   - docker compose ps
4. 查看日志：
   - docker compose logs -f

### 3.2 本地脚本启动（兼容）
1. 激活虚拟环境并安装依赖
2. 运行：
   - python start_all_services.py
3. 访问：
   - http://localhost:5000

## 4. 登录与鉴权
对外网关需要 JWT。

1. 获取 token：
- POST http://localhost:3000/auth/login
- Body:
```json
{
  "username": "admin",
  "password": "admin123"
}
```

2. 在后续请求 Header 中带上：
- Authorization: Bearer <token>

## 5. 核心功能使用

### 5.1 普通对话
- 接口：POST http://localhost:3000/chat
- 作用：默认问答与智能分流
- 示例请求：
```json
{
  "message": "帮我总结一下今天重点",
  "session_id": "s1",
  "stream": false
}
```

### 5.2 流式对话
- 接口：POST http://localhost:3000/chat
- 参数：stream=true
- 说明：返回 SSE，适合前端逐字展示

### 5.3 医疗分诊（内部）
- 接口：POST http://localhost:8000/internal/medical/triage
- Header：x-internal-token
- 示例：
```json
{
  "complaint": "胸痛伴随呼吸困难"
}
```

### 5.4 决策 Copilot
- 接口：POST http://localhost:8000/decisions
- 作用：提交问题与评估维度，返回结构化决策结果

### 5.5 岗位职责导向能力规划（新）
用于快速产出从需求到交付的 1-8 步方案，贴合 Job Responsibility。

1. 获取模板：
- GET http://localhost:8000/internal/capability-planning/bootstrap/template
- Header：x-internal-token

2. 生成定制计划：
- POST http://localhost:8000/internal/capability-planning/bootstrap
- Header：x-internal-token
- 示例请求：
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
    },
    {
      "name": "智能检索分析",
      "summary": "结合关键词和向量检索进行证据化分析",
      "priority": 2
    },
    {
      "name": "标注反馈回流",
      "summary": "把人工反馈回流到策略优化流程",
      "priority": 3
    },
    {
      "name": "AI 写作应用",
      "summary": "基于证据生成可编辑内容草稿",
      "priority": 4
    }
  ],
  "constraints": [
    "优先可演示",
    "优先可观测性"
  ]
}
```

返回中包含：
- step_status（已完成到第 7 步，第 8 步待你补业务优先级）
- user_stories（按岗位职责映射）
- architecture_blocks
- data_intelligence_flow
- agent_search_strategy
- api_contracts
- production_ops_checklist

## 6. 演示流程建议（20 分钟）
1. 登录获取 token（2 分钟）
2. 调用普通对话与流式对话（5 分钟）
3. 调用能力规划接口展示 1-8 步方案（5 分钟）
4. 演示回放与 trace（5 分钟）
5. 展示故障注入与定位思路（3 分钟）

## 7. 回放与排障

### 7.1 回放接口
- GET http://localhost:8000/internal/chat/replay/{trace_id}
- Header：x-internal-token

### 7.2 常见问题
1. 问题：401 Invalid internal token
- 处理：确认 x-internal-token 与服务配置一致

2. 问题：无法连接 Postgres/Redis
- 处理：确认 docker compose ps 中 postgres、redis healthy

3. 问题：网关 502 或超时
- 处理：检查 fastapi 日志和 network 连接

4. 问题：流式输出中断
- 处理：确认网关与前端未被代理层缓存或超时截断

## 8. 输入规范建议
- message 长度尽量小于 5000 字符
- session_id 和 user_id 保持稳定，便于上下文连续
- 场景描述要包含动作和目标，例如“检索 + 重排 + 可解释输出”

## 9. 安全与合规提示
- 默认账号仅用于本地演示
- 上线前务必替换 JWT_SECRET、INTERNAL_API_TOKEN、DEMO_PASSWORD
- 医疗相关内容仅作技术演示，不作为诊疗建议

## 10. 附录：常用命令
- 启动：docker compose up --build -d
- 状态：docker compose ps
- 日志：docker compose logs -f fastapi
- 停止：docker compose down
