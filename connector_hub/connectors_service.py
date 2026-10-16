"""连接器实例服务 —— 移植自 Octop connectors.service + crypto。

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
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return {}


def _write_config(cfg: dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def _instances() -> list[dict[str, Any]]:
    cfg = _read_config()
    br = cfg.setdefault("connector_hub", {})
    conn = br.setdefault("connectors", {})
    return conn.setdefault("instances", [])


def _save_instances(instances: list[dict[str, Any]]) -> None:
    cfg = _read_config()
    cfg.setdefault("connector_hub", {}).setdefault("connectors", {})["instances"] = instances
    _write_config(cfg)


# ── spec 生成（remote / gateway / internal）────────────────
_REMOTE_BASE_URLS: dict[str, str] = {
    # kind -> 远程 MCP 基础 URL 模板（token 通过 header/query 注入）
    "tencent-docs": "https://docs.qq.com/open/mcp",
    "tencent-meeting": "https://meeting.tencent.com/ai-skill/mcp",
    "tencent-lexiang": "https://lexiangla.com/mcp",
    "tencent-weiyun": "https://www.weiyun.com/act/openclaw/mcp",
    "didi": "https://mcp.didichuxing.com/mcp-servers",
    "tencent-ardot": None,  # OAuth2，暂未实现
    "youdao-note": None,
    "notion": None,
    "openalex": None,
    "dida365": None,
    "qcc": None,
}


def _remote_url_for(kind: str, creds: dict[str, Any]) -> str | None:
    """拼出远程 MCP URL，并注入 token（personal_token 走 query）。"""
    base = _REMOTE_BASE_URLS.get(kind)
    if base is None:
        # custom_fields / 用户直接给 mcp_url（如 dify）
        return creds.get("mcp_url") or creds.get("url")
    token = creds.get("token") or creds.get("api_key") or creds.get("personal_token")
    if not token:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}token={token}"


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
        url = _remote_url_for(kind, creds)
        if not url:
            return None
        transport = entry.remote_transport
        spec: dict[str, Any] = {"type": "http", "url": url}
        if transport == "streamable_http":
            spec["transport"] = "streamable_http"
        return spec
    if entry.mcp_mode == "gateway" or kind in GATEWAY_KINDS:
        # in-process gateway：标记的条目，由 connectors_gateway 运行时解析
        return {"type": "gateway", "kind": kind}
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
    mcp_cfg = {}
    if os.path.exists(MCP_CONFIG_FILE):
        try:
            with open(MCP_CONFIG_FILE, "r", encoding="utf-8") as fh:
                mcp_cfg = json.load(fh)
        except json.JSONDecodeError:
            mcp_cfg = {}
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
        os.makedirs(os.path.dirname(MCP_CONFIG_FILE), exist_ok=True)
        with open(MCP_CONFIG_FILE, "w", encoding="utf-8") as fh:
            json.dump(mcp_cfg, fh, ensure_ascii=False, indent=2)
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


def create_instance(kind: str, name: str, creds: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    entry = get_catalog_entry(kind)
    if entry is None:
        raise ValueError(f"unknown connector kind: {kind}")
    if not entry.supported:
        raise ValueError(f"connector kind {kind} 当前未实现（auth_kind={entry.auth_kind}）")
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
            url = _remote_url_for(kind, creds)
            token = creds.get("token") or creds.get("api_key")
            tools = asyncio.run(amap_list_tools(url, token, entry.remote_transport))
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
    if include_secrets:
        if inst.get("creds_enc"):
            d["creds"] = decrypt_credentials(inst["creds_enc"])
    d.pop("creds_enc", None)
    return d


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()
