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
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from ..logging_setup import get_logger
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
                },
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_read",
            "description": "根据 UID 读取一封邮件的完整正文。先用 list_inbox / search 拿到 UID 再调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string"},
                    "mailbox": {"type": "string", "default": "INBOX"},
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
                },
                "required": ["uid", "to"],
            },
        },
    },
]


# tool function 名 → 内部工具注册名（registry 里用的是 email.xxx）
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
}


AGENT_SYSTEM_PROMPT = """\
你是 Aido，一个能真正动手帮我处理邮件的私人助手。你能调用工具：

只读（随时可用）：
- email_list_inbox / email_search / email_read / email_list_attachments

写操作（必须先得到用户口头确认）：
- email_send：发送新邮件
- email_reply：回复某封邮件（自动套 In-Reply-To / Re: / 引文）
- email_forward：转发某封邮件给新收件人
- email_delete：删除邮件（默认软删，hard=true 才不可恢复）

可逆写操作（一般无需特别确认，但应在回复里说明做了什么）：
- email_mark_seen / email_mark_flag / email_move

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

1. 【看上下文不重复调】如果用户要的信息在历史 tool result 里已经有了（UID、正文、列表），直接用，不要再调一次工具。

2. 【全文优先】用户说「详情 / 全文 / 完整内容」时，把对应 UID 的 email_read 结果的 body 字段完整贴出来（保留链接、代码块、换行），不要再做"内容摘要"。如果上次没读过这个 UID，先调 email_read 拿到 body 再贴。

3. 【默认范围】用户没说数量时默认 limit=10；说 N 封就 N；说「全部 / 所有」就 limit=50。

4. 【不要道歉乱重查】发现内容跟预期不一致时，先怀疑是否选错了 UID，而不是怀疑搜索结果。直接问用户「你说的第3封是不是 UID xxx？」，让用户给你正确的 UID，不要推翻重搜。

5. 【写操作守门】要发邮件或删邮件时，先输出摘要 + 询问「确认吗？」；用户回复「发 / 删 / 确认 / OK」之后再调。

6. 【汇报形式】调用完工具后用中文简洁汇报，不要堆 JSON。但邮件正文 / 链接要原样保留。

7. 【多步操作】如「把昨天 GitHub 的邮件都标已读」：先 search/list 拿 UID，再依次调 email_mark_seen。

8. 【地址校验】收件人地址不完整或可疑时主动询问，不要瞎补。

9. 【邮件 permalink】每次列出邮件时，**在每一项的 UID 后面附上这封邮件的本地永久链接**，格式：
   `http://localhost:8100/v1/email/messages/<UID>/view?mailbox=<MAILBOX>`
   推荐写成：
   ```
   1. [UID 4350] 主题：xxx  → http://localhost:8100/v1/email/messages/4350/view?mailbox=INBOX
      发件人：xxx
      日期：xxx
   ```
   mailbox 不是 INBOX 时（例如 "Sent Messages"）记得 URL encode。
   用户点这个链接会直接在新标签打开整封邮件（含正文里的所有超链接）。
"""


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
) -> AgentResult:
    """运行一轮 EmailAgent。

    Args:
        user_message: 用户本轮发来的内容
        history: 历史消息（OpenAI 格式 [{role, content}]），可选
        allow_send: False 时强制阻止 email_send（即使模型试图调用）
        max_iterations: 工具调用上限，超过就把当前已收集的内容返回
    """
    if not hasattr(provider, "chat_with_tools"):
        raise RuntimeError(
            f"Current provider '{getattr(provider, 'name', '?')}' "
            "does not support tool calling. Switch to azure/openai."
        )

    messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

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

            # 写操作守门：email_send / email_reply / email_forward / email_delete 都不可撤销
            if fn_name in {"email_send", "email_reply", "email_forward", "email_delete"} and not allow_send:
                log.error = f"{fn_name} blocked: allow_send=False"
                actions.append(log)
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
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": fn_name,
                        "content": json.dumps({"error": log.error}, ensure_ascii=False),
                    }
                )
                continue

            try:
                result = await tool_fn(args, {})
                log.result = result
                logger.info("agent_tool_ok", extra={"tool": fn_name, "iter": it})
            except Exception as exc:
                log.error = str(exc)
                result = {"error": str(exc)}
                logger.warning("agent_tool_failed", extra={"tool": fn_name, "err": str(exc)})

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
) -> AsyncIterator[dict]:
    """流式版本：边推理边把进度事件 yield 出去。"""
    if not hasattr(provider, "chat_with_tools_stream"):
        yield {
            "type": "error",
            "message": f"Provider '{getattr(provider, 'name', '?')}' 不支持流式 tool calling，请用 /v1/chat/agent。",
        }
        return

    messages: list[dict] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

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
            if fn_name in {"email_send", "email_reply", "email_forward", "email_delete"} and not allow_send:
                log.error = f"{fn_name} blocked: allow_send=False"
                actions.append(log)
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

            try:
                result = await tool_fn(args, {})
                log.result = result
                yield {"type": "tool_end", "name": fn_name, "ok": True, "result": _trim_for_event(result), "iter": it}
                logger.info("agent_stream_tool_ok", extra={"tool": fn_name, "iter": it})
            except Exception as exc:
                log.error = str(exc)
                result = {"error": str(exc)}
                yield {"type": "tool_end", "name": fn_name, "ok": False, "error": str(exc), "iter": it}
                logger.warning("agent_stream_tool_failed", extra={"tool": fn_name, "err": str(exc)})

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
