"""OAuth2 凭据管理器。

核心职责：
1. PKCE 流程（code_challenge = S256）
2. 动态客户端注册（DCR，RFC 7591）
3. 授权码流程（浏览器跳转 → 本地回调 → 换 token）
4. Token 存储和刷新（access_token + refresh_token）
5. 401 时的 token 自动刷新
6. 手动粘贴 token 兜底（对无标准发现的连接器）
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from connector_hub import PROJECT_ROOT
from connector_hub.connectors_service import (
    _read_config,
    _write_config,
    encrypt_credentials,
    decrypt_credentials,
)
from connector_hub.oauth_providers import (
    PROVIDERS,
    OAuth2ProviderConfig,
    get_provider,
    list_providers,
)

logger = logging.getLogger("connector-hub-oauth2")

# ── 数据模型 ───────────────────────────────────────────────────

@dataclass
class OAuth2Credentials:
    """OAuth2 凭据数据模型。"""
    kind: str
    access_token: str
    refresh_token: str | None = None
    expires_at: int | None = None  # unix timestamp
    token_type: str = "Bearer"
    scope: str | None = None
    # 以下字段用于 DCR 注册后的客户端标识
    client_id: str | None = None
    client_secret: str | None = None
    # 手动 token 模式下的原始 token
    manual_token: str | None = None
    # 注册响应原始数据（用于刷新等）
    registration_response: dict[str, Any] | None = None

    def is_expired(self) -> bool:
        """检查 token 是否过期（提前 60 秒判定过期）。"""
        if self.expires_at is None:
            return False
        return int(time.time()) + 60 >= self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OAuth2Credentials:
        return cls(
            kind=d.get("kind", ""),
            access_token=d.get("access_token", ""),
            refresh_token=d.get("refresh_token"),
            expires_at=d.get("expires_at"),
            token_type=d.get("token_type", "Bearer"),
            scope=d.get("scope"),
            client_id=d.get("client_id"),
            client_secret=d.get("client_secret"),
            manual_token=d.get("manual_token"),
            registration_response=d.get("registration_response"),
        )

    def serialize_encrypted(self) -> str:
        """序列化为 JSON 后加密。"""
        return encrypt_credentials(self.to_dict())

    @classmethod
    def from_encrypted(cls, token: str) -> OAuth2Credentials:
        """从加密字符串解密并还原。"""
        d = decrypt_credentials(token)
        return cls.from_dict(d)


@dataclass
class AuthState:
    """授权流程中的临时状态。"""
    kind: str
    code_verifier: str
    redirect_uri: str
    state: str
    created_at: float  # unix timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "code_verifier": self.code_verifier,
            "redirect_uri": self.redirect_uri,
            "state": self.state,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuthState:
        return cls(
            kind=d["kind"],
            code_verifier=d["code_verifier"],
            redirect_uri=d["redirect_uri"],
            state=d["state"],
            created_at=d["created_at"],
        )


# ── PKCE 工具 ───────────────────────────────────────────────────

def generate_code_verifier() -> str:
    """生成随机 code_verifier（43-128 字符）。"""
    return secrets.token_urlsafe(32)  # 43 字符


def generate_code_challenge(code_verifier: str) -> str:
    """生成 S256 方式的 code_challenge。"""
    sha256_hash = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(sha256_hash).rstrip(b"=").decode("ascii")


# ── config.json 存储 ───────────────────────────────────────────

def _get_oauth2_section() -> dict[str, Any]:
    """获取 config.json 中的 oauth2 段（不存在则初始化）。"""
    cfg = _read_config()
    connector_hub = cfg.setdefault("connector_hub", {})
    return connector_hub.setdefault("oauth2", {
        "auth_states": {},
        "clients": {},
        "token_cache": {},
    })


def _save_oauth2_section(section: dict[str, Any]) -> None:
    """保存 oauth2 段到 config.json。"""
    cfg = _read_config()
    cfg.setdefault("connector_hub", {})["oauth2"] = section
    _write_config(cfg)


# ── AuthState 存储 ──────────────────────────────────────────────

def create_auth_state(kind: str, redirect_uri: str) -> tuple[str, AuthState]:
    """创建一个新的授权状态，返回 (state_token, AuthState)。"""
    state = secrets.token_urlsafe(16)
    code_verifier = generate_code_verifier()
    auth_state = AuthState(
        kind=kind,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        state=state,
        created_at=time.time(),
    )

    section = _get_oauth2_section()
    # 设置 10 分钟过期
    section["auth_states"][state] = auth_state.to_dict()
    _save_oauth2_section(section)

    return state, auth_state


def get_auth_state(state: str) -> AuthState | None:
    """获取授权状态（同时检查是否过期）。"""
    section = _get_oauth2_section()
    raw = section.get("auth_states", {}).get(state)
    if not raw:
        return None
    auth_state = AuthState.from_dict(raw)
    # 10 分钟过期
    if time.time() - auth_state.created_at > 600:
        del section["auth_states"][state]
        _save_oauth2_section(section)
        return None
    return auth_state


def remove_auth_state(state: str) -> None:
    """删除授权状态。"""
    section = _get_oauth2_section()
    section.get("auth_states", {}).pop(state, None)
    _save_oauth2_section(section)


# ── 客户端注册（DCR） ───────────────────────────────────────────

def register_client(kind: str, redirect_uri: str) -> tuple[str, str]:
    """动态客户端注册，返回 (client_id, client_secret)。"""
    provider = get_provider(kind)
    if not provider or not provider.registration_endpoint:
        raise ValueError(f"Provider {kind} does not support DCR")

    # 检查是否已注册
    section = _get_oauth2_section()
    clients = section.get("clients", {})
    if kind in clients:
        return clients[kind]["client_id"], clients[kind]["client_secret"]

    # 构造注册请求
    reg_request = {
        "redirect_uris": [redirect_uri],
        "client_name": f"BT Connector Hub - {kind}",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "scope": " ".join(provider.scopes_supported or []),
        "token_endpoint_auth_method": provider.default_token_endpoint_auth,
    }

    headers = {"Content-Type": "application/json"}
    try:
        resp = httpx.post(
            provider.registration_endpoint,
            json=reg_request,
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        reg_response = resp.json()
    except Exception as exc:
        logger.error("DCR failed for %s: %s", kind, exc)
        raise ValueError(f"Dynamic client registration failed: {exc}")

    client_id = reg_response.get("client_id", "")
    client_secret = reg_response.get("client_secret", "")

    if not client_id:
        raise ValueError("DCR response missing client_id")

    # 保存
    clients[kind] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "registered_at": int(time.time()),
        "registration_response": reg_response,
    }
    section["clients"] = clients
    _save_oauth2_section(section)

    return client_id, client_secret


def get_or_register_client(kind: str, redirect_uri: str) -> tuple[str, str]:
    """获取或注册客户端，返回 (client_id, client_secret)。"""
    return register_client(kind, redirect_uri)


# ── Token 缓存 ─────────────────────────────────────────────────

def get_credentials(kind: str, instance_id: str) -> OAuth2Credentials | None:
    """获取指定实例的 OAuth2 凭据。"""
    section = _get_oauth2_section()
    token_cache = section.get("token_cache", {})
    raw = token_cache.get(instance_id)
    if not raw:
        return None
    return OAuth2Credentials.from_dict(raw)


def save_credentials(instance_id: str, creds: OAuth2Credentials) -> None:
    """保存 OAuth2 凭据到 token_cache。"""
    section = _get_oauth2_section()
    token_cache = section.setdefault("token_cache", {})
    token_cache[instance_id] = creds.to_dict()
    _save_oauth2_section(section)


def delete_credentials(instance_id: str) -> None:
    """删除 OAuth2 凭据。"""
    section = _get_oauth2_section()
    token_cache = section.get("token_cache", {})
    token_cache.pop(instance_id, None)
    _save_oauth2_section(section)


def get_credentials_by_kind(kind: str) -> list[tuple[str, OAuth2Credentials]]:
    """获取指定 kind 的所有凭据（可能有多个实例）。"""
    section = _get_oauth2_section()
    token_cache = section.get("token_cache", {})
    result = []
    for instance_id, raw in token_cache.items():
        if raw.get("kind") == kind:
            result.append((instance_id, OAuth2Credentials.from_dict(raw)))
    return result


# ── 授权 URL 生成 ───────────────────────────────────────────────

def build_authorize_url(
    kind: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    state: str,
    scope: str | None = None,
) -> str:
    """构建 OAuth2 授权 URL。"""
    provider = get_provider(kind)
    if not provider:
        raise ValueError(f"Unknown provider: {kind}")
    if not provider.authorization_endpoint:
        raise ValueError(f"Provider {kind} has no authorization endpoint")

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": generate_code_challenge(code_verifier),
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    elif provider.scopes_supported:
        params["scope"] = " ".join(provider.scopes_supported)

    query = urllib.parse.urlencode(params)
    return f"{provider.authorization_endpoint}?{query}"


# ── Token 交换 ─────────────────────────────────────────────────

async def exchange_code_for_token(
    kind: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None = None,
) -> OAuth2Credentials:
    """用 authorization code 交换 token。"""
    provider = get_provider(kind)
    if not provider:
        raise ValueError(f"Unknown provider: {kind}")
    if not provider.token_endpoint:
        raise ValueError(f"Provider {kind} has no token endpoint")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            provider.token_endpoint,
            data=data,
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        token_resp = resp.json()

    # 解析 token 响应
    access_token = token_resp.get("access_token", "")
    if not access_token:
        raise ValueError("Token response missing access_token")

    creds = OAuth2Credentials(
        kind=kind,
        access_token=access_token,
        refresh_token=token_resp.get("refresh_token"),
        token_type=token_resp.get("token_type", "Bearer"),
        scope=token_resp.get("scope"),
        client_id=client_id,
        client_secret=client_secret,
    )

    # 计算过期时间
    if "expires_in" in token_resp:
        creds.expires_at = int(time.time()) + token_resp["expires_in"]

    return creds


# ── Token 刷新 ─────────────────────────────────────────────────

async def refresh_token(
    kind: str,
    instance_id: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> OAuth2Credentials:
    """刷新 access_token。"""
    creds = get_credentials(kind, instance_id)
    if not creds:
        raise ValueError(f"No credentials found for instance {instance_id}")
    if not creds.refresh_token:
        raise ValueError("No refresh_token available")

    provider = get_provider(kind)
    if not provider or not provider.token_endpoint:
        raise ValueError(f"Provider {kind} has no token endpoint")

    # 使用保存的 client_id/client_secret，或传入的
    cid = client_id or creds.client_id
    csec = client_secret or creds.client_secret

    data = {
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": cid or "",
        "scope": creds.scope or "",
    }
    if csec:
        data["client_secret"] = csec

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            provider.token_endpoint,
            data=data,
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        token_resp = resp.json()

    # 更新凭据
    creds.access_token = token_resp.get("access_token", creds.access_token)
    if "refresh_token" in token_resp:
        creds.refresh_token = token_resp["refresh_token"]
    if "expires_in" in token_resp:
        creds.expires_at = int(time.time()) + token_resp["expires_in"]
    if "token_type" in token_resp:
        creds.token_type = token_resp["token_type"]
    if "scope" in token_resp:
        creds.scope = token_resp["scope"]

    save_credentials(instance_id, creds)
    return creds


# ── 手动 Token 存储 ────────────────────────────────────────────

def save_manual_token(kind: str, token: str, instance_id: str) -> OAuth2Credentials:
    """保存手动粘贴的 token。"""
    creds = OAuth2Credentials(
        kind=kind,
        access_token=token,
        manual_token=token,
        expires_at=None,  # 无过期信息
    )
    save_credentials(instance_id, creds)
    return creds


# ── 自动刷新 ───────────────────────────────────────────────────

async def ensure_valid_token(
    kind: str,
    instance_id: str,
) -> tuple[OAuth2Credentials, str]:
    """确保凭据有效（必要时刷新），返回 (credentials, access_token)。"""
    creds = get_credentials(kind, instance_id)
    if not creds:
        raise ValueError(f"No credentials for instance {instance_id}")

    if creds.is_expired():
        logger.info("Token expired for %s/%s, refreshing...", kind, instance_id)
        creds = await refresh_token(kind, instance_id)

    return creds, creds.access_token


# ── 状态查询 ───────────────────────────────────────────────────

def get_status() -> dict[str, Any]:
    """查询当前 OAuth2 状态。"""
    section = _get_oauth2_section()
    return {
        "pending": list(section.get("auth_states", {}).keys()),
        "clients_registered": list(section.get("clients", {}).keys()),
        "token_cache_count": len(section.get("token_cache", {})),
        "token_cache_kinds": list({
            v.get("kind")
            for v in section.get("token_cache", {}).values()
            if isinstance(v, dict)
        }),
    }


# ── 回调处理 ───────────────────────────────────────────────────

def get_callback_host_from_request(request: Request | None = None) -> str:
    """从 HTTP 请求动态推导回调 host（支持反向代理/远程访问）。

    优先级：
    1. X-Forwarded-Host / X-Forwarded-Proto（反向代理场景）
    2. request.headers["host"]（直连）
    3. config.json 里的 port 字段（兜底 localhost）
    4. 默认 http://localhost:9876
    """
    if request is not None:
        proto = (
            request.headers.get("x-forwarded-proto", "http")
            or request.url.scheme
        )
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or "localhost"
        )
        # 若 host 包含端口则直接用，否则补上端口
        port = request.url.port
        if port and str(port) not in host:
            return f"{proto}://{host}"
        return f"{proto}://{host}"

    # 无 request 时从 config 兜底
    cfg = _read_config()
    port = cfg.get("port")
    if not port:
        # 尝试从 web_server 默认端口推断
        port = 9876
    return f"http://localhost:{port}"


def build_callback_url(request: Request | None = None) -> str:
    """构造完整回调 URL（不含 kind 时用通用路径）。

    优先从 HTTP 请求动态推导 host（支持反向代理/远程访问）；
    无 request 时退化为 config.json 兜底。
    """
    base = get_callback_host_from_request(request)
    path = "/api/v2/connectors/oauth2/callback"
    return f"{base}{path}"


async def handle_callback(
    code: str,
    state: str,
    redirect_uri: str,
) -> tuple[str, str, OAuth2Credentials]:
    """处理 OAuth 回调，返回 (instance_id, kind, credentials)。

    交换 token 后自动创建 connector 实例并保存完整凭据到 token_cache。
    """
    from connector_hub.connectors_service import create_instance

    auth_state = get_auth_state(state)
    if not auth_state:
        raise ValueError("Invalid or expired state parameter")

    kind = auth_state.kind
    code_verifier = auth_state.code_verifier

    # 获取或注册客户端
    client_id, client_secret = get_or_register_client(kind, redirect_uri)

    # 交换 token
    creds = await exchange_code_for_token(
        kind=kind,
        code=code,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
        client_id=client_id,
        client_secret=client_secret,
    )

    # 创建 connector 实例（触发 sync_to_mcp_config 写入 mcp/config.json）
    provider = get_provider(kind)
    name = provider.name_zh if provider else kind
    try:
        inst = create_instance(
            kind=kind,
            name=name,
            creds={"access_token": creds.access_token},
            enabled=True,
        )
        instance_id = inst["instance_id"]
    except Exception:
        # 若实例创建失败（如 kind 未启用），仍保存 token_cache 以便后续使用
        import uuid
        instance_id = str(uuid.uuid4())[:12]

    # 保存完整凭据（含 refresh_token/client_id/expires_at）到 token_cache
    save_credentials(instance_id, creds)

    # 清理 auth state
    remove_auth_state(state)

    return instance_id, kind, creds


def create_auth_url(
    kind: str,
    redirect_uri: str,
) -> tuple[str, str]:
    """创建授权 URL，返回 (auth_url, state)。"""
    # 生成 auth state
    state, auth_state = create_auth_state(kind, redirect_uri)

    # 获取或注册客户端
    client_id, client_secret = get_or_register_client(kind, redirect_uri)

    # 构建授权 URL
    auth_url = build_authorize_url(
        kind=kind,
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_verifier=auth_state.code_verifier,
        state=state,
    )

    return auth_url, state


# ── 同步版刷新（供 _remote_spec_for 等同步调用点使用）────────────

def refresh_token_sync(kind: str, instance_id: str) -> str:
    """同步刷新 access_token 并返回新 token（供同步调用点使用）。"""
    creds = get_credentials(kind, instance_id)
    if not creds or not creds.refresh_token:
        raise ValueError("No refresh_token available")

    provider = get_provider(kind)
    if not provider or not provider.token_endpoint:
        raise ValueError(f"Provider {kind} has no token endpoint")

    data = {
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id or "",
        "scope": creds.scope or "",
    }
    if creds.client_secret:
        data["client_secret"] = creds.client_secret

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    try:
        resp = httpx.post(
            provider.token_endpoint,
            data=data,
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        token_resp = resp.json()
    except Exception as exc:
        logger.error("sync refresh failed for %s/%s: %s", kind, instance_id, exc)
        raise

    creds.access_token = token_resp.get("access_token", creds.access_token)
    if "refresh_token" in token_resp:
        creds.refresh_token = token_resp["refresh_token"]
    if "expires_in" in token_resp:
        creds.expires_at = int(time.time()) + token_resp["expires_in"]
    if "scope" in token_resp:
        creds.scope = token_resp["scope"]

    save_credentials(instance_id, creds)
    return creds.access_token


def get_valid_access_token_sync(kind: str, instance_id: str, fallback: str = "") -> str:
    """同步获取有效 access_token；过期则尝试刷新，失败时回退到 fallback。"""
    creds = get_credentials(kind, instance_id)
    if creds is None:
        return fallback
    if creds.is_expired():
        try:
            return refresh_token_sync(kind, instance_id)
        except Exception:
            logger.warning("refresh failed for %s/%s, falling back to stored token",
                           kind, instance_id)
            return creds.access_token or fallback
    return creds.access_token
