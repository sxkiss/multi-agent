"""OAuth2 连接器提供商配置表。

每个支持的 OAuth2 连接器一个 entry，包含：
- 端点 URL（authorization、token、registration、resource metadata）
- Scopes、grant types、code challenge methods
- 是否支持 DCR（动态客户端注册）
- 回调路径、MCP 端点等
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class OAuth2ProviderConfig:
    """单个 OAuth2 提供商的配置。"""

    # 标识
    kind: str
    name_zh: str
    name_en: str

    # 端点
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None = None
    token_revocation_endpoint: str | None = None
    resource_metadata_endpoint: str | None = None

    # Scopes
    scopes_supported: list[str] | None = None

    # 能力
    code_challenge_methods_supported: list[str] | None = None  # ["S256"]
    grant_types_supported: list[str] | None = None  # ["authorization_code", "refresh_token"]
    client_authentication_methods_supported: list[str] | None = None

    # 回调
    callback_path: str = "/api/v2/connectors/oauth2/callback"
    # redirect_uri 由 OAuthManager 动态构造，此处仅作为兜底

    # MCP 端点（用于生成 MCP spec）
    mcp_url: str | None = None
    mcp_resource: str | None = None

    # 能力标志
    supports_dcr: bool = False
    """是否支持 OAuth 2.0 Dynamic Client Registration (RFC 7591)。"""

    supports_refresh: bool = True
    """是否支持 refresh_token 刷新。"""

    supports_manual_token: bool = False
    """是否允许手动粘贴 access_token 作为兜底。"""

    # token_endpoint_auth 默认值（客户端认证方式）
    # "client_secret_basic", "client_secret_post", "none" 等
    default_token_endpoint_auth: str = "client_secret_basic"

    @property
    def supports_pkce(self) -> bool:
        return "S256" in (self.code_challenge_methods_supported or [])

    @property
    def callback_url_template(self) -> str:
        return f"http://{{host}}{{port}}{self.callback_path}"


# ── 5 个 OAuth2 连接器配置 ──────────────────────────────────────

PROVIDERS: dict[str, OAuth2ProviderConfig] = {
    # 1. Notion — 完整 OAuth 2.1 发现 + DCR
    "notion": OAuth2ProviderConfig(
        kind="notion",
        name_zh="Notion",
        name_en="Notion",
        issuer="https://mcp.notion.com",
        authorization_endpoint="https://mcp.notion.com/authorize",
        token_endpoint="https://mcp.notion.com/token",
        registration_endpoint="https://mcp.notion.com/register",
        scopes_supported=["default", "ema_issuer:manage"],
        code_challenge_methods_supported=["S256"],
        grant_types_supported=["authorization_code", "refresh_token"],
        client_authentication_methods_supported=[
            "client_secret_basic",
            "client_secret_post",
        ],
        mcp_url="https://mcp.notion.com/mcp",
        supports_dcr=True,
        supports_refresh=True,
        default_token_endpoint_auth="client_secret_basic",
    ),

    # 2. 腾讯 Ardot — 完整 OAuth 2.1 发现 + DCR
    "tencent-ardot": OAuth2ProviderConfig(
        kind="tencent-ardot",
        name_zh="腾讯 Ardot",
        name_en="Tencent Ardot",
        issuer="https://ardot.tencent.com",
        authorization_endpoint="https://ardot.tencent.com/oauth/authorize",
        token_endpoint="https://ardot.tencent.com/oauth/token",
        registration_endpoint="https://ardot.tencent.com/oauth/register",
        scopes_supported=["mcp:use"],
        code_challenge_methods_supported=["S256"],
        grant_types_supported=["authorization_code", "refresh_token"],
        client_authentication_methods_supported=["none"],
        mcp_url="https://ardot.tencent.com/mcp",
        supports_dcr=True,
        supports_refresh=True,
        default_token_endpoint_auth="none",  # 无需 client_secret
    ),

    # 3. 滴答清单 — 完整 OAuth 2.1 发现 + DCR
    "dida365": OAuth2ProviderConfig(
        kind="dida365",
        name_zh="滴答清单",
        name_en="Dida365",
        issuer="https://dida365.com",
        authorization_endpoint="https://dida365.com/oauth/authorize",
        token_endpoint="https://api.dida365.com/oauth/token",
        registration_endpoint="https://api.dida365.com/oauth/register",
        scopes_supported=["tasks:read", "tasks:write"],
        code_challenge_methods_supported=["S256"],
        grant_types_supported=["authorization_code", "refresh_token"],
        client_authentication_methods_supported=[
            "client_secret_basic",
            "client_secret_post",
        ],
        mcp_url="https://api.dida365.com/mcp",
        mcp_resource="https://api.dida365.com/.well-known/oauth-protected-resource/tasks",
        supports_dcr=True,
        supports_refresh=True,
        default_token_endpoint_auth="client_secret_basic",
    ),

    # 4. OpenAlex — 完整 OAuth 2.1 发现 + DCR（实测 .well-known/oauth-authorization-server）
    # 同时保留手动粘贴 token 作为兜底（避免 OAuth 流因厂商策略临时不可用）
    "openalex": OAuth2ProviderConfig(
        kind="openalex",
        name_zh="OpenAlex",
        name_en="OpenAlex",
        issuer="https://mcp.openalex.org",
        authorization_endpoint="https://mcp.openalex.org/authorize",
        token_endpoint="https://mcp.openalex.org/oauth/token",
        registration_endpoint="https://mcp.openalex.org/oauth/register",
        # RFC 9728 保护资源元数据端点（从 401 响应中获取）
        resource_metadata_endpoint=(
            "https://mcp.openalex.org/.well-known/oauth-protected-resource/mcp"
        ),
        scopes_supported=["openalex:query"],
        code_challenge_methods_supported=["S256"],
        grant_types_supported=["authorization_code", "refresh_token"],
        client_authentication_methods_supported=[
            "client_secret_basic",
            "client_secret_post",
            "none",
        ],
        mcp_url="https://mcp.openalex.org/mcp",
        mcp_resource="https://mcp.openalex.org/mcp",
        supports_dcr=True,
        supports_refresh=True,
        supports_manual_token=True,
        default_token_endpoint_auth="none",
    ),

    # 5. 企查查 — 完整 OAuth 2.1 发现 + DCR（实测 .well-known/oauth-authorization-server）
    # 内部 MCP 模式，但授权服务器独立。同时保留手动粘贴 token 作为兜底。
    "qcc": OAuth2ProviderConfig(
        kind="qcc",
        name_zh="企查查",
        name_en="QCC",
        issuer="https://agent.qcc.com",
        authorization_endpoint="https://agent.qcc.com/oauth/authorize",
        token_endpoint="https://agent.qcc.com/oauth/token",
        registration_endpoint="https://agent.qcc.com/oauth/register",
        # RFC 9728 保护资源元数据端点（从 401 响应中获取）
        resource_metadata_endpoint=(
            "https://agent.qcc.com/mcp/.well-known/oauth-protected-resource/company/stream"
        ),
        scopes_supported=["mcp:tools"],
        code_challenge_methods_supported=["S256"],
        grant_types_supported=["authorization_code", "refresh_token"],
        client_authentication_methods_supported=["none"],
        mcp_url="https://agent.qcc.com/mcp/company/stream",
        mcp_resource=(
            "https://agent.qcc.com/mcp/.well-known/oauth-protected-resource/company/stream"
        ),
        supports_dcr=True,
        supports_refresh=True,
        supports_manual_token=True,
        default_token_endpoint_auth="none",
    ),
}


def get_provider(kind: str) -> OAuth2ProviderConfig | None:
    """按 kind 获取 OAuth2 提供商配置。"""
    return PROVIDERS.get(kind)


def list_providers() -> list[OAuth2ProviderConfig]:
    """返回所有 OAuth2 提供商配置列表。"""
    return list(PROVIDERS.values())


def get_oauth2_kinds() -> list[str]:
    """返回所有支持 OAuth2 的连接器 kind 列表。"""
    return list(PROVIDERS.keys())


def supports_dcr(kind: str) -> bool:
    """指定 kind 是否支持动态客户端注册。"""
    p = get_provider(kind)
    return p.supports_dcr if p else False


def supports_manual_token(kind: str) -> bool:
    """指定 kind 是否支持手动粘贴 token。"""
    p = get_provider(kind)
    return p.supports_manual_token if p else False
