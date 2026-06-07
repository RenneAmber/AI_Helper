"""
邮箱账号配置 + 主流服务商 IMAP/SMTP 预设。

设计要点：
- v1 暂时用环境变量配置「单个默认账号」，最快打通端到端
- 多账号 / OAuth / 加密存储留到 v2
- 通过 EMAIL_PROVIDER 选择预设（qq / gmail / outlook），可被显式 HOST/PORT 覆盖
- 同时支持 `.env` 文件：模块导入时若发现项目根目录存在 .env，自动读入 os.environ
  （已存在的同名变量不会被覆盖，命令行 $env: 仍然优先）
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv_if_present() -> None:
    """非常轻量的 .env 加载器：避免再多一个第三方依赖。"""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",  # proactive_ai_backend/.env
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except Exception:
            # 不让 .env 解析问题阻塞应用启动
            continue


_load_dotenv_if_present()


# 主流邮箱的 IMAP/SMTP 默认参数
PRESETS: dict[str, dict] = {
    "qq": {
        # QQ 邮箱：需要先在网页版「设置 → 账号」里
        #   1) 开启 IMAP/SMTP 服务
        #   2) 生成"授权码"（16 位），用授权码作为 EMAIL_PASSWORD
        "imap_host": "imap.qq.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.qq.com", "smtp_port": 465, "smtp_ssl": True,
    },
    "163": {
        # 网易 163 邮箱：同样需要在网页版开启 IMAP/SMTP 并生成"客户端授权密码"
        "imap_host": "imap.163.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.163.com", "smtp_port": 465, "smtp_ssl": True,
    },
    "gmail": {
        # Gmail：账号必须开启 2 步验证，然后在「应用专用密码」里生成 App Password
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_ssl": True,
    },
    "outlook": {
        # Outlook / Hotmail / Office 365：注意微软已在逐步关闭 IMAP basic auth；
        # 商业租户基本要走 Microsoft Graph OAuth2。个人版部分账号仍可用 IMAP。
        "imap_host": "outlook.office365.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_ssl": False,  # STARTTLS
    },
}


@dataclass
class EmailAccount:
    address: str
    password: str         # QQ/163 是授权码；Gmail 是 App Password
    imap_host: str
    imap_port: int
    imap_ssl: bool
    smtp_host: str
    smtp_port: int
    smtp_ssl: bool        # True=SSL/TLS 直连；False=STARTTLS

    @property
    def configured(self) -> bool:
        return bool(self.address) and bool(self.password)


def get_default_account() -> EmailAccount:
    """从环境变量读取默认账号；任意字段缺失时账号 `.configured` 为 False。"""
    provider = (os.getenv("EMAIL_PROVIDER") or "qq").lower()
    preset = PRESETS.get(provider, PRESETS["qq"])

    def _get(key: str, default):
        return os.getenv(key, default)

    return EmailAccount(
        address=_get("EMAIL_ADDRESS", ""),
        password=_get("EMAIL_PASSWORD", ""),
        imap_host=_get("EMAIL_IMAP_HOST", preset["imap_host"]),
        imap_port=int(_get("EMAIL_IMAP_PORT", preset["imap_port"])),
        imap_ssl=str(_get("EMAIL_IMAP_SSL", preset["imap_ssl"])).lower() in ("1", "true", "yes"),
        smtp_host=_get("EMAIL_SMTP_HOST", preset["smtp_host"]),
        smtp_port=int(_get("EMAIL_SMTP_PORT", preset["smtp_port"])),
        smtp_ssl=str(_get("EMAIL_SMTP_SSL", preset["smtp_ssl"])).lower() in ("1", "true", "yes"),
    )
