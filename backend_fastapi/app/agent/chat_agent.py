from collections.abc import AsyncGenerator
import asyncio
import json

from ..core.logging_setup import get_logger
from .state_machine import AgentContext, AgentState
from .tools.knowledge_tool import run_knowledge_lookup

logger = get_logger("agent")


TOPIC_GUIDANCE = {
    "高血压": "高血压方面：建议先连续记录 7 天家庭血压（晨起与睡前），若多次>=140/90，请尽快线下就诊评估用药与生活方式干预。",
    "糖尿病": "糖尿病方面：建议补充最近空腹血糖、餐后 2 小时血糖和 HbA1c 数据；若出现口渴、多尿、体重下降，应尽快就医。",
    "挂号": "挂号方面：建议优先确认科室（全科/内分泌/心内科）与就诊紧急程度，再准备既往检查与用药清单。",
    "报告": "报告解读方面：请提供关键指标、参考区间、检测时间与当前症状，便于给出分层解释和下一步建议。",
}


def compose_answer(message: str, matched_topics: list[str]) -> str:
    lines = ["已基于工具结果完成初步回答。"]

    if matched_topics:
        lines.append(f"识别主题：{', '.join(matched_topics)}。")
        for topic in matched_topics:
            guidance = TOPIC_GUIDANCE.get(topic)
            if guidance:
                lines.append(guidance)
    else:
        lines.append("识别主题：通用咨询。")
        lines.append("当前问题未命中专病关键词，请补充年龄、主要症状、持续时间、既往史和当前用药，以便给出更精准建议。")

    lines.append(f"你刚才的问题：{message.strip()}")
    lines.append("说明：以上为辅助信息，不替代医生诊断；若有急性胸痛、呼吸困难、意识改变等症状，请立即急诊。")
    return "\n".join(lines)


async def execute_agent(message: str, force_fail: bool = False) -> tuple[str, list[dict]]:
    ctx = AgentContext(message=message, force_fail=force_fail)
    ctx.transition(AgentState.PLANNING, "Request accepted")

    ctx.transition(AgentState.TOOL_RUNNING, "Run knowledge tool")

    if force_fail or "FAIL_TOOL" in message:
        ctx.transition(AgentState.TOOL_FAILED, "Forced failure branch enabled")
        answer = "本次执行失败：工具阶段被强制失败。请重试或关闭 force_fail。"
        logger.warning("agent.tool_failed", extra={"extra_fields": {"reason": "forced_failure"}})
        return answer, ctx.evidence

    tool_result = run_knowledge_lookup(message)
    ctx.evidence.append({"type": "tool", "name": "knowledge_lookup", "output": tool_result})

    ctx.transition(AgentState.ANSWERING, "Compose answer from tool evidence")
    topics = tool_result.get("matched_topics") or ["通用咨询"]
    answer = compose_answer(message, topics if topics != ["通用咨询"] else [])

    ctx.transition(AgentState.COMPLETED, "Execution finished")
    logger.info("agent.completed", extra={"extra_fields": {"states": [e for e in ctx.evidence if e.get('type') == 'state']}})
    return answer, ctx.evidence


async def stream_agent_answer(answer: str) -> AsyncGenerator[str, None]:
    for segment in answer.split("。"):
        text = segment.strip()
        if not text:
            continue
        yield f"data: {json.dumps({'chunk': text + '。'}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.08)
    yield "event: done\ndata: [DONE]\n\n"
