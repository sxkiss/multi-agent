"""连接器目录（catalog）加载。

"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_FILE = os.path.join(_HERE, "catalog.json")

AuthKind = Literal[
    "personal_token", "oauth2", "auth_code", "api_key",
    "imap_app_password", "session_cookie", "api_credentials", "custom_fields",
]
McpMode = Literal["remote", "gateway", "internal", "stdio", "cli"]
CredentialFieldType = Literal["text", "password", "url", "tags"]

# BT 实际支持录入的凭据类型（oauth 类型走手动粘贴 access_token 兜底，同样可接入）
SUPPORTED_AUTH_KINDS = frozenset({
    "none", "personal_token", "api_key", "imap_app_password", "custom_fields", "oauth",
})

# gateway 模式连接器（in-process Python 适配器），这些需要 connectors_gateway 注册
GATEWAY_KINDS = frozenset({
    "agently-cli", "qq-mail", "qq-music", "fliggy", "baidu-map", "ctrip-wendao",
    "meituan-travel", "yuandian", "tencent-ima", "tencent-news", "wechat-reading",
    "feishu-cli", "wecom-cli", "obsidian-cli", "weknora",
})

# internal 模式（自托管 HTTP MCP）；BT 暂无对应自托管服务，标记但走 gateway 协议兜底
INTERNAL_KINDS = frozenset({"qcc"})


@dataclass(frozen=True)
class LocalizedText:
    en: str
    zh: str

    def resolve(self, locale: str = "en") -> str:
        return self.zh if locale.lower().startswith("zh") else self.en


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: LocalizedText
    field_type: CredentialFieldType = "text"
    required: bool = True
    placeholder: LocalizedText | None = None
    help: LocalizedText | None = None
    secret: bool = False


@dataclass(frozen=True)
class CatalogEntry:
    kind: str
    name: LocalizedText
    description: LocalizedText
    auth_kind: AuthKind
    doc_url: str
    icon: str
    color: str
    phase: str
    mcp_mode: McpMode
    category: str
    quick_auth_url: str | None = None
    login_url: str | None = None
    guide_url: str | None = None
    manual_url: str | None = None
    auth_hint: LocalizedText | None = None
    allowed_tools: tuple[str, ...] | None = None
    oauth_issuer: str | None = None
    mcp_url: str | None = None
    oauth_resource: str | None = None
    oauth_scopes: str | None = None
    mcp_user_agent: str | None = None
    credential_fields: tuple[CredentialField, ...] = ()
    remote_transport: str = "raw_http"
    # MCP headers 模板（值可能含 ${VAR} 占位符，由 service 层用用户凭据替换）
    headers_template: dict[str, str] | None = None
    # 本地 stdio MCP 的启动命令（npx/uvx 等），用于 generate_mcp_spec 直接生成 stdio 配置
    stdio_command: str | None = None
    stdio_args: list[str] | None = None
    stdio_env: dict[str, str] | None = None

    # ── 展示感知 ──────────────────────────────────────────
    @property
    def supported(self) -> bool:
        """BT 能否实际录入凭据并接入。"""
        if self.auth_kind == "oauth2":
            from connector_hub.oauth_providers import get_provider
            return get_provider(self.kind) is not None
        if self.auth_kind not in SUPPORTED_AUTH_KINDS:
            return False
        # stdio 本地 MCP：BT mcp_client 原生支持，只要有启动命令即可接入
        if self.mcp_mode == "stdio":
            return bool(self.stdio_command)
        # gateway 模式需要已注册的进程内适配器才能真正调用
        if self.mcp_mode == "gateway":
            from connector_hub.connectors_gateway import get_gateway_adapter
            return get_gateway_adapter(self.kind) is not None
        # internal 模式（qcc 自托管）走 oauth2 手动 token 兜底，同样可接入
        if self.mcp_mode == "internal":
            return True
        return True

    def to_api(self, locale: str = "en") -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name.resolve(locale),
            "description": self.description.resolve(locale),
            "auth_kind": self.auth_kind,
            "doc_url": self.doc_url,
            "icon": self.icon,
            "color": self.color,
            "phase": self.phase,
            "mcp_mode": self.mcp_mode,
            "category": self.category,
            "quick_auth_url": self.quick_auth_url,
            "login_url": self.login_url,
            "guide_url": self.guide_url or self.doc_url,
            "manual_url": self.manual_url or self.guide_url or self.doc_url,
            "auth_hint": self.auth_hint.resolve(locale) if self.auth_hint else None,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools else None,
            "credential_fields": [
                {
                    "key": f.key,
                    "label": f.label.resolve(locale),
                    "field_type": f.field_type,
                    "required": f.required,
                    "placeholder": f.placeholder.resolve(locale) if f.placeholder else None,
                    "help": f.help.resolve(locale) if f.help else None,
                    "secret": f.secret,
                }
                for f in self.credential_fields
            ],
            "supported": self.supported,
        }


def _loc(d: Any) -> LocalizedText:
    if isinstance(d, dict):
        return LocalizedText(en=d.get("en", ""), zh=d.get("zh", ""))
    return LocalizedText(en=str(d), zh=str(d))


def _fields(raw: list[dict[str, Any]]) -> tuple[CredentialField, ...]:
    out = []
    for f in raw:
        out.append(CredentialField(
            key=f["key"],
            label=_loc(f.get("label", f["key"])),
            field_type=f.get("field_type", "text"),
            required=f.get("required", True),
            placeholder=_loc(f["placeholder"]) if f.get("placeholder") else None,
            help=_loc(f["help"]) if f.get("help") else None,
            secret=bool(f.get("secret", False)),
        ))
    return tuple(out)


_ENTRIES: dict[str, CatalogEntry] | None = None


def _load() -> dict[str, CatalogEntry]:
    global _ENTRIES
    if _ENTRIES is not None:
        return _ENTRIES
    with open(_CATALOG_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    entries: dict[str, CatalogEntry] = {}
    for c in data.get("connectors", []):
        entries[c["kind"]] = CatalogEntry(
            kind=c["kind"],
            name=_loc(c.get("name", c["kind"])),
            description=_loc(c.get("description", "")),
            auth_kind=c.get("auth_kind", "api_key"),
            doc_url=c.get("doc_url", ""),
            icon=c.get("icon", c["kind"]),
            color=c.get("color", "#666666"),
            phase=c.get("phase", "available"),
            mcp_mode=c.get("mcp_mode", "remote"),
            category=c.get("category", "other"),
            quick_auth_url=c.get("quick_auth_url"),
            login_url=c.get("login_url"),
            guide_url=c.get("guide_url"),
            manual_url=c.get("manual_url"),
            auth_hint=_loc(c["auth_hint"]) if c.get("auth_hint") else None,
            allowed_tools=tuple(c.get("allowed_tools", [])) or None,
            oauth_issuer=c.get("oauth_issuer"),
            mcp_url=c.get("mcp_url"),
            oauth_resource=c.get("oauth_resource"),
            oauth_scopes=c.get("oauth_scopes"),
            mcp_user_agent=c.get("mcp_user_agent"),
            credential_fields=_fields(c.get("credential_fields", [])),
            remote_transport=c.get("remote_transport", "raw_http"),
            headers_template=c.get("headers_template"),
            stdio_command=c.get("stdio_command"),
            stdio_args=c.get("stdio_args"),
            stdio_env=c.get("stdio_env"),
        )
    _ENTRIES = entries
    return _ENTRIES


def get_catalog_entry(kind: str) -> CatalogEntry | None:
    return _load().get(kind)


def list_catalog_entries() -> list[CatalogEntry]:
    return list(_load().values())


def reset_catalog_cache() -> None:
    """强制清除进程级缓存，下次调用 _load() 会重新读取 catalog.json。

    用于管理面板编辑 catalog 后无需重启服务即可生效。
    """
    global _ENTRIES
    _ENTRIES = None


def is_gateway_kind(kind: str) -> bool:
    return kind in GATEWAY_KINDS


def get_oauth2_config(kind: str):
    """返回指定 kind 的 OAuth2 配置，不存在时返回 None。"""
    from connector_hub.oauth_providers import get_provider
    return get_provider(kind)
