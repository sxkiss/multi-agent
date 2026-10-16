"""HTTP / SSE MCP 客户端（补齐 BT mcp_client 仅支持 stdio 的缺口）。

Octop 的 remote 模式连接器（tencent-docs、didi、dify 等）都是远程 HTTP/SSE
MCP 服务。BT 现有 chat_client/mcp_client.py 在 `_connect_server` 中显式跳过
HTTP/SSE（注释"当前仅支持 stdio"）。本模块新增一个轻量异步 HTTP MCP 客户端，
复用 MCP 2024-11-05 JSON-RPC 协议，支持：

- streamable_http：POST 单端点，Accept 同时含 application/json 与
  text/event-stream，响应可能是 JSON 或 SSE 流（按 Content-Type 分派）。
- sse：GET 长连接收事件，POST 发请求（带 SSE session id）。

本模块**不改**现有 stdio 逻辑，仅作为 remote 连接器的执行后端。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx

logger = logging.getLogger("octop-bridge-http-mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
_STREAMABLE_ACCEPT = "application/json, text/event-stream"
_SSE_ACCEPT = "text/event-stream"

# 远程调用超时（秒）：initialize 等握手短，tools/call 给足时间
_TIMEOUT_HANDSHAKE = 20.0
_TIMEOUT_CALL = 120.0


class HttpMcpError(RuntimeError):
    """HTTP MCP 调用层错误。"""


class HttpMcpClient:
    """一个远程 MCP 端点的有状态会话客户端。

    用法：
        async with HttpMcpClient(url, token=..., transport="streamable_http") as c:
            tools = await c.list_tools()
            res = await c.call_tool("foo", {"x": 1})
    """

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        transport: str = "streamable_http",
        headers: dict[str, str] | None = None,
        timeout: float = _TIMEOUT_CALL,
        verify: bool = True,
    ) -> None:
        self.url = url.rstrip("/")
        self.transport = transport
        self._token = token
        self._verify = verify
        self._timeout = timeout
        self._session_id: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._tools_cache: list[dict[str, Any]] | None = None

    # ── 生命周期 ──────────────────────────────────────────────
    async def __aenter__(self) -> "HttpMcpClient":
        base = {
            "User-Agent": "bt-octop-bridge/1.0",
            "Accept": _STREAMABLE_ACCEPT if self.transport == "streamable_http" else _SSE_ACCEPT,
        }
        if self._token:
            base["Authorization"] = f"Bearer {self._token}"
        if headers:
            base.update(headers)
        self._client = httpx.AsyncClient(
            base_url=self.url,
            headers=base,
            timeout=self._timeout,
            verify=self._verify,
        )
        await self.initialize()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    # ── 底层 JSON-RPC ─────────────────────────────────────────
    async def _rpc(self, method: str, params: dict[str, Any] | None = None, *, handshake: bool = False) -> dict[str, Any]:
        if self._client is None:
            raise HttpMcpError("client not connected; use async with")
        msg_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        to = _TIMEOUT_HANDSHAKE if handshake else self._timeout
        # SSE 传输用 GET 接收通道 + POST 发送；streamable 用 POST 同端点
        resp = await self._client.post("/", json=payload, timeout=to)
        ctype = resp.headers.get("content-type", "")
        text = resp.text
        if "text/event-stream" in ctype:
            # 解析最后一条 data: 行作为 JSON-RPC 响应
            data = _last_sse_data(text)
            if data is None:
                raise HttpMcpError(f"SSE 响应无 data 帧: {text[:200]}")
            body = json.loads(data)
            # SSE 端点可能回带 Mcp-Session-Id，保存供后续请求
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
        else:
            body = resp.json()
        if "error" in body:
            raise HttpMcpError(f"MCP error {body['error']}")
        return body.get("result", {})

    # ── 高层方法 ──────────────────────────────────────────────
    async def initialize(self) -> dict[str, Any]:
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "bt-octop-bridge", "version": "1.0.0"},
            },
            handshake=True,
        )
        # 发送 initialized 通知
        try:
            await self._client.post(
                "/",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                timeout=_TIMEOUT_HANDSHAKE,
            )
        except Exception:  # noqa: BLE001
            pass
        return result

    async def list_tools(self) -> list[dict[str, Any]]:
        if self._tools_cache is None:
            res = await self._rpc("tools/list")
            self._tools_cache = res.get("tools", [])
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        res = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        return _extract_tool_text(res)


def _extract_tool_text(result: dict[str, Any]) -> str:
    """从 tools/call 的 result 中抽取纯文本（兼容 content 数组 / 结构化）。"""
    if not isinstance(result, dict):
        return str(result)
    content = result.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif "data" in item:
                    parts.append(json.dumps(item["data"], ensure_ascii=False))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(p for p in parts if p)
    if "content" in result and isinstance(result["content"], str):
        return result["content"]
    return json.dumps(result, ensure_ascii=False)


def _last_sse_data(text: str) -> str | None:
    """从 SSE 文本中取最后一条 data: 行。"""
    last = None
    for line in text.splitlines():
        if line.startswith("data:"):
            last = line[len("data:") :].strip()
    return last


# ── 便捷同步包装（供 service 层在线程池 / 同步路径调用）──────────
async def amap_call(url: str, token: str | None, name: str, args: dict[str, Any], transport: str = "streamable_http") -> str:
    async with HttpMcpClient(url, token=token, transport=transport) as c:
        return await c.call_tool(name, args)


async def amap_list_tools(url: str, token: str | None, transport: str = "streamable_http") -> list[dict[str, Any]]:
    async with HttpMcpClient(url, token=token, transport=transport) as c:
        return await c.list_tools()
