"""连接器实例服务。

职责：
1. 实例持久化（list/create/update/delete/test），存于 config.json 的
   connector_hub.connectors.instances 段（不引入数据库）。
2. 凭据 Fernet 加密（复用 BT 已有的 cryptography 依赖）。
3. 从实例生成 BT 兼容的 MCP server spec，写入 BT 的 mcp/config.json
   （remote/gateway/internal 三模式分别处理），让 BT 既有 MCP 体系能用上。

所有写操作都保持与 BT 现有 mcp/config.json 格式兼容，不破坏既有条目。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid
from typing import Any

from cryptography.fernet import Fernet

from config_store import load_config, save_config, update_config
from connector_hub import PROJECT_ROOT
from connector_hub.connectors_catalog import (
    GATEWAY_KINDS,
    INTERNAL_KINDS,
    get_catalog_entry,
    is_gateway_kind,
)

logger = logging.getLogger("connector-hub-connectors")

CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")
MCP_CONFIG_FILE = os.path.join(PROJECT_ROOT, "mcp", "config.json")
_FERNET_KEY_FILE = os.path.join(PROJECT_ROOT, "connector_hub", ".fernet_key")

# 自托管 internal MCP 端点（BT /mcp 已暴露工具集，这里复用为主服务端口）
# 实际 URL 在 generate_spec 时按 config 的 bind 端口拼接。


def _ensure_fernet_key() -> bytes:
    if os.path.exists(_FERNET_KEY_FILE):
        with open(_FERNET_KEY_FILE, "rb") as fh:
            return fh.read().strip()
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(_FERNET_KEY_FILE), exist_ok=True)
    with open(_FERNET_KEY_FILE, "wb") as fh:
        fh.write(key)
    try:
        os.chmod(_FERNET_KEY_FILE, 0o600)
    except OSError:
        pass
    return key


def _fernet() -> Fernet:
    return Fernet(_ensure_fernet_key())


def encrypt_credentials(payload: dict[str, Any]) -> str:
    """加密凭据 dict，返回 base64 token 字符串。"""
    blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _fernet().encrypt(blob).decode("utf-8")


def decrypt_credentials(token: str) -> dict[str, Any]:
    data = _fernet().decrypt(token.encode("utf-8"))
    obj = json.loads(data.decode("utf-8"))
    return obj if isinstance(obj, dict) else {}


# ── config.json 读写 ────────────────────────────────────────
def _read_config() -> dict[str, Any]:
    return load_config(CONFIG_FILE)


def _write_config(cfg: dict[str, Any]) -> None:
    save_config(CONFIG_FILE, cfg)


def _instances() -> list[dict[str, Any]]:
    cfg = _read_config()
    br = cfg.setdefault("connector_hub", {})
    conn = br.setdefault("connectors", {})
    return conn.setdefault("instances", [])


def _save_instances(instances: list[dict[str, Any]]) -> None:
    def _apply(cfg: dict[str, Any]) -> None:
        cfg.setdefault("connector_hub", {}).setdefault("connectors", {})["instances"] = instances

    update_config(CONFIG_FILE, _apply)


# ── spec 生成（remote / gateway / internal）────────────────
# 地址、传输方式、认证头按各厂商官方 MCP 实现，不再把 token 拼进 URL query。
_MCP_STREAMABLE_HTTP_ACCEPT = "application/json, text/event-stream"


def _mcp_http_headers() -> dict[str, str]:
    return {"Accept": _MCP_STREAMABLE_HTTP_ACCEPT}


def _normalize_transport(raw: Any) -> str:
    """把 catalog 的 remote_transport 归一到客户端实际支持的两值。

    catalog 里未显式声明时是 "raw_http"（占位），而 http_mcp 只实现
    streamable_http / sse；未知值一律按 streamable_http 处理。
    """
    t = str(raw or "").strip().lower()
    return t if t in ("streamable_http", "sse") else "streamable_http"


def normalize_weiyun_mcp_token(raw: str) -> str:
    """从粘贴的 env/WyHeader 片段中提取微云 MCP token。"""
    import re

    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return ""
    m = re.search(r"mcp_token=([^\s;,&\"']+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r"WEIYUN_MCP_TOKEN\s*=\s*['\"]?([^'\"\s]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def _remote_spec_for(
    kind: str, creds: dict[str, Any], entry: Any = None
) -> tuple[str, dict[str, str], str] | None:
    """返回 (url, headers, transport)；None 表示无法构建。"""
    from urllib.parse import quote

    if kind == "tencent-docs":
        token = str(creds.get("token") or "").strip()
        return ("https://docs.qq.com/openapi/mcp", {"Authorization": token}, "streamable_http")

    if kind == "tencent-meeting":
        token = str(creds.get("token") or "").strip()
        return (
            "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1",
            {"X-Tencent-Meeting-Token": token, "X-Skill-Version": "v1.0.1"},
            "streamable_http",
        )

    if kind == "tencent-lexiang":
        token = str(creds.get("api_key") or creds.get("token") or "").strip()
        company = str(creds.get("company_from") or creds.get("client_id") or "").strip()
        url = "https://mcp.lexiang-app.com/mcp?preset=meta"
        if company:
            url = (
                "https://mcp.lexiang-app.com/mcp"
                f"?company_from={quote(company, safe='')}&preset=meta"
            )
        headers = {**_mcp_http_headers(), "Authorization": f"Bearer {token}"}
        return (url, headers, "streamable_http")

    if kind == "tencent-weiyun":
        raw = str(creds.get("token") or creds.get("access_token") or "").strip()
        token = normalize_weiyun_mcp_token(raw)
        headers = {**_mcp_http_headers(), "WyHeader": f"mcp_token={token}"}
        return ("https://www.weiyun.com/api/v3/mcpserver", headers, "streamable_http")

    if kind == "didi":
        api_key = str(creds.get("api_key") or "").strip()
        url = f"https://mcp.didichuxing.com/mcp-servers?key={quote(api_key, safe='')}"
        return (url, _mcp_http_headers(), "streamable_http")

    if kind == "youdao-note":
        api_key = str(
            creds.get("token") or creds.get("api_key") or creds.get("access_token") or ""
        ).strip()
        return (
            "https://open.mail.163.com/api/ynote/mcp/sse",
            {"x-api-key": api_key},
            "sse",
        )

    # OAuth2 类 remote（notion / openalex / dida365 / tencent-ardot）
    # 需要 access_token + catalog 的 mcp_url；未完成 OAuth 流程时不可用。
    # 若实例关联的 token_cache 存在且 token 过期，先尝试自动刷新再返回。
    if getattr(entry, "auth_kind", "") == "oauth2":
        mcp_url = str(getattr(entry, "mcp_url", "") or "").strip()
        if not mcp_url:
            return None
        access_token = str(creds.get("access_token") or creds.get("token") or "").strip()
        if not access_token:
            return None
        # 自动刷新：从 oauth_manager 的 token_cache 取有效 token
        # （instance_id 在调用方通过 instance 传入时由调用方拼接，这里尽力推断）
        from connector_hub import oauth_manager as oauth
        instance_id = creds.get("instance_id") or creds.get("_instance_id") or ""
        if instance_id and oauth.get_provider(kind):
            try:
                access_token = oauth.get_valid_access_token_sync(kind, instance_id, fallback=access_token)
            except Exception:
                logger.warning("OAuth2 auto-refresh failed for %s/%s; using stored token",
                              kind, instance_id)
                access_token = access_token
        headers = {**_mcp_http_headers(), "Authorization": f"Bearer {access_token}"}
        user_agent = str(getattr(entry, "mcp_user_agent", "") or "").strip()
        if user_agent:
            headers["User-Agent"] = user_agent
        transport = _normalize_transport(getattr(entry, "remote_transport", None))
        return (mcp_url, headers, transport)

    # 自定义 MCP（dify / 自定义 url）：用户直接提供 mcp_url
    custom_url = str(creds.get("mcp_url") or creds.get("url") or "").strip()
    if custom_url:
        api_key = str(creds.get("api_key") or creds.get("token") or "").strip()
        headers = dict(_mcp_http_headers())
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        transport = _normalize_transport(getattr(entry, "remote_transport", None))
        return (custom_url, headers, transport)

    return None


def _remote_url_for(kind: str, creds: dict[str, Any]) -> str | None:
    """兼容旧调用：仅返回 URL（不含 token）。"""
    entry = get_catalog_entry(kind)
    spec = _remote_spec_for(kind, creds, entry)
    return spec[0] if spec else None


def generate_mcp_spec(instance: dict[str, Any]) -> dict[str, Any] | None:
    """从连接器实例生成 BT mcp/config.json 的一个 mcpServers 条目。

    返回 None 表示该实例当前无法生成 spec（如 OAuth2 未实现）。
    结构对齐 BT mcp/config.json 的 {"type":"http","url":...} 格式。
    """
    kind = instance["kind"]
    entry = get_catalog_entry(kind)
    if entry is None:
        return None
    creds = decrypt_credentials(instance.get("creds_enc", "")) if instance.get("creds_enc") else {}
    if entry.mcp_mode == "remote":
        # OAuth2 实例需要把 instance_id 带给 _remote_spec_for，用于自动刷新
        if getattr(entry, "auth_kind", "") == "oauth2" and creds:
            creds["_instance_id"] = instance["instance_id"]
        spec3 = _remote_spec_for(kind, creds, entry)
        if not spec3:
            return None
        url, headers, transport = spec3
        spec: dict[str, Any] = {"type": "http", "url": url, "headers": headers}
        if transport == "streamable_http":
            spec["transport"] = "streamable_http"
        elif transport == "sse":
            spec["transport"] = "sse"
        return spec
    if entry.mcp_mode == "gateway" or kind in GATEWAY_KINDS:
        internal_token = creds.get("internal_token") or ""
        if not internal_token:
            return None
        return {
            "type": "http",
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:9876/api/v2/connectors/internal/mcp/{kind}/{instance['instance_id']}?token={internal_token}",
        }
    if kind in INTERNAL_KINDS:
        return {"type": "internal", "kind": kind}
    return None


# ── 同步 mcp/config.json ───────────────────────────────────
def _server_name(instance: dict[str, Any]) -> str:
    return f"chub__{instance['kind']}__{instance['instance_id']}"


def sync_to_mcp_config(dry_run: bool = False) -> dict[str, Any]:
    """把 enabled 的连接器实例同步进 BT mcp/config.json。

    只增删本桥管理的 chub__ 前缀条目，不动其它条目。
    返回 {added, removed, kept} 统计。
    """
    instances = _instances()
    mcp_cfg = load_config(MCP_CONFIG_FILE)
    servers = mcp_cfg.setdefault("mcpServers", {})

    desired: dict[str, dict[str, Any]] = {}
    for inst in instances:
        if not inst.get("enabled", True):
            continue
        spec = generate_mcp_spec(inst)
        if spec is None:
            continue
        desired[_server_name(inst)] = spec

    existing_hub = {k: v for k, v in servers.items() if k.startswith("chub__")}
    added = [k for k in desired if k not in existing_hub]
    removed = [k for k in existing_hub if k not in desired]
    kept = [k for k in desired if k in existing_hub]

    if not dry_run:
        for k in removed:
            servers.pop(k, None)
        servers.update(desired)
        save_config(MCP_CONFIG_FILE, mcp_cfg)
    return {"added": added, "removed": removed, "kept": kept}


# ── CRUD ───────────────────────────────────────────────────
def list_instances(include_secrets: bool = False) -> list[dict[str, Any]]:
    out = []
    for inst in _instances():
        d = _redact(inst, include_secrets)
        out.append(d)
    return out


def get_instance(instance_id: str, include_secrets: bool = False) -> dict[str, Any] | None:
    for inst in _instances():
        if inst["instance_id"] == instance_id:
            return _redact(inst, include_secrets)
    return None


def normalize_credentials(kind: str, entry: Any, creds: dict[str, Any]) -> dict[str, Any]:
    """按 auth_kind 归一化并校验凭据字段。

    不同连接器的凭据键名
    不同（token / api_key / email+password），前端在无 credential_fields
    时可能只提交 ``token``，这里统一映射成各适配器实际读取的键名。
    """
    creds = dict(creds or {})
    auth = getattr(entry, "auth_kind", "") or ""

    if auth == "personal_token":
        token = str(creds.get("token") or creds.get("access_token") or "").strip()
        if not token:
            raise ValueError("请填写 Token")
        if kind == "tencent-weiyun":
            token = normalize_weiyun_mcp_token(token)
        return {"token": token}

    if auth == "api_key":
        # 兼容前端兜底提交的 token 字段
        api_key = str(creds.get("api_key") or creds.get("token") or "").strip()
        if not api_key:
            raise ValueError("请填写 API Key")
        # 前缀格式校验由各 gateway 适配器在 probe/调用时执行（qq-music qmk-、
        # wechat-reading wrk-、yuandian sk_），创建时只做非空校验。
        out: dict[str, Any] = {"api_key": api_key}
        if kind == "tencent-ima":
            client_id = str(creds.get("client_id") or "").strip()
            if not client_id:
                raise ValueError("IMA 需要填写 client_id")
            out["client_id"] = client_id
        if kind == "tencent-lexiang":
            company = str(creds.get("company_from") or creds.get("client_id") or "").strip()
            if not company:
                raise ValueError("乐享需要填写 company_from")
            out["company_from"] = company
        return out

    if auth == "imap_app_password":
        email = str(creds.get("email") or "").strip()
        password = str(creds.get("password") or creds.get("token") or "").strip()
        if not email or not password:
            raise ValueError("请填写邮箱与授权码")
        out = {"email": email, "password": password}
        provider = str(creds.get("mail_provider") or "").strip().lower()
        if provider:
            out["mail_provider"] = provider
        return out

    if auth == "oauth2":
        # 支持两种来源：1) 已完成 OAuth 流程自动带 access_token；
        #              2) 手动粘贴的 manual_token（无标准 OAuth 发现的兜底）。
        access_token = str(
            creds.get("access_token") or creds.get("token") or creds.get("manual_token") or ""
        ).strip()
        if not access_token:
            raise ValueError("OAuth2 连接器需先完成授权，或手动粘贴 Token")
        return {
            "access_token": access_token,
            "manual_token": access_token if creds.get("manual_token") else None,
        }

    if auth == "custom_fields":
        # 原样保留（dify / weknora / obsidian-cli 等）
        return creds

    return creds


def create_instance(kind: str, name: str, creds: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    entry = get_catalog_entry(kind)
    if entry is None:
        raise ValueError(f"unknown connector kind: {kind}")
    if not entry.supported:
        raise ValueError(f"connector kind {kind} 当前未实现（auth_kind={entry.auth_kind}）")
    creds = normalize_credentials(kind, entry, creds)
    if (kind in GATEWAY_KINDS or (entry.mcp_mode == "gateway")) and not creds.get("internal_token"):
        creds["internal_token"] = secrets.token_urlsafe(32)
    inst = {
        "instance_id": uuid.uuid4().hex[:16],
        "kind": kind,
        "name": name,
        "enabled": enabled,
        "created_at": _now(),
        "creds_enc": encrypt_credentials(creds) if creds else "",
    }
    instances = _instances()
    instances.append(inst)
    _save_instances(instances)
    sync_to_mcp_config()
    return _redact(inst, False)


def update_instance(instance_id: str, *, name: str | None = None,
                    creds: dict[str, Any] | None = None,
                    enabled: bool | None = None) -> dict[str, Any] | None:
    instances = _instances()
    for inst in instances:
        if inst["instance_id"] == instance_id:
            if name is not None:
                inst["name"] = name
            if creds is not None:
                entry = get_catalog_entry(inst["kind"])
                # 编辑场景：前端 secret 留空表示不修改。
                # 以旧凭据为底，用新提交的非空字段覆盖，这样未修改的 secret 得以保留。
                old = decrypt_credentials(inst.get("creds_enc", "")) if inst.get("creds_enc") else {}
                merged = dict(old)
                merged.update(creds)
                if entry is not None and merged:
                    creds = normalize_credentials(inst["kind"], entry, merged)
                else:
                    creds = merged
                # 保留已有 internal_token（gateway 连接器更新凭据时不重置）
                if old.get("internal_token") and not creds.get("internal_token"):
                    creds["internal_token"] = old["internal_token"]
                elif (
                    inst["kind"] in GATEWAY_KINDS
                    or (entry is not None and entry.mcp_mode == "gateway")
                ) and not creds.get("internal_token"):
                    creds["internal_token"] = secrets.token_urlsafe(32)
                inst["creds_enc"] = encrypt_credentials(creds)
            if enabled is not None:
                inst["enabled"] = enabled
            inst["updated_at"] = _now()
            _save_instances(instances)
            sync_to_mcp_config()
            return _redact(inst, False)
    return None


def delete_instance(instance_id: str) -> bool:
    instances = _instances()
    new = [i for i in instances if i["instance_id"] != instance_id]
    if len(new) == len(instances):
        return False
    _save_instances(new)
    sync_to_mcp_config()
    return True


def verify_internal_token(instance_id: str, token: str) -> dict[str, Any] | None:
    """校验连接器实例的 internal_token，返回解密后的凭据或 None。"""
    for inst in _instances():
        if inst["instance_id"] == instance_id:
            creds = decrypt_credentials(inst.get("creds_enc", "")) if inst.get("creds_enc") else {}
            if creds.get("internal_token") == token:
                return creds
            return None
    return None


def get_oauth2_instance(instance_id: str) -> dict[str, Any] | None:
    """返回指定 instance_id 的 OAuth2 凭据 + 实例信息。

    与 get_instance 的区别：返回的 dict 额外带 oauth2 相关字段
    （access_token 明文、refresh_token、expires_at），供 API 层使用。
    """
    from connector_hub import oauth_manager as oauth

    inst = get_instance(instance_id, include_secrets=True)
    if not inst:
        return None
    # 附加 OAuth2 凭据
    try:
        creds = oauth.get_credentials(inst["kind"], instance_id)
        if creds:
            inst["oauth2"] = creds.to_dict()
    except Exception:
        pass
    return inst


def test_instance(instance_id: str) -> dict[str, Any]:
    """探测实例凭据是否可用（gateway 走适配器 probe；remote 走 HTTP MCP list_tools）。"""
    inst = None
    for i in _instances():
        if i["instance_id"] == instance_id:
            inst = i
            break
    if inst is None:
        return {"ok": False, "error": "instance not found"}
    kind = inst["kind"]
    creds = decrypt_credentials(inst.get("creds_enc", "")) if inst.get("creds_enc") else {}
    entry = get_catalog_entry(kind)
    try:
        if entry and entry.mcp_mode == "remote":
            from connector_hub.http_mcp import amap_list_tools
            import asyncio
            spec3 = _remote_spec_for(kind, creds, entry)
            if not spec3:
                return {"ok": False, "error": "无法构建连接配置（缺少必要凭据）"}
            url, headers, transport = spec3
            tools = asyncio.run(
                amap_list_tools(url, None, transport, headers=headers)
            )
            return {"ok": True, "tool_count": len(tools)}
        if kind in GATEWAY_KINDS:
            from connector_hub.connectors_gateway import probe_gateway_credentials
            probe_gateway_credentials(kind, creds)
            return {"ok": True}
        return {"ok": False, "error": f"unsupported mcp_mode for probe: {entry.mcp_mode if entry else '?'}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("test_instance %s failed", instance_id)
        return {"ok": False, "error": str(exc)}


def _redact(inst: dict[str, Any], include_secrets: bool = False) -> dict[str, Any]:
    d = dict(inst)
    creds: dict[str, Any] = {}
    if include_secrets:
        if inst.get("creds_enc"):
            creds = decrypt_credentials(inst["creds_enc"])
            d["creds"] = creds
    else:
        # 不泄露明文，但标记哪些 secret 字段已配置（供前端「留空表示不修改」）
        if inst.get("creds_enc"):
            try:
                creds = decrypt_credentials(inst["creds_enc"])
            except Exception:  # noqa: BLE001
                creds = {}
    d.pop("creds_enc", None)
    # 计算已配置的 secret 字段名
    configured: list[str] = []
    entry = get_catalog_entry(inst.get("kind", ""))
    if entry is not None:
        for f in entry.credential_fields:
            if getattr(f, "secret", False) and creds.get(f.key):
                configured.append(f.key)
    d["configured_fields"] = configured
    return d


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
