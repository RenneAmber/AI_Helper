from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..core.trace import get_trace_id
from ..database import get_db
from ..models import AuditLog
from ..schemas_capability_planning import (
    ApiContract,
    ArchitectureBlock,
    CapabilityPlanRequest,
    CapabilityPlanResponse,
    DataFlowStep,
    OpsChecklist,
    SearchStrategy,
    StepSummary,
    UserStory,
)

router = APIRouter(prefix="/internal/capability-planning", tags=["capability-planning"])


async def verify_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    if x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal token")


def _goal_statement(payload: CapabilityPlanRequest) -> str:
    top_items = ", ".join(item.name for item in sorted(payload.scenarios, key=lambda x: x.priority)[:3])
    return (
        f"在 {payload.timeline_days} 天内完成 {payload.project_name} 的岗位职责导向 MVP，"
        f"优先交付 {top_items}，面向 {payload.audience}，并突出 {payload.focus}。"
    )


def _user_stories(payload: CapabilityPlanRequest) -> list[UserStory]:
    stories: list[UserStory] = []
    for item in sorted(payload.scenarios, key=lambda x: x.priority):
        stories.append(
            UserStory(
                scenario=item.name,
                user_story=(
                    f"作为平台业务方，我希望系统可完成{item.summary}，"
                    "以支持数据智能抽取分析、标注反馈闭环与可解释交付。"
                ),
                acceptance=[
                    "API 返回结构化字段并含 trace_id",
                    "输出含 evidence 便于追溯和复盘",
                    "支持可控失败注入并返回定位信息",
                ],
                responsibility_mapping=[
                    "后端需求拆解、架构设计与开发",
                    "AI Agent 系统与智能检索策略设计",
                    "生产问题定位、代码质量与架构优化",
                ],
            )
        )
    return stories


def _architecture_blocks() -> list[ArchitectureBlock]:
    return [
        ArchitectureBlock(
            name="Backend API Service",
            responsibility="统一对外接口、鉴权、参数校验、响应协议",
            interfaces=["POST /chat", "POST /internal/chat", "POST /internal/capability-planning/bootstrap"],
        ),
        ArchitectureBlock(
            name="Agent Workflow",
            responsibility="意图识别、任务编排、工具路由、答案生成",
            interfaces=["planning_node", "tool_router_node", "answer_node"],
        ),
        ArchitectureBlock(
            name="Search Strategy",
            responsibility="关键词召回、向量召回、重排、证据裁剪",
            interfaces=["keyword_search", "vector_search", "rerank", "evidence_filter"],
        ),
        ArchitectureBlock(
            name="Data Intelligence Loop",
            responsibility="抽取、分析、标签反馈、策略迭代",
            interfaces=["ingest", "extract", "feedback", "policy_update"],
        ),
        ArchitectureBlock(
            name="Observability and Operations",
            responsibility="日志、追踪、回放、故障定位",
            interfaces=["audit_logs", "trace_id", "replay", "metrics"],
        ),
    ]


def _data_flow() -> list[DataFlowStep]:
    return [
        DataFlowStep(
            stage="Ingestion",
            input="原始文本、业务请求、历史上下文",
            process="字段规整、敏感信息清洗、上下文拼装",
            output="可处理请求对象",
            observable="ingest_latency_ms",
        ),
        DataFlowStep(
            stage="Extraction",
            input="可处理请求对象",
            process="实体抽取、标签抽取、结构化转写",
            output="结构化特征与标签",
            observable="extract_success_rate",
        ),
        DataFlowStep(
            stage="Analysis",
            input="结构化特征 + 检索证据",
            process="Agent 打分、冲突消解、结果排序",
            output="候选答案与置信度",
            observable="answer_confidence",
        ),
        DataFlowStep(
            stage="Label Feedback",
            input="候选答案",
            process="人工校正、误差标注、反馈入库",
            output="高质量反馈样本",
            observable="feedback_coverage",
        ),
        DataFlowStep(
            stage="Policy Update",
            input="反馈样本",
            process="提示词特征工程、规则更新、检索参数更新",
            output="策略新版本",
            observable="version_gain",
        ),
    ]


def _search_strategy() -> SearchStrategy:
    return SearchStrategy(
        retrieval_layers=[
            "L1: 关键词检索，保障高精度命中",
            "L2: 向量检索，保障语义召回",
            "L3: 规则过滤与去重，减少噪声",
        ],
        rerank_policy="按相关性、时效性、可信度加权重排；低于阈值触发澄清问题",
        fallback_policy="检索为空时回退到保守回答模板，并要求用户补充关键信息",
        prompt_principles=[
            "只基于证据回答，不编造来源",
            "输出固定 JSON 结构，便于后处理",
            "不确定时明确标注不确定性",
        ],
    )


