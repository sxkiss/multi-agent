"""Gateway 适配器注册表。

kind -> adapter（list_tools / call_tool / probe_credentials）。
各厂商 MCP 适配器（纯 httpx 实现；*_cli 类需主机安装外部 CLI 二进制）。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from connector_hub.gateway_adapters import (
    agently_cli,
    baidu_map,
    ctrip_wendao,
    feishu_cli,
    fliggy,
    meituan_travel,
    obsidian_cli,
    qq_mail,
    qq_music,
    tencent_ima,
    tencent_news,
    wechat_reading,
    wecom_cli,
    weknora,
    yuandian,
)

logger = logging.getLogger("connector-hub-gateway")


class GatewayAdapter(Protocol):
    def list_tools(self) -> list[dict[str, Any]]: ...
    def call_tool(self, creds: dict[str, Any], name: str, args: dict[str, Any]) -> str: ...
    def probe_credentials(self, creds: dict[str, Any]) -> None: ...


_ADAPTERS: dict[str, GatewayAdapter] = {
    "agently-cli": agently_cli,
    "qq-mail": qq_mail,
    "qq-music": qq_music,
    "fliggy": fliggy,
    "baidu-map": baidu_map,
    "ctrip-wendao": ctrip_wendao,
    "meituan-travel": meituan_travel,
    "yuandian": yuandian,
    "tencent-ima": tencent_ima,
    "tencent-news": tencent_news,
    "wechat-reading": wechat_reading,
    "feishu-cli": feishu_cli,
    "wecom-cli": wecom_cli,
    "obsidian-cli": obsidian_cli,
    "weknora": weknora,
}

# 需要主机安装外部 CLI 二进制的 kind（无二进制时 list_tools 仍可用，call 才报错）
EXTERNAL_CLI_KINDS = frozenset({"agently-cli", "feishu-cli", "wecom-cli", "obsidian-cli"})


def get_gateway_adapter(kind: str) -> GatewayAdapter | None:
    return _ADAPTERS.get(kind)


def mcp_tools_for_kind(kind: str) -> list[dict[str, Any]]:
    adapter = get_gateway_adapter(kind)
    return adapter.list_tools() if adapter else []


def call_gateway_tool(kind: str, creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    adapter = get_gateway_adapter(kind)
    if adapter is None:
        raise ValueError(f"unknown gateway connector kind: {kind}")
    return adapter.call_tool(creds, name, args)


def probe_gateway_credentials(kind: str, creds: dict[str, Any]) -> None:
    adapter = get_gateway_adapter(kind)
    if adapter is None:
        raise ValueError(f"unknown gateway connector kind: {kind}")
    adapter.probe_credentials(creds)


def list_gateway_kinds() -> list[str]:
    return sorted(_ADAPTERS.keys())


MCP_PROTOCOL_VERSION = "2024-11-05"


def handle_mcp_request(*, kind: str, creds: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params") or {}
    if method == "initialize":
        return _ok(req_id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": f"bt-{kind}", "version": "1.0.0"},
        })
    if method == "notifications/initialized":
        return {}
    if method == "tools/list":
        return _ok(req_id, {"tools": mcp_tools_for_kind(kind)})
    if method == "tools/call":
        name = (params.get("name") or "") if isinstance(params, dict) else ""
        arguments = (params.get("arguments") or {}) if isinstance(params, dict) else {}
        if isinstance(arguments, dict) and set(arguments.keys()) == {"kwargs"}:
            nested = arguments.get("kwargs")
            if isinstance(nested, dict):
                arguments = nested
        try:
            text = call_gateway_tool(kind, creds, str(name), arguments)
            return _ok(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001
            logger.exception("gateway tool %s failed", name)
            return _ok(req_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
    if method == "ping":
        return _ok(req_id, {})
    return _err(req_id, -32601, f"Method not found: {method}")


def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    if req_id is None:
        return {}
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
