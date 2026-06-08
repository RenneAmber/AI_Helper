"""
MS Graph 登录辅助端点。

前端在调用涉及日历的 Agent 工具时，后端可能首次触发 device code flow
（同步阻塞，等待用户在浏览器完成登录）。本端点让前端能轮询拿到
verification_uri + user_code，把它显示成卡片，避免用户去翻服务端日志。
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from ..integrations.ms_auth import auth

router = APIRouter(prefix="/v1/auth/msgraph", tags=["auth"])


@router.get("/pending")
async def pending_device_flow() -> dict:
    """返回当前是否有待用户完成的 device code 登录。

    返回结构：
      - 无待办：{"pending": null}
      - 有待办：{"pending": {"verification_uri": "...", "user_code": "...",
                              "message": "...", "expires_in": 870}}
    """
    flow = auth.pending_flow
    if not flow:
        return {"pending": None}
    remaining = max(0, int(flow.get("expires_at", 0) - time.time()))
    return {
        "pending": {
            "verification_uri": flow.get("verification_uri"),
            "user_code": flow.get("user_code"),
            "message": flow.get("message"),
            "expires_in": remaining,
        }
    }