def _api_contracts() -> list[ApiContract]:
    return [
        ApiContract(
            route="/internal/capability-planning/bootstrap",
            method="POST",
            purpose="生成岗位职责导向的 1-8 步实施蓝图",
            required_fields=["project_name", "scenarios", "timeline_days", "focus"],
        ),
        ApiContract(
            route="/internal/chat",
            method="POST",
            purpose="对话主流程，执行 Agent + 工具",
            required_fields=["message", "session_id", "user_id"],
        ),
        ApiContract(
            route="/internal/chat/replay/{trace_id}",
            method="GET",
            purpose="回放审计日志和对话记录",
            required_fields=["trace_id"],
        ),
    ]


def _ops_checklist() -> list[OpsChecklist]:
    return [
        OpsChecklist(
            item="接口稳定性",
            metric="5xx_rate",
            alert_rule="5 分钟窗口内 > 1% 告警",
        ),
        OpsChecklist(
            item="响应性能",
            metric="p95_latency_ms",
            alert_rule="连续 10 分钟 p95 > 1500ms 告警",
        ),
        OpsChecklist(
            item="工具成功率",
            metric="tool_success_rate",
            alert_rule="15 分钟窗口内 < 98% 告警",
        ),
        OpsChecklist(
            item="检索质量",
            metric="evidence_hit_rate",
            alert_rule="日均命中率 < 85% 告警",
        ),
    ]


def _step_status() -> list[StepSummary]:
    return [
        StepSummary(step_no=1, title="目标与范围定义", status="done", output="目标声明与范围边界"),
        StepSummary(step_no=2, title="用户故事与验收标准", status="done", output="故事清单与验收条目"),
        StepSummary(step_no=3, title="最小可用架构设计", status="done", output="模块职责与接口草图"),
        StepSummary(step_no=4, title="数据智能流程设计", status="done", output="抽取分析反馈闭环"),
        StepSummary(step_no=5, title="Agent 与搜索策略", status="done", output="多层召回与重排策略"),
        StepSummary(step_no=6, title="核心 API 设计", status="done", output="关键接口契约"),
        StepSummary(step_no=7, title="生产排障与可观测性", status="done", output="运维检查项与告警规则"),
        StepSummary(step_no=8, title="技术演进路径", status="pending", output="等待你补充业务优先级"),
    ]


def _build_response(payload: CapabilityPlanRequest) -> CapabilityPlanResponse:
    return CapabilityPlanResponse(
        project_name=payload.project_name,
        step_status=_step_status(),
        goal_statement=_goal_statement(payload),
        in_scope=[x.name for x in sorted(payload.scenarios, key=lambda x: x.priority)],
        out_of_scope=["复杂权限体系", "多集群全自动发布", "完整数据标注平台"],
        user_stories=_user_stories(payload),
        architecture_blocks=_architecture_blocks(),
        data_intelligence_flow=_data_flow(),
        agent_search_strategy=_search_strategy(),
        api_contracts=_api_contracts(),
        production_ops_checklist=_ops_checklist(),
        next_inputs_needed=[
            "每个优先场景的真实输入样例",
            "需要演示的失败注入点",
            "上线环境约束和合规要求",
            "性能目标和压测阈值",
        ],
    )


@router.get(
    "/bootstrap/template",
    response_model=CapabilityPlanResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def capability_bootstrap_template() -> CapabilityPlanResponse:
    payload = CapabilityPlanRequest(
        project_name="Data Intelligence Platform Capability Plan",
        audience="interviewer",
        focus="balanced",
        timeline_days=5,
        scenarios=[
            {"name": "数据智能抽取", "summary": "从复杂文本抽取结构化数据并落库", "priority": 1},
            {"name": "智能检索分析", "summary": "结合关键词和向量检索进行证据化分析", "priority": 2},
            {"name": "标注反馈回流", "summary": "把人工反馈回流到策略优化流程", "priority": 3},
            {"name": "AI 写作应用", "summary": "基于证据生成可编辑内容草稿", "priority": 4},
        ],
        constraints=["优先可演示", "优先可观测性"],
    )
    return _build_response(payload)


@router.post(
    "/bootstrap",
    response_model=CapabilityPlanResponse,
    dependencies=[Depends(verify_internal_token)],
)
async def capability_bootstrap(
    payload: CapabilityPlanRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CapabilityPlanResponse:
    response = _build_response(payload)

    db.add(
        AuditLog(
            event_type="capability.plan.generated",
            user_id="capability-planner",
            session_id=payload.project_name,
            route=request.url.path,
            client_ip=request.client.host if request.client else "",
            details_json={
                "trace_id": get_trace_id(),
                "audience": payload.audience,
                "focus": payload.focus,
                "timeline_days": payload.timeline_days,
                "scenario_count": len(payload.scenarios),
            },
        )
    )
    await db.commit()
    return response
