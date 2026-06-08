"""
EmailAgent —— 让 LLM 真的能调用邮箱工具。

设计要点：
- 使用 OpenAI / Azure 的原生 `tools` + `tool_choice="auto"` function-calling 协议
- 单次会话用一个循环：模型说要调哪个工具 → 我们执行 → 把结果塞回 messages → 再请模型
- 循环上限 5 次，避免模型反复调工具死循环
- 写动作（email.send）默认要求 LLM 先做"中文摘要 + 等用户确认"：
    通过 system prompt 强约束；如果 confirm=False，直接拒绝执行 email.send
- 复用 app.tools 里已注册的 email.* 工具实现，逻辑只在 agent 层做"调度"
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..logging_setup import get_logger
from ..metrics import agent_iterations, agent_tool_calls_total, agent_tool_duration_seconds
from ..providers import provider
from ..tools import registry

logger = get_logger("agent.email")


# ---------- OpenAI tools schema -----------------------------------------------

EMAIL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "email_list_inbox",
            "description": "列出最近若干封邮件的发件人/主题/日期。用户问『看下我最新的邮件 / 收件箱有什么』时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "邮箱账号名（来自 EMAIL_ACCOUNTS）。省略=默认账号；用户明确点名时再传。"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_search",
            "description": "按关键字搜索邮件（主题 / 发件人）。中文关键字会本地过滤最近 200 封。",
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "关键字，如 发票 / github / boss@xxx.com"},
                    "limit": {"type": "integer", "default": 10},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "邮箱账号名，省略=默认账号"},
                },
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_read",
            "description": "根据 UID 读取一封邮件的完整正文。先用 list_inbox / search 拿到 UID 再调用。**account 必须与拿到 UID 时同一账号**。",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与上一次 list_inbox/search 同一账号"},
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_send",
            "description": (
                "发送邮件。⚠ 这是一个不可撤销的写操作，调用前必须已经获得用户的明确确认。"
                "如果用户只是提出『起草 / 帮我写』，请先输出草稿，并询问『要现在发送吗？』。"
                "只有用户回复『发 / 确认 / 发送』后才调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "收件人邮箱地址列表",
                    },
                    "subject": {"type": "string", "minLength": 1},
                    "body": {"type": "string", "description": "纯文本正文"},
                    "cc": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "抄送地址（可选）",
                    },
                    "html": {"type": "string", "description": "HTML 正文，可选；与 body 二选一即可"},
                    "account": {"type": "string", "description": "用哪个账号发；省略=默认账号"},
                },
                "required": ["to", "subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_mark_seen",
            "description": "标记一封邮件为已读 / 未读。常用于『把刚才那封标已读』。",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "seen": {"type": "boolean", "default": True},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_mark_flag",
            "description": "给一封邮件加 / 取消星标（IMAP \\Flagged）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "flagged": {"type": "boolean", "default": True},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_move",
            "description": "把一封邮件从某文件夹移到另一文件夹（归档、整理）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "dest_mailbox": {"type": "string", "description": "目标文件夹名，如 Archive / 工作"},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid", "dest_mailbox"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_delete",
            "description": (
                "删除一封邮件。⚠ 不可撤销的写操作，调用前必须获得用户的明确确认。"
                "默认 hard=false（移到回收站，可恢复）；hard=true 是 EXPUNGE，彻底无法恢复。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "hard": {"type": "boolean", "default": False},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_list_attachments",
            "description": "列出某封邮件的所有附件元数据（文件名 / MIME / 大小 / part_id）。只读。",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_reply",
            "description": (
                "对某封邮件回复。会自动套 In-Reply-To/References、Re: 主题前缀、原文引文。"
                "⚠ 写操作，等同 email_send，调用前必须用户确认。"
                "reply_all=true 时把原邮件 To+Cc 全部加上（去重 + 排除自己）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "要回复的原邮件 UID"},
                    "body": {"type": "string", "description": "正文（引文会被自动追加在末尾）"},
                    "reply_all": {"type": "boolean", "default": False},
                    "extra_cc": {"type": "array", "items": {"type": "string"}},
                    "include_quote": {"type": "boolean", "default": True},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_forward",
            "description": (
                "转发某封邮件给新收件人。主题自动加 Fwd: 前缀，原文以引文形式插入。"
                "⚠ 写操作，等同 email_send，调用前必须用户确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "to": {"type": "array", "items": {"type": "string"}},
                    "cc": {"type": "array", "items": {"type": "string"}},
                    "body_prefix": {"type": "string", "description": "你想加在转发引文前的说明文字（可空）"},
                    "include_quote": {"type": "boolean", "default": True},
                    "mailbox": {"type": "string", "default": "INBOX"},
                    "account": {"type": "string", "description": "必须与拿到 UID 时同一账号"},
                },
                "required": ["uid", "to"],
            },
        },
    },
    # ---------------- Calendar 协同工具（与 Email 同一 Agent）----------------
    {
        "type": "function",
        "function": {
            "name": "calendar_list_events",
            "description": "列出用户在某个时间窗内的日程事件。只读，可任意调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "事件所属用户；**你不要填**，会话默认值已注入进空环境"},
                    "time_min": {"type": "string", "description": "ISO8601，仅返回结束晚于此时刻的事件"},
                    "time_max": {"type": "string", "description": "ISO8601，仅返回开始早于此时刻的事件"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_find_conflict",
            "description": "检查 [start, end) 时间窗内是否已有日程冲突。返回 has_conflict 与冲突事件列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "**你不要填**，会话默认值已注入进空环境"},
                    "start": {"type": "string", "description": "ISO8601"},
                    "end": {"type": "string", "description": "ISO8601"},
                },
                "required": ["start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_create_event",
            "description": (
                "在用户日程上创建新事件。⚠ 写操作，调用前需确认（受 allow_send 守门）。"
                "建议先调 calendar_find_conflict 检查冲突；end 与 duration_minutes 二选一。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "**你不要填**，会话默认值已注入进空环境"},
                    "title": {"type": "string"},
                    "start": {"type": "string", "description": "ISO8601 起始时间"},
                    "end": {"type": "string", "description": "ISO8601 结束时间；与 duration_minutes 二选一"},
                    "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "attendees": {"type": "array", "items": {"type": "string"}},
                    "source": {"type": "string", "description": "事件来源标识，如 agent.email"},
                    "online_meeting": {
                        "type": "boolean",
                        "description": "true 时自动生成 Teams 会议链接（仅 msgraph 后端 + 工作/学校账号有效；sqlite/memory 后端会忽略）",
                    },
                },
                "required": ["title", "start"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_propose_slot",
            "description": "在 [earliest, latest] 区间内寻找第一个能容纳指定时长且无冲突的空档。",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "**你不要填**"},
                    "duration_minutes": {"type": "integer", "minimum": 5, "maximum": 1440},
                    "earliest": {"type": "string", "description": "ISO8601"},
                    "latest": {"type": "string", "description": "ISO8601"},
                    "granularity_minutes": {"type": "integer", "default": 30},
                },
                "required": ["duration_minutes", "earliest", "latest"],
            },
        },
    },
]


# tool function 名 → 内部工具注册名（registry 里用的是 email.xxx / calendar.xxx）
_TOOL_NAME_MAP = {
    "email_list_inbox": "email.list_inbox",
    "email_search": "email.search",
    "email_read": "email.read",
    "email_send": "email.send",
    "email_mark_seen": "email.mark_seen",
    "email_mark_flag": "email.mark_flag",
    "email_move": "email.move",
    "email_delete": "email.delete",
    "email_list_attachments": "email.list_attachments",
    "email_reply": "email.reply",
    "email_forward": "email.forward",
    "calendar_list_events": "calendar.list_events",
    "calendar_find_conflict": "calendar.find_conflict",
    "calendar_create_event": "calendar.create_event",
    "calendar_propose_slot": "calendar.propose_slot",
}


AGENT_SYSTEM_PROMPT = """\
你是 Aido，一个能真正动手帮我处理邮件、日程，并在两者之间协同的私人助手。你能调用的工具按类别如下：

