"""
Calendar 后端工厂 —— 按 settings.calendar_backend 选择后端实现。

支持值：
- "memory"  → InMemoryCalendarBackend（进程内，重启即清空）
- "sqlite"  → SqliteCalendarBackend（默认；落 proactive_ai.db）
- "msgraph" → MsGraphCalendarBackend（真连 Outlook / Teams 日历）

模块只导出一个名字 `backend`，业务代码统一通过它访问。
切换后端只需改 `CALENDAR_BACKEND` 环境变量 + 重启进程，**不动业务代码**。
"""

from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger("calendar_factory")


def _build():
    kind = (settings.calendar_backend or "sqlite").strip().lower()
    if kind == "memory":
        from .calendar_local import backend as b
        logger.info("calendar_backend_selected", extra={"kind": "memory"})
        return b
    if kind == "msgraph":
        from .calendar_msgraph import backend as b
        logger.info("calendar_backend_selected", extra={"kind": "msgraph"})
        return b
    # 默认 sqlite
    from .calendar_sqlite import backend as b
    logger.info("calendar_backend_selected", extra={"kind": "sqlite"})
    return b


# 模块级单例
backend = _build()
