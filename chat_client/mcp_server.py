"""
BT 工具 MCP server：将 ToolRegistry 中的全部自研工具暴露为 MCP 工具。

支持两种模式：
- stdio 模式：opencode 通过 stdio 直接拉起（默认）
- SSE 模式：通过 HTTP SSE 远程访问（--sse --port 8976）

设计要点：
- 工具执行统一走 registry 的 wrapper（内部已含 _guarded_execute 五道护栏）
- 特殊工具上下文通过环境变量注入

用法：
    # stdio 模式（默认）
    python3 chat_client/mcp_server.py

    # SSE 远程模式
    python3 chat_client/mcp_server.py --sse --port 8976
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from mcp.server import Server
from mcp import types
from mcp.types import Tool, TextContent, ListToolsResult, CallToolResult

# 确保能从项目根导入 chat_client
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from chat_client.tools import registry  # noqa: E402  触发所有工具注册

logger = logging.getLogger("mcp_server")

# ── 环境变量配置 ──────────────────────────────────────────────────────────

# 允许通过环境变量过滤暴露的工具 ID（逗号分隔），空 = 全量
_ENABLED_TOOLS_RAW = os.environ.get("BT_ENABLED_TOOLS", "").strip()

# 特殊工具上下文注入（由 bridge 传入，缺失时留空）
_SESSION_ID = os.environ.get("BT_SESSION_ID", "")
_SESSIONS_DIR = os.environ.get("BT_SESSIONS_DIR", "sessions")
_PARENT_CONFIG_RAW = os.environ.get("BT_PARENT_CONFIG", "")
_PARENT_CONFIG = {}
if _PARENT_CONFIG_RAW:
    try:
        _PARENT_CONFIG = json.loads(_PARENT_CONFIG_RAW)
    except Exception:
        _PARENT_CONFIG = {}

# 需要注入 session 上下文的工具集合
_SESSION_CONTEXT_TOOLS = {"TodoWrite", "TodoRead", "TaskSummary"}
# 需要注入 parent 上下文的编排工具集合
_PARENT_CONTEXT_TOOLS = {"Task", "RunCrew", "ConsultPeer"}

# ── MCP Server 实例 ──────────────────────────────────────────────────────

server = Server("agent-tools")


def _resolve_enabled() -> set[str] | None:
    """解析 BT_ENABLED_TOOLS 环境变量为工具 ID 集合；空字符串返回 None（全量）"""
    if not _ENABLED_TOOLS_RAW:
        return None
    return {t.strip() for t in _ENABLED_TOOLS_RAW.split(",") if t.strip()}


def _schema_to_input_schema(params: dict[str, Any]) -> dict[str, Any]:
    """OpenAI parameters 对象 → MCP inputSchema"""
    out = {"type": "object", "properties": {}}
    for name, spec in (params.get("properties") or {}).items():
        out["properties"][name] = spec
    if params.get("required"):
        out["required"] = params["required"]
    return out


def _collect_tools() -> list[Tool]:
    enabled = _resolve_enabled()
    tools: list[Tool] = []
    for name, meta in registry._metadata.items():
        if not meta.get("show", True):
            continue
        if name == "Skills":
            continue
        if enabled is not None and meta.get("id") not in enabled:
            continue
        schema = next((s for s in registry._schemas if s["function"]["name"] == name), None)
        if not schema:
            continue
        input_schema = _schema_to_input_schema(schema["function"].get("parameters", {}))
        tools.append(
            Tool(
                name=name,
                description=meta.get("description") or f"BT 工具: {name}",
                inputSchema=input_schema,
            )
        )
    return tools


def _inject_context(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """为需要上下文的工具注入 parent/session 兜底参数"""
    args = dict(arguments or {})
    if name in _SESSION_CONTEXT_TOOLS:
        args.setdefault("session_id", _SESSION_ID)
        args.setdefault("sessions_dir", _SESSIONS_DIR)
    if name in _PARENT_CONTEXT_TOOLS and _PARENT_CONFIG:
        args.setdefault("parent_config", _PARENT_CONFIG)
        args.setdefault("parent_session_id", _SESSION_ID)
    return args


# ── MCP 请求处理器 ──────────────────────────────────────────────────────

async def handle_list_tools(_ctx, _params=None):
    return ListToolsResult(tools=_collect_tools())


async def handle_call_tool(_ctx, params):
    name = params.name
    arguments = dict(params.arguments) if params.arguments else {}
    arguments = _inject_context(name, arguments)

    func = registry.get_tool_func(name)
    if func is None:
        return CallToolResult(
            content=[TextContent(type="text", text=f"[MCP 工具 {name} 不存在]")],
            isError=True,
        )

    def _run():
        return func(**arguments)

    try:
        result = await asyncio.get_running_loop().run_in_executor(None, _run)
        return CallToolResult(content=[TextContent(type="text", text=str(result))])
    except Exception as e:
        logger.exception("工具 %s 执行异常", name)
        return CallToolResult(
            content=[TextContent(type="text", text=f"[MCP 工具 {name} 执行异常]: {e}")],
            isError=True,
        )


server.add_request_handler("tools/list", types.PaginatedRequestParams, handle_list_tools)
server.add_request_handler("tools/call", types.CallToolRequestParams, handle_call_tool)


# ── SSE 模式（FastAPI HTTP 服务器）──────────────────────────────────────

def create_sse_app(host: str = "0.0.0.0", port: int = 8976):
    """创建 FastAPI SSE 服务器，通过 HTTP 暴露 MCP 工具"""
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse, JSONResponse
    import uvicorn

    app = FastAPI(title="Multi-agent MCP Server")

    # 添加 CORS 支持
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 存储每个连接的 session
    sessions: dict[str, Any] = {}

    @app.get("/health")
    async def health():
        return {"status": "ok", "server": "agent-tools"}

    @app.get("/tools")
    async def list_tools():
        """列出所有可用工具"""
        tools = _collect_tools()
        return {
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ]
        }

    @app.post("/tools/{tool_name}/call")
    async def call_tool(tool_name: str, request: Request):
        """调用指定工具"""
        body = await request.json()
        arguments = body.get("arguments", {})

        func = registry.get_tool_func(tool_name)
        if func is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"工具 {tool_name} 不存在"}
            )

        arguments = _inject_context(tool_name, arguments)

        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, lambda: func(**arguments)
            )
            return {"result": str(result)}
        except Exception as e:
            logger.exception("工具 %s 执行异常", tool_name)
            return JSONResponse(
                status_code=500,
                content={"error": f"执行异常: {e}"}
            )

    @app.get("/sse")
    async def sse_endpoint(request: Request):
        """SSE 端点：用于 MCP 协议通信"""
        import uuid
        from mcp.server.sse import SseServerTransport

        session_id = str(uuid.uuid4())
        sse = SseServerTransport("/messages/")

        async def event_generator():
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                session = await server.create_session(streams[0], streams[1])
                sessions[session_id] = session

                # 保持连接
                try:
                    while True:
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    pass
                finally:
                    sessions.pop(session_id, None)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/messages/")
    async def messages_endpoint(request: Request):
        """消息端点：接收 JSON-RPC 请求"""
        body = await request.json()
        method = body.get("method", "")
        params = body.get("params", {})
        msg_id = body.get("id")

        # 根据方法分发
        if method == "tools/list":
            tools = _collect_tools()
            result = {"tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ]}
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            arguments = _inject_context(tool_name, arguments)

            func = registry.get_tool_func(tool_name)
            if func is None:
                result = {"error": {"code": -32601, "message": f"工具 {tool_name} 不存在"}}
            else:
                try:
                    result_text = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: func(**arguments)
                    )
                    result = {
                        "content": [{"type": "text", "text": str(result_text)}]
                    }
                except Exception as e:
                    result = {"error": {"code": -32603, "message": str(e)}}
        elif method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agent-tools", "version": "1.0.0"},
            }
        elif method == "notifications/initialized":
            # 客户端确认初始化完成
            return JSONResponse({"ok": True})
        else:
            result = {"error": {"code": -32601, "message": f"未知方法: {method}"}}

        response = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": result,
        }
        return JSONResponse(response)

    return app


# ── 主入口 ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-agent MCP Server")
    parser.add_argument("--sse", action="store_true", help="启用 SSE 远程模式")
    parser.add_argument("--port", type=int, default=8976, help="SSE 服务器端口（默认 8976）")
    parser.add_argument("--host", default="0.0.0.0", help="SSE 服务器绑定地址（默认 0.0.0.0）")
    args = parser.parse_args()

    if args.sse:
        # SSE 远程模式
        logging.basicConfig(level=logging.INFO, format="[mcp] %(message)s")
        import uvicorn
        app = create_sse_app(args.host, args.port)
        logger.info("Multi-agent MCP SSE 服务器启动 http://%s:%d", args.host, args.port)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        # stdio 模式（默认）
        logging.basicConfig(level=logging.WARNING, format="[mcp] %(message)s", stream=sys.stderr)
        asyncio.run(_run_stdio())


async def _run_stdio():
    """stdio 模式主循环"""
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    main()