【邮件】
只读：email_list_inbox / email_search / email_read / email_list_attachments
可逆写：email_mark_seen / email_mark_flag / email_move
不可撤销写（需用户确认）：email_send / email_reply / email_forward / email_delete

【日程 Calendar】
只读：calendar_list_events / calendar_find_conflict / calendar_propose_slot
不可撤销写（需用户确认）：calendar_create_event

重要行为准则（优先级从高到低）：

0. 【UID 是唯一身份证】每次给用户列出邮件时，**必须在每一项里显式写出 UID**。
   推荐格式：
   ```
   1. [UID 4350] 主题：xxx
      发件人：xxx
      日期：xxx
   ```
   当用户说「第3封 / 打开它」时，**必须按列表里显示的 UID 去 email_read**，
   绝对不要凭记忆或重新数搜索结果来推断 UID。

1. 【看上下文不重复调】如果用户要的信息在历史 tool result 里已经有了（UID、正文、列表、event_id），直接用，不要再调一次工具。

2. 【全文优先】用户说「详情 / 全文 / 完整内容」时，把对应 UID 的 email_read 结果的 body 字段完整贴出来（保留链接、代码块、换行），不要再做"内容摘要"。如果上次没读过这个 UID，先调 email_read 拿到 body 再贴。

3. 【默认范围】用户没说数量时默认 limit=10；说 N 封就 N；说「全部 / 所有」就 limit=50。

