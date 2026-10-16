"""本地化补丁：为从 Octop 移植的 gateway 适配器提供最小依赖实现。

Octop 原 adapter 引用了 octop.infra.utils.paths / cli_runner / cli_dirs /
mail_servers / builder 等内部模块。BT 没有这些，这里提供最小本地实现：
- mail_servers：纯数据，直接移植
- normalize_weknora_base_url：纯函数
- cli_runner / cli_dirs：占位，调用时给出清晰错误（这些 adapter 需要外部 CLI 二进制）
- cli_fingerprint：占位
- feishu_creds / wecom_creds / sanitize_plugin_tool_name：占位
这样既能让文件通过 import，又不引入 octop 依赖。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit, urlunsplit


# ── mail_servers（移植自 Octop，纯数据）────────────────────
MailServers = tuple[str, int, str, int]

_MAIL_PROVIDER_PRESETS: dict[str, MailServers] = {
    "qq": ("imap.qq.com", 993, "smtp.qq.com", 587),
    "gmail": ("imap.gmail.com", 993, "smtp.gmail.com", 587),
}
_NETEASE_DOMAIN_SERVERS: dict[str, MailServers] = {
    "163.com": ("imap.163.com", 993, "smtp.163.com", 587),
    "126.com": ("imap.126.com", 993, "smtp.126.com", 587),
    "yeah.net": ("imap.yeah.net", 993, "smtp.yeah.net", 587),
}
_NETEASE_DEFAULT_SERVERS = _NETEASE_DOMAIN_SERVERS["163.com"]
NETEASE_IMAP_HOSTS = frozenset(s[0] for s in _NETEASE_DOMAIN_SERVERS.values())
NETEASE_SMTP_HOSTS = frozenset(s[2] for s in _NETEASE_DOMAIN_SERVERS.values())
DEFAULT_IMAP_HOST = "imap.qq.com"
DEFAULT_SMTP_HOST = "smtp.qq.com"
IMAP_CONNECT_TIMEOUT = 30


def _email_domain(email: str) -> str:
    text = email.strip().lower()
    return text.rsplit("@", 1)[-1] if "@" in text else ""


def resolve_mail_servers(credentials: dict[str, Any]) -> MailServers:
    provider = str(credentials.get("mail_provider") or "").strip().lower()
    email = str(credentials.get("email") or "").strip().lower()
    domain = _email_domain(email)
    if provider == "netease" or domain in _NETEASE_DOMAIN_SERVERS:
        return _NETEASE_DOMAIN_SERVERS.get(domain, _NETEASE_DEFAULT_SERVERS)
    if provider in _MAIL_PROVIDER_PRESETS:
        return _MAIL_PROVIDER_PRESETS[provider]
    if domain in ("qq.com", "foxmail.com"):
        return _MAIL_PROVIDER_PRESETS["qq"]
    if domain == "gmail.com":
        return _MAIL_PROVIDER_PRESETS["gmail"]
    return (
        str(credentials.get("imap_host") or DEFAULT_IMAP_HOST),
        int(credentials.get("imap_port") or 993),
        str(credentials.get("smtp_host") or DEFAULT_SMTP_HOST),
        int(credentials.get("smtp_port") or 587),
    )


def correct_netease_imap_host(email: str, stored_host: str | None) -> str:
    host = str(stored_host or "").strip()
    preferred = preferred_netease_imap_host(email)
    if preferred and (not host or host in NETEASE_IMAP_HOSTS):
        return preferred
    return host or DEFAULT_IMAP_HOST


def correct_netease_smtp_host(email: str, stored_host: str | None) -> str:
    host = str(stored_host or "").strip()
    preferred = preferred_netease_smtp_host(email)
    if preferred and (not host or host in NETEASE_SMTP_HOSTS):
        return preferred
    return host or DEFAULT_SMTP_HOST


# ── weknora normalize（移植自 Octop builder，纯函数）─────────
def validate_mcp_http_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url:
        raise ValueError("base_url 不能为空")
    if " " in url:
        raise ValueError("base_url 不能含空格")
    return url


def normalize_weknora_base_url(raw: str) -> str:
    url = validate_mcp_http_url(raw)
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain a query string or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/v1"):
        path = f"{path}/api/v1"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


# ── cli_runner / cli_dirs（委托真实 cli_install，支持自动安装）──
class _ExternalCliRequired(RuntimeError):
    """标记该连接器需要主机安装对应 CLI 二进制（feishu/wecom/agently/obsidian）。"""


def resolve_binary(name: str) -> str:
    # 先确保用户级 npm 全局 bin（~/.npm-global/bin）与 ~/.local/bin 在 PATH 中，
    # 否则自动安装到用户级目录后 shutil.which 仍找不到（与 Octop 行为一致）。
    try:
        from octop_bridge.gateway_adapters.cli_install import ensure_cli_path
        ensure_cli_path()
    except Exception:  # noqa: BLE001
        pass
    path = shutil.which(name)
    if not path:
        raise _ExternalCliRequired(
            f"未找到主机命令 {name!r}。请在「连接器」面板点击「安装 CLI」，"
            f"或在主机执行对应的 npm install 命令后重试。"
        )
    return path


def run_cli(argv, *, env=None, timeout_s=30.0, cwd=None, stdin_text=None) -> str:
    if not argv:
        raise ValueError("CLI argv is empty")
    completed = subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True,
        env=dict(env) if env is not None else None,
        timeout=timeout_s, cwd=cwd, check=False,
    )
    if completed.returncode == 0:
        return (completed.stdout or "").strip() or "{}"
    raise ValueError((completed.stderr or completed.stdout or f"CLI exited {completed.returncode}")[:4000])


def resolve_cli_config_key(creds: dict[str, Any]) -> str:
    for field in ("cli_config_key", "instance_id"):
        val = str(creds.get(field) or "").strip()
        if val:
            return val
    raise ValueError("cli_config_key or instance_id is required")


def cleanup_creds_cli_dirs(kind: str, creds: dict[str, Any], *, keep=None) -> None:
    return None  # 占位：BT 不维护 per-instance CLI 目录


def credential_fingerprint(*args: Any) -> str:
    return secrets.token_hex(8)


def sanitize_plugin_tool_name(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def prepare_feishu_cli_env(*args: Any, **kwargs: Any):
    raise _ExternalCliRequired(
        "feishu-cli（lark-cli）尚未就绪。请先在「连接器」面板安装 CLI，再完成飞书登录授权。"
    )


def materialize_wecom_bot_config(*args: Any, **kwargs: Any):
    raise _ExternalCliRequired(
        "wecom-cli 尚未就绪。请先在「连接器」面板安装 CLI。"
    )


def read_mcp_items_count(*args: Any, **kwargs: Any) -> int:
    raise _ExternalCliRequired("wecom-cli 二进制未安装")


# obsidian-cli 安装占位
OBSIDIAN_CLI_MISSING = "OBSIDIAN_CLI_MISSING"
OBSIDIAN_NOT_RUNNING = "OBSIDIAN_NOT_RUNNING"


def locate_obsidian_binary() -> str:
    raise _ExternalCliRequired("obsidian-cli 二进制未安装")


# PathLayout 占位（Octop 用它定位 CLI 实例目录；BT 不维护该目录）
class PathLayout:
    @classmethod
    def from_env(cls) -> "PathLayout":
        return cls()

    def connector_cli_instance_dir(self, kind: str, key: str):
        import pathlib
        return pathlib.Path.home() / ".bt-octop-bridge" / "connector-cli" / kind / key
