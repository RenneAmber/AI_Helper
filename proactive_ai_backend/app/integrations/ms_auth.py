"""
Microsoft Graph 认证 —— Device Code Flow（MSAL PublicClientApplication）。

为什么用 Device Code Flow
-------------------------
1. 不需要在代码里放 client_secret（公共客户端）
2. 不需要本地 HTTP 回调服务，适合 CLI / 后端进程 / 容器
3. 首次登录在浏览器里走一次，之后用 refresh_token 自动续约
4. Token 持久化到本地 JSON 文件（MSAL 的 SerializableTokenCache）

注意
----
- 若使用学校 / 公司账号：tenant_id 填实际 GUID
- 若使用个人 Microsoft 账号（outlook.com 等）：tenant_id 填 "consumers"
- 混合：填 "common"
- Application 必须在 AAD Portal 启用 "Allow public client flows = Yes"
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path
from typing import Any

import msal

from ..config import settings

logger = logging.getLogger("ms_auth")

# Authority 模式：
#   https://login.microsoftonline.com/<tenant_id>
#     - 工作/学校账号：实际 tenant GUID
#     - 个人账号：consumers
#     - 混合：common
_AUTHORITY_BASE = "https://login.microsoftonline.com"


def _scopes() -> list[str]:
    raw = (settings.ms_graph_scopes or "").strip()
    if not raw:
        return ["Calendars.ReadWrite", "User.Read"]
    return [s for s in raw.split() if s]


class GraphAuth:
    """对 MSAL 的薄封装：第一次登录走 device code，之后内存 + 文件双缓存 token。

    线程安全：MSAL 的 PublicClientApplication 是线程安全的；我们额外加了进程锁以避免
    并发的 acquire_token_interactive_device_code 同时弹出两组 user_code。
    """

    def __init__(self) -> None:
        self._cache = msal.SerializableTokenCache()
        self._cache_path = Path(settings.ms_graph_token_cache_path)
        self._load_cache()
        self._lock = threading.Lock()
        self._app: msal.PublicClientApplication | None = None
        # 当前正在等待用户完成的 device flow（前端轮询用）。
        # None 表示无待办；dict 含 verification_uri / user_code / message / expires_at(epoch)
        self.pending_flow: dict | None = None

    # ---------- token cache 持久化 ----------

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._cache.deserialize(self._cache_path.read_text(encoding="utf-8"))
            except Exception as exc:  # 损坏的 cache 不应阻塞启动
                logger.warning("ms_auth_cache_load_failed", extra={"err": str(exc)})

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            try:
                self._cache_path.parent.mkdir(parents=True, exist_ok=True)
                self._cache_path.write_text(self._cache.serialize(), encoding="utf-8")
                # 收紧权限，避免别的进程读到 refresh_token（Windows 上不影响）
                try:
                    os.chmod(self._cache_path, 0o600)
                except OSError:
                    pass
            except Exception as exc:
                logger.warning("ms_auth_cache_save_failed", extra={"err": str(exc)})

    # ---------- MSAL app 单例 ----------

    def _ensure_app(self) -> msal.PublicClientApplication:
        if self._app is not None:
            return self._app
        if not settings.ms_graph_client_id:
            raise RuntimeError(
                "MS_GRAPH_CLIENT_ID not configured. "
                "Set env var or app/config.py field before using msgraph calendar backend."
            )
        authority = f"{_AUTHORITY_BASE}/{settings.ms_graph_tenant_id or 'common'}"
        self._app = msal.PublicClientApplication(
            client_id=settings.ms_graph_client_id,
            authority=authority,
            token_cache=self._cache,
        )
        return self._app

    # ---------- 主入口：拿一个 access_token（必要时弹 device code） ----------

    def _acquire_token_sync(self) -> str:
        app = self._ensure_app()
        scopes = _scopes()
        # 1) 先尝试从缓存里拿
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(scopes, account=accounts[0])
            if result and "access_token" in result:
                return result["access_token"]

        # 2) 缓存没命中 → 启动 device code flow
        with self._lock:
            # 重新检查（避免并发时双重弹窗）
            accounts = app.get_accounts()
            if accounts:
                result = app.acquire_token_silent(scopes, account=accounts[0])
                if result and "access_token" in result:
                    return result["access_token"]

            flow = app.initiate_device_flow(scopes=scopes)
            if "user_code" not in flow:
                raise RuntimeError(
                    f"Device code flow failed to start: {flow.get('error_description') or flow}"
                )
            # 醒目地打印到 stderr，确保用户能看到
            print("\n" + "=" * 70, flush=True)
            print("[MS Graph] First-time login required.", flush=True)
            print(flow["message"], flush=True)
            print("=" * 70 + "\n", flush=True)
            logger.info(
                "ms_auth_device_flow_started",
                extra={"verification_uri": flow.get("verification_uri"), "user_code": flow.get("user_code")},
            )

            # 暴露给前端轮询：用户没完成登录前，/v1/auth/msgraph/pending 会返回这段
            import time as _time
            self.pending_flow = {
                "verification_uri": flow.get("verification_uri") or flow.get("verification_uri_complete"),
                "user_code": flow.get("user_code"),
                "message": flow.get("message"),
                "expires_at": _time.time() + int(flow.get("expires_in") or 900),
            }
            try:
                result = app.acquire_token_by_device_flow(flow)  # 同步阻塞直到用户完成
            finally:
                self.pending_flow = None

        self._save_cache()
        if "access_token" not in result:
            err = result.get("error_description") or result.get("error") or str(result)
            raise RuntimeError(f"Device code login failed: {err}")
        return result["access_token"]

    async def get_token(self) -> str:
        """异步入口：把同步 MSAL 调用扔到线程池，避免阻塞 event loop。"""
        return await asyncio.to_thread(self._acquire_token_sync)

    async def clear(self) -> None:
        """清除缓存（登出）。"""
        def _clear() -> None:
            app = self._ensure_app()
            for acc in app.get_accounts():
                app.remove_account(acc)
            if self._cache_path.exists():
                try:
                    self._cache_path.unlink()
                except OSError:
                    pass
        await asyncio.to_thread(_clear)


# 进程单例
auth = GraphAuth()