4. 【不要道歉乱重查】发现内容跟预期不一致时，先怀疑是否选错了 UID，而不是怀疑搜索结果。直接问用户「你说的第3封是不是 UID xxx？」，让用户给你正确的 UID，不要推翻重搜。

5. 【写操作守门】要发邮件、删邮件、或创建日程时，先输出摘要 + 询问「确认吗？」；用户回复「发 / 删 / 确认 / OK」之后再调。若工具返回 `blocked: allow_send=False`，礼貌告知用户开启「允许真发送」后重试。

6. 【汇报形式】调用完工具后用中文简洁汇报，不要堆 JSON。但邮件正文 / 链接 / 时间要原样保留。

7. 【多步操作】如「把昨天 GitHub 的邮件都标已读」：先 search/list 拿 UID，再依次调 email_mark_seen。

8. 【地址校验】收件人地址不完整或可疑时主动询问，不要瞎补。

9. 【邮件 permalink】每次列出邮件时，**在每一项的 UID 后面附上这封邮件的本地永久链接**：
   `http://localhost:8100/v1/email/messages/<UID>/view?mailbox=<MAILBOX>`

10. 【跨工具协同】当用户提到「把邮件里说的会议加到日程」「先看冲突再约」「订完会议回封确认」等跨工具诉求时，按如下推荐顺序执行：
    a. email_search / email_read 拿到时间、主题、参与人
    b. calendar_find_conflict 检查目标时间窗有无冲突；若有，calendar_propose_slot 提议替代时段
    c. 把准备创建的事件摘要给用户 → 等确认 → calendar_create_event（source 标为 "agent.email"，attendees 带上原邮件发件人）
    d. 视用户意图调 email_reply 写一段中文确认（不要在用户未要求时主动回信）
    把这些步骤的 event_id / UID 在最终汇报里都列出来，便于用户事后回溯。

