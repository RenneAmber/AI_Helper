"""
Email 多账号管理 + 客户端工厂。

设计目标
--------
1. 多账号共存：QQ（IMAP）+ Outlook（Microsoft Graph）同时可用，互不影响。
2. 兼容旧单账号配置：未设 `EMAIL_ACCOUNTS` 时，回退到 `EMAIL_PROVIDER/EMAIL_ADDRESS/EMAIL_PASSWORD`
   定义的隐式 "default" 账号，老用户零改动。
3. 业务调用统一入口：`get_email_client(name)` 返回鸭子类型一致的客户端实例。

环境变量约定（多账号模式）
-------------------------
EMAIL_ACCOUNTS=qq,outlook          # CSV 列出账号名（任意标识符）
EMAIL_DEFAULT_ACCOUNT=qq           # 未指定 name 时用谁；省略则取列表第一个

# 每个账号一组（以下以 qq 为例，把 QQ 改成你自己的名字）：
EMAIL_QQ_BACKEND=imap              # imap | msgraph
EMAIL_QQ_PROVIDER=qq               # qq | 163 | gmail | outlook（决定 IMAP/SMTP host）
EMAIL_QQ_ADDRESS=foo@qq.com
EMAIL_QQ_PASSWORD=...              # QQ 是授权码、Gmail 是 App Password
# 可选覆写：EMAIL_QQ_IMAP_HOST/PORT/SSL、EMAIL_QQ_SMTP_HOST/PORT/SSL

# msgraph 账号只需声明 backend；走 ms_auth 的共享 device-code token：
EMAIL_OUTLOOK_BACKEND=msgraph
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from ..config import settings
from .email_accounts import PRESETS, EmailAccount, get_default_account

logger = logging.getLogger("email_factory")


# ---------- 账号注册表 ----------

@dataclass
class AccountSpec:
    """一个账号的"配方"：决定怎么实例化 client。"""
    name: str
    backend: str  # "imap" | "msgraph"
    account: EmailAccount | None  # imap 才有；msgraph 不需要

    @property
    def configured(self) -> bool:
        if self.backend == "msgraph":
            return True  # 共享 GraphAuth，运行期触发 device flow
        return bool(self.account and self.account.configured)


def _build_imap_account_from_env(prefix: str) -> EmailAccount:
    """读取 EMAIL_<NAME>_* 环境变量，缺省走预设。"""
    def _g(suffix: str, default: Any = "") -> str:
        return os.getenv(f"{prefix}_{suffix}", default)

    provider = (_g("PROVIDER", "qq") or "qq").lower()
    preset = PRESETS.get(provider, PRESETS["qq"])
    return EmailAccount(
        address=_g("ADDRESS", ""),
        password=_g("PASSWORD", ""),
        imap_host=_g("IMAP_HOST", preset["imap_host"]),
        imap_port=int(_g("IMAP_PORT", str(preset["imap_port"]))),
        imap_ssl=str(_g("IMAP_SSL", str(preset["imap_ssl"]))).lower() in ("1", "true", "yes"),
        smtp_host=_g("SMTP_HOST", preset["smtp_host"]),
        smtp_port=int(_g("SMTP_PORT", str(preset["smtp_port"]))),
        smtp_ssl=str(_g("SMTP_SSL", str(preset["smtp_ssl"]))).lower() in ("1", "true", "yes"),
    )


def _load_specs() -> tuple[dict[str, AccountSpec], str]:
    """返回 (name -> spec, default_name)。"""
    raw = (os.getenv("EMAIL_ACCOUNTS") or "").strip()
    specs: dict[str, AccountSpec] = {}

    if raw:
        names = [n.strip() for n in raw.split(",") if n.strip()]
        for name in names:
            env_prefix = f"EMAIL_{name.upper()}"
            backend = (os.getenv(f"{env_prefix}_BACKEND") or "imap").strip().lower()
            account = _build_imap_account_from_env(env_prefix) if backend == "imap" else None
            specs[name] = AccountSpec(name=name, backend=backend, account=account)

        default_name = (os.getenv("EMAIL_DEFAULT_ACCOUNT") or names[0]).strip()
        if default_name not in specs:
            logger.warning(
                "email_default_account_unknown",
                extra={"name": default_name, "available": list(specs)},
            )
            default_name = names[0]
        return specs, default_name

    # —— 回退：旧单账号配置 ——
    # 1) 老的全局 EMAIL_BACKEND（覆盖 settings.email_backend）
    # 2) 老的 EMAIL_PROVIDER/EMAIL_ADDRESS/EMAIL_PASSWORD → "default" 账号
    backend = (os.getenv("EMAIL_BACKEND") or settings.email_backend or "imap").strip().lower()
    account = get_default_account() if backend == "imap" else None
    specs["default"] = AccountSpec(name="default", backend=backend, account=account)
    return specs, "default"


# 模块级单例（进程启动后稳定不变）
_SPECS, _DEFAULT_NAME = _load_specs()


# ---------- 公共 API ----------

def list_account_names() -> list[str]:
    """对外可见的账号名列表，default 排第一。"""
    names = list(_SPECS.keys())
    if _DEFAULT_NAME in names:
        names.remove(_DEFAULT_NAME)
        names.insert(0, _DEFAULT_NAME)
    return names


def get_default_account_name() -> str:
    return _DEFAULT_NAME


def get_spec(name: str | None = None) -> AccountSpec:
    """name 为空 → 默认账号；找不到名字 → 抛 KeyError。"""
    key = (name or _DEFAULT_NAME).strip()
    if key not in _SPECS:
        raise KeyError(
            f"unknown email account: {key!r}; available: {list(_SPECS)}"
        )
    return _SPECS[key]


def get_email_client(name: str | None = None) -> Any:
    """返回指定账号的 email 客户端（鸭子类型一致：IMAP/Graph 两个实现接口同构）。"""
    spec = get_spec(name)
    if spec.backend == "msgraph":
        from .email_msgraph import EmailGraphClient
        return EmailGraphClient()
    from .email_imap import EmailClient
    if spec.account is None:
        # 理论不会发生（_load_specs 已为 imap 构造 account）；保险
        raise RuntimeError(f"imap account {spec.name!r} missing account config")
    return EmailClient(spec.account)


def get_account_for_status(name: str | None = None) -> EmailAccount:
    """`/v1/email/account` 健康检查端点用：返回某账号配置概览。
    Graph 后端用占位字段告诉前端走的是哪条路径。
    """
    spec = get_spec(name)
    if spec.backend == "msgraph":
        return EmailAccount(
            address="(msgraph: 当前登录的 Microsoft 账号)",
            password="oauth2",  # 占位让 .configured 为 True
            imap_host="graph.microsoft.com",
            imap_port=443,
            imap_ssl=True,
            smtp_host="graph.microsoft.com",
            smtp_port=443,
            smtp_ssl=True,
        )
    return spec.account or get_default_account()


def describe_accounts() -> list[dict]:
    """诊断 / 前端展示用：返回所有账号的非敏感摘要。"""
    out: list[dict] = []
    for name in list_account_names():
        spec = _SPECS[name]
        item: dict[str, Any] = {
            "name": name,
            "backend": spec.backend,
            "default": name == _DEFAULT_NAME,
            "configured": spec.configured,
        }
        if spec.backend == "imap" and spec.account:
            item["address"] = spec.account.address
            item["imap_host"] = spec.account.imap_host
        else:
            item["address"] = "(当前登录的 Microsoft 账号)"
        out.append(item)
    return out
