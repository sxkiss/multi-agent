"""本地化补丁：为移植的 gateway 适配器提供最小依赖实现。

原 adapter 引用了上游内部的 paths / cli_runner / cli_dirs /
mail_servers / builder 等模块。BT 没有这些，这里提供最小本地实现：
- mail_servers：纯数据，直接移植
- normalize_weknora_base_url：纯函数
- cli_runner / cli_dirs：占位，调用时给出清晰错误（这些 adapter 需要外部 CLI 二进制）
- cli_fingerprint：占位
- feishu_creds / wecom_creds / sanitize_plugin_tool_name：占位
这样既能让文件通过 import，又不引入上游依赖。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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


def preferred_netease_imap_host(email: str) -> str | None:
    domain = _email_domain(email)
    if domain in _NETEASE_DOMAIN_SERVERS:
        return _NETEASE_DOMAIN_SERVERS[domain][0]
    return None


def preferred_netease_smtp_host(email: str) -> str | None:
    domain = _email_domain(email)
    if domain in _NETEASE_DOMAIN_SERVERS:
        return _NETEASE_DOMAIN_SERVERS[domain][2]
    return None


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
    # 否则自动安装到用户级目录后 shutil.which 仍找不到（与上游实现行为一致）。
    try:
        from connector_hub.gateway_adapters.cli_install import ensure_cli_path
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
    # 测试凭据等场景尚未生成 instance_id：用唯一标识派生稳定 key，
    # 使「测试」与实际保存共用同一 CLI 配置目录（可复现、不污染）。
    for field in ("app_id", "client_id", "bot_id", "api_key", "email", "token"):
        val = str(creds.get(field) or "").strip()
        if val:
            digest = hashlib.sha256(val.encode("utf-8")).hexdigest()[:16]
            return f"cred-{digest}"
    raise ValueError("cli_config_key or instance_id is required")


def cleanup_creds_cli_dirs(kind: str, creds: dict[str, Any], *, keep=None) -> None:
    return None  # 占位：BT 不维护 per-instance CLI 目录


def credential_fingerprint(*args: Any) -> str:
    return secrets.token_hex(8)


def sanitize_plugin_tool_name(name: str) -> str:
    import re
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def prepare_feishu_cli_env(config_dir, *, app_id=None, app_secret=None, default_as="bot"):
    """在隔离目录下初始化/复用 lark-cli 配置，返回 (binary, env)。

    lark-cli 把配置写到 $HOME/.lark-cli。这里设置 HOME=config_dir，
    使每个连接器实例使用独立配置目录，互不污染、也避免动全局配置。
    """
    binary = resolve_binary("lark-cli")
    import pathlib
    config_dir = pathlib.Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = config_dir / ".lark-cli" / "config.json"

    env = os.environ.copy()
    env["HOME"] = str(config_dir)

    # 已初始化过则直接复用（不覆盖已有 profile / token）
    if not cfg_file.exists():
        if not app_id or not app_secret:
            raise _ExternalCliRequired(
                "飞书 CLI 配置尚未初始化。请在「连接器」面板填写 App ID / App Secret 后保存并测试。"
            )
        # 用 --app-secret-stdin 避免 app_secret 出现在进程列表
        init_args = [
            binary, "config", "init",
            "--app-id", str(app_id),
            "--app-secret-stdin",
            "--name", "default",
            "--force-init",
        ]
        try:
            completed = subprocess.run(
                init_args,
                input=str(app_secret) + "\n",
                capture_output=True,
                text=True,
                env=env,
                timeout=60.0,
                cwd=str(config_dir),
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise _ExternalCliRequired("飞书 CLI 配置初始化超时，请重试") from None
        if completed.returncode != 0:
            err = (completed.stderr or completed.stdout or "").strip()
            raise _ExternalCliRequired(
                f"飞书 CLI 配置初始化失败：{err[:300] or '未知错误'}"
            )
    else:
        # 已初始化：校验 app_id 匹配，避免用错实例目录
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            apps = cfg.get("apps") or []
            if app_id and apps and str(apps[0].get("appId")) != str(app_id):
                raise _ExternalCliRequired(
                    f"飞书 CLI 配置已存在但 App ID 不匹配（{apps[0].get('appId')} != {app_id}）。"
                    "请清理该连接器实例后重建。"
                )
        except _ExternalCliRequired:
            raise
        except Exception:  # noqa: BLE001
            pass  # 配置解析失败时不做强校验，让 auth status 自行报错

    return binary, env


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


# PathLayout 占位（上游用它定位 CLI 实例目录；BT 不维护该目录）
class PathLayout:
    @classmethod
    def from_env(cls) -> "PathLayout":
        return cls()

    def connector_cli_instance_dir(self, kind: str, key: str):
        import pathlib
        return pathlib.Path.home() / ".bt-connector-hub" / "connector-cli" / kind / key

    def ensure_connector_cli_instance_dir(self, kind: str, key: str):
        """确保 CLI 实例配置目录存在并返回其路径（对齐上游 PathLayout 接口）。"""
        d = self.connector_cli_instance_dir(kind, key)
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d


# ── i18n 翻译占位（原 adapter 引用上游 i18n 模块）────────
# 这里提供最小 fallback：支持 tr(key) 与 tr(key, locale) 两种签名，
# 找不到翻译时返回 key 最后一段（去除 "connector.agently." 等前缀），
# 保证适配器逻辑（尤其是 *_cli 的 list_tools）可正常运行而不抛 NameError。
_KNOWN_TRANSLATIONS: dict[str, str] = {
    "connector.agently.untrusted": "工具输出为原始 CLI 响应，未经信任，请人工核对。",
    "connector.agently.confirmation": "此操作需要确认。",
    "connector.agently.recipients": "收件人（逗号分隔）。",
    "connector.agently.attachment_handles": "附件句柄（每行一个）。",
}


def tr(key: str, locale: str = "en") -> str:
    """最小 i18n fallback：返回内置翻译，未知 key 返回可读的末尾段。

    兼容适配器中 tr(key) 与 tr(key, locale) 两种调用形态。
    """
    text = _KNOWN_TRANSLATIONS.get(str(key))
    if text is not None:
        return text
    # 未知 key：取点号分隔的最后一段作为可读兜底，避免抛异常。
    seg = str(key)
    return seg.rsplit(".", 1)[-1] if "." in seg else seg