11. 【多邮箱账号】用户可能配置了多个邮箱（如 QQ + Outlook）。规则：
    - **省略 `account` 参数 = 用默认账号**（运行时上下文里会告诉你是哪一个，通常是 QQ）。
    - 用户随口说「看邮件 / 收件箱有啥」→ 不传 `account`，走默认账号即可。
    - 用户明确点名「看 Outlook / 工作邮箱 / outlook 收件箱」→ 传 `account="outlook"`。
    - 同名点「QQ」→ 传 `account="qq"`。
    - **跨账号操作时 UID 不能复用**：在 QQ 里 list 出来的 UID 只能去 QQ 里 read/reply/delete。
      所以拿到 UID 之后，所有后续工具调用必须复用同一个 `account` 值（要么都不传、要么都传同一个）。
    - 用户没指定但你不确定走哪个账号时，**直接走默认账号**，不要反复追问。
"""


def _build_system_prompt(*, user_id: str | None) -> str:
    """拼上动态 runtime 上下文：
    - 当前日期（LLM 默认用训练截止日期体会把“明天”推到 2024）
    - 当前 session 的 user_id（避免 LLM 自己编 user_id导致 create/list 不在同一人名下）
    """
    # 本地时区；UTC 同时给一个双保险
    from datetime import datetime, timezone
    now_local = datetime.now().astimezone()
    now_utc = datetime.now(timezone.utc)

    # 已配置的邮箱账号列表
    try:
        from ..integrations.email_factory import describe_accounts, get_default_account_name
        accounts = describe_accounts()
        default_name = get_default_account_name()
        if accounts:
            lines = []
            for a in accounts:
                marker = "（默认）" if a.get("default") else ""
                lines.append(f"    · {a['name']}{marker} — backend={a['backend']}, address={a.get('address', '')}")
            accounts_block = (
                f"- 已配置邮箱账号（{len(accounts)} 个，默认 = `{default_name}`）：\n"
                + "\n".join(lines) + "\n"
                + "  调用任意 email_* 工具时，省略 `account` = 用默认账号；要操作其他账号请显式传 `account=\"<name>\"`。\n"
            )
        else:
            accounts_block = "- 当前未配置任何邮箱账号。\n"
    except Exception:
        accounts_block = ""

    runtime_block = (
        "\n---\n"
        "【运行时上下文】\n"
        f"- 当前本地时间: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"- 当前 UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"- 今天: {now_local.strftime('%Y-%m-%d (%A)')}\n"
        "- 计算“明天/后天/下周一”等相对日期时，**一律以上面这个“今天”为基准**，"
        "不要用你训练数据里的旧日期。\n"
        f"{accounts_block}"
    )
    if user_id:
        runtime_block += (
            f"- 当前会话 user_id: `{user_id}`。工具调用时请不要传 user_id 参数，"
            f"后端已自动填入 `{user_id}`。你主动填反而会造成跨工具不一致。\n"
        )
    else:
        runtime_block += (
            "- 当前会话未提供 user_id，如需你可以填，但**同一轮对话里多个工具调用必须用同一个 user_id**。\n"
        )
    return AGENT_SYSTEM_PROMPT + runtime_block


# ---------- 数据结构 -----------------------------------------------------------

@dataclass
class ToolCallLog:
    name: str
    args: dict
    result: dict | None = None
    error: str | None = None


@dataclass
class AgentResult:
    text: str
    actions: list[ToolCallLog] = field(default_factory=list)
    iterations: int = 0


# ---------- Agent 主循环 -------------------------------------------------------

async def run_email_agent(
    user_message: str,
    *,
    history: list[dict] | None = None,
    allow_send: bool = True,
    max_iterations: int = 5,
    max_tokens: int = 1024,
    user_id: str | None = None,
) -> AgentResult:
    """运行一轮 EmailAgent。

    Args:
        user_message: 用户本轮发来的内容
        history: 历史消息（OpenAI 格式 [{role, content}]），可选
        allow_send: False 时强制阻止 email_send（即使模型试图调用）
        max_iterations: 工具调用上限，超过就把当前已收集的内容返回
        user_id: 会话用户 ID，会注入到工具 ctx 中让 calendar/email 工具拿到一致身份
    """
    if not hasattr(provider, "chat_with_tools"):
        raise RuntimeError(
            f"Current provider '{getattr(provider, 'name', '?')}' "
            "does not support tool calling. Switch to azure/openai."
        )

    messages: list[dict] = [{"role": "system", "content": _build_system_prompt(user_id=user_id)}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    tool_ctx: dict[str, Any] = {"user_id": user_id} if user_id else {}

    actions: list[ToolCallLog] = []

    for it in range(1, max_iterations + 1):
        resp = await provider.chat_with_tools(  # type: ignore[attr-defined]
            messages=messages,
            tools=EMAIL_TOOLS,
            max_tokens=max_tokens,
        )

        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, "tool_calls", None) or []

        # 没有工具调用 → 终态：模型直接给出文字回复
        if not tool_calls:
            final_text = (msg.content or "").strip() or "（无内容）"
            agent_iterations.observe(it)
            logger.info("agent_done_no_tools", extra={"iter": it, "len": len(final_text)})
            return AgentResult(text=final_text, actions=actions, iterations=it)

        # 把 assistant 消息（带 tool_calls）原样塞回 messages，让 OpenAI 协议成立
        messages.append(
            {
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

        # 依次执行工具
        for tc in tool_calls:
            fn_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            internal_name = _TOOL_NAME_MAP.get(fn_name)
            log = ToolCallLog(name=fn_name, args=args)

            # 写操作守门：email_send / email_reply / email_forward / email_delete / calendar_create_event 都不可撤销
            if fn_name in {"email_send", "email_reply", "email_forward", "email_delete", "calendar_create_event"} and not allow_send:
                log.error = f"{fn_name} blocked: allow_send=False"
                actions.append(log)
                agent_tool_calls_total.labels(fn_name, "blocked").inc()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": json.dumps({"error": "write op blocked", "hint": "ask user to confirm"}, ensure_ascii=False),
                    }
                )
                continue

            if not internal_name or not (tool_fn := registry.get(internal_name)):
                log.error = f"unknown tool: {fn_name}"
                actions.append(log)
                agent_tool_calls_total.labels(fn_name, "error").inc()
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": json.dumps({"error": log.error}, ensure_ascii=False),
                    }
                )
                continue

            t0 = time.perf_counter()
            try:
                result = await tool_fn(args, tool_ctx)
                log.result = result
                agent_tool_calls_total.labels(fn_name, "ok").inc()
                logger.info("agent_tool_ok", extra={"tool": fn_name, "iter": it})
            except Exception as exc:
                log.error = str(exc)
                result = {"error": str(exc)}
                agent_tool_calls_total.labels(fn_name, "error").inc()
                logger.warning("agent_tool_failed", extra={"tool": fn_name, "err": str(exc)})
            finally:
                agent_tool_duration_seconds.labels(fn_name).observe(time.perf_counter() - t0)

            actions.append(log)
            # 工具结果回灌给模型；正文不能太长，否则下一次 LLM 调用要处理一大段文本，会明显变慢。
            # email_read 的 body 字段需要稍大预算（用户可能问"全文"）；其他工具 4KB 就够。
            limit = 12000 if fn_name == "email_read" else 4000
            payload = json.dumps(result, ensure_ascii=False, default=str)
            if len(payload) > limit:
                payload = payload[:limit] + " …(truncated)"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": fn_name,
                    "content": payload,
                }
            )

    # 触发最大轮次：再请一次模型让它收尾，但不再给 tools
    logger.warning("agent_max_iter_reached", extra={"max_iter": max_iterations})
    resp = await provider.chat_with_tools(  # type: ignore[attr-defined]
        messages=messages + [{"role": "system", "content": "已达到工具调用上限，直接用中文总结当前进展给用户。"}],
        tools=[],
        max_tokens=max_tokens,
    )
    final_text = (resp.choices[0].message.content or "已完成当前可执行的步骤。").strip()
    agent_iterations.observe(max_iterations)
    return AgentResult(text=final_text, actions=actions, iterations=max_iterations)


# ---------- 流式 Agent ---------------------------------------------------------
#
# 事件协议（每个 yield 是一个 dict，路由层会序列化成 SSE）：
#   {"type":"meta",       "trace_id":"..."}
#   {"type":"tool_start", "name":"email_search", "args":{...}, "iter":1}
#   {"type":"tool_end",   "name":"email_search", "result":{...}, "ok":true, "iter":1}
#   {"type":"tool_blocked","name":"email_send", "reason":"allow_send=False"}
#   {"type":"token",      "text":"模型回复的下一段文字"}
#   {"type":"done",       "text":"最终完整文本", "actions":[...], "iterations":N}
#   {"type":"error",      "message":"..."}

async def run_email_agent_stream(
    user_message: str,
    *,
    history: list[dict] | None = None,
    allow_send: bool = True,
    max_iterations: int = 5,
    max_tokens: int = 1024,
    user_id: str | None = None,
) -> AsyncIterator[dict]:
    """流式版本：边推理边把进度事件 yield 出去。"""
    if not hasattr(provider, "chat_with_tools_stream"):
        yield {
            "type": "error",
            "message": f"Provider '{getattr(provider, 'name', '?')}' 不支持流式 tool calling，请用 /v1/chat/agent。",
        }
        return

    messages: list[dict] = [{"role": "system", "content": _build_system_prompt(user_id=user_id)}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    tool_ctx: dict[str, Any] = {"user_id": user_id} if user_id else {}

    actions: list[ToolCallLog] = []
    final_text_parts: list[str] = []

    for it in range(1, max_iterations + 1):
        # —— 本轮收集 ——
        content_buf: list[str] = []
        # tool_calls 按 index 累积：{index: {"id":..., "name":..., "arguments_str":...}}
        pending_calls: dict[int, dict] = {}
        finish_reason: str | None = None

        try:
            async for chunk in provider.chat_with_tools_stream(  # type: ignore[attr-defined]
                messages=messages,
                tools=EMAIL_TOOLS,
                max_tokens=max_tokens,
            ):
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason

                # 文本 token 流出去
                if getattr(delta, "content", None):
                    content_buf.append(delta.content)
                    yield {"type": "token", "text": delta.content}

                # tool_calls 增量
                tc_delta = getattr(delta, "tool_calls", None)
                if tc_delta:
                    for tc in tc_delta:
                        idx = tc.index
                        slot = pending_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments_str": ""}
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        fn = getattr(tc, "function", None)
                        if fn:
                            if getattr(fn, "name", None):
                                slot["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                slot["arguments_str"] += fn.arguments
        except Exception as exc:
            logger.exception("agent_stream_provider_failed")
            yield {"type": "error", "message": f"provider 调用失败：{exc}"}
            return

        # —— 本轮没有工具调用 → 终态 ——
        if not pending_calls:
            final_text = "".join(content_buf).strip() or "（无内容）"
            final_text_parts.append(final_text)
            agent_iterations.observe(it)
            yield {
                "type": "done",
                "text": "\n".join(final_text_parts).strip(),
                "actions": [_action_to_dict(a) for a in actions],
                "iterations": it,
            }
            return

        # —— 本轮有工具调用：先把流出来的 content + tool_calls 拼成 assistant message ——
        assistant_content = "".join(content_buf)
        if assistant_content.strip():
            final_text_parts.append(assistant_content.strip())
        messages.append(
            {
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": [
                    {
                        "id": pending_calls[i]["id"],
                        "type": "function",
                        "function": {
                            "name": pending_calls[i]["name"],
                            "arguments": pending_calls[i]["arguments_str"] or "{}",
                        },
                    }
                    for i in sorted(pending_calls)
                ],
            }
        )

        # —— 依次执行 ——
        for idx in sorted(pending_calls):
            call = pending_calls[idx]
            fn_name = call["name"]
            try:
                args = json.loads(call["arguments_str"] or "{}")
            except json.JSONDecodeError:
                args = {}

            log = ToolCallLog(name=fn_name, args=args)

            # 写操作守门
            if fn_name in {"email_send", "email_reply", "email_forward", "email_delete", "calendar_create_event"} and not allow_send:
                log.error = f"{fn_name} blocked: allow_send=False"
                actions.append(log)
                agent_tool_calls_total.labels(fn_name, "blocked").inc()
                yield {
                    "type": "tool_blocked",
                    "name": fn_name,
                    "reason": "allow_send=False",
                    "iter": it,
                }
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": fn_name,
                        "content": json.dumps(
                            {"error": "write op blocked", "hint": "ask user to confirm"},
                            ensure_ascii=False,
                        ),
                    }
                )
                continue

            internal_name = _TOOL_NAME_MAP.get(fn_name)
            if not internal_name or not (tool_fn := registry.get(internal_name)):
                log.error = f"unknown tool: {fn_name}"
                actions.append(log)
                agent_tool_calls_total.labels(fn_name, "error").inc()
                yield {"type": "tool_end", "name": fn_name, "ok": False, "error": log.error, "iter": it}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": fn_name,
                        "content": json.dumps({"error": log.error}, ensure_ascii=False),
                    }
                )
                continue

            # 推工具开始事件（让前端能显示"📧 正在搜索…"）
            yield {"type": "tool_start", "name": fn_name, "args": args, "iter": it}

            t0 = time.perf_counter()
            try:
                result = await tool_fn(args, tool_ctx)
                log.result = result
                agent_tool_calls_total.labels(fn_name, "ok").inc()
                yield {"type": "tool_end", "name": fn_name, "ok": True, "result": _trim_for_event(result), "iter": it}
                logger.info("agent_stream_tool_ok", extra={"tool": fn_name, "iter": it})
            except Exception as exc:
                log.error = str(exc)
                result = {"error": str(exc)}
                agent_tool_calls_total.labels(fn_name, "error").inc()
                yield {"type": "tool_end", "name": fn_name, "ok": False, "error": str(exc), "iter": it}
                logger.warning("agent_stream_tool_failed", extra={"tool": fn_name, "err": str(exc)})
            finally:
                agent_tool_duration_seconds.labels(fn_name).observe(time.perf_counter() - t0)

            actions.append(log)

            # 回灌结果给模型
            limit = 12000 if fn_name == "email_read" else 4000
            payload = json.dumps(result, ensure_ascii=False, default=str)
            if len(payload) > limit:
                payload = payload[:limit] + " …(truncated)"
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": fn_name,
                    "content": payload,
                }
            )

    # 触发最大轮次
    logger.warning("agent_stream_max_iter_reached", extra={"max_iter": max_iterations})
    agent_iterations.observe(max_iterations)
    yield {
        "type": "done",
        "text": ("\n".join(final_text_parts).strip() or "已完成当前可执行的步骤。") + "\n\n（已达工具调用上限）",
        "actions": [_action_to_dict(a) for a in actions],
        "iterations": max_iterations,
    }


def _action_to_dict(a: ToolCallLog) -> dict:
    return {"name": a.name, "args": a.args, "result": a.result, "error": a.error}


def _trim_for_event(obj: Any, max_len: int = 800) -> Any:
    """工具结果 SSE 出去时做一次裁剪，避免一次性把 12KB 的 email body 推给前端事件流。"""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return {"_trim_error": "non-serializable"}
    if len(s) <= max_len:
        return obj
    return {"_truncated": True, "preview": s[:max_len] + " …"}
