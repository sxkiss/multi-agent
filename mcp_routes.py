"""
Multi-agent MCP 服务器模块（唯一权威实现）。

将 BT 工具集暴露为 MCP 远程服务器端点，复用主服务端口，提供标准 MCP HTTP/SSE 接口。
 合并自原 web_server 内联实现、web_server_mcp_patch.py 等重复代码，
统一为单文件实现，保留超时保护与 per-request ContextVar 上下文隔离。

对外导出：
- register_mcp_routes(app)：将 /mcp/* 路由挂载到 FastAPI app
- set_mcp_context(...)：由 bridge 在每次请求前注入会话上下文
"""
import asyncio
import json
import logging
import os
from contextvars import ContextVar
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from chat_client.tools import registry

logger = logging.getLogger("agent-mcp")

# ── 工具上下文（per-request，避免并发请求互相覆盖）─────────────────────
_mcp_session_id_var = ContextVar("mcp_session_id", default="")
_mcp_sessions_dir_var = ContextVar("mcp_sessions_dir", default="sessions")
_mcp_parent_config_var = ContextVar("mcp_parent_config", default={})


def set_mcp_context(session_id="", sessions_dir="sessions", parent_config=None):
    """设置当前请求的 MCP 工具上下文（非全局，每个 HTTP 请求独立）"""
    _mcp_session_id_var.set(session_id)
    _mcp_sessions_dir_var.set(sessions_dir)
    if parent_config:
        _mcp_parent_config_var.set(parent_config)


def _mcp_collect_tools():
    """收集所有可用工具，返回 MCP Tool 格式列表（排除 Skills / MCP 管理工具自身，防递归）"""
    raw = os.environ.get("BT_ENABLED_TOOLS", "").strip()
    enabled = None
    if raw:
        enabled = {t.strip() for t in raw.split(",") if t.strip()}
    tools = []
    for name, meta in registry._metadata.items():
        if not meta.get("show", True):
            continue
        if name == "Skills":
            continue
        if name == "MCP":
            continue
        if enabled is not None and meta.get("id") not in enabled:
            continue
        schema = next((s for s in registry._schemas if s["function"]["name"] == name), None)
        if not schema:
            continue
        params = schema["function"].get("parameters", {})
        input_schema = {"type": "object", "properties": {}}
        for pname, pspec in (params.get("properties") or {}).items():
            input_schema["properties"][pname] = pspec
        if params.get("required"):
            input_schema["required"] = params["required"]
        tools.append({
            "name": name,
            "description": meta.get("description") or f"BT 工具: {name}",
            "inputSchema": input_schema,
        })
    return tools


def _mcp_inject_ctx(name, arguments):
    """从 ContextVar 读取当前请求的 MCP 上下文（非全局共享）"""
    args = dict(arguments or {})
    if name in {"TodoWrite", "TodoRead", "TaskSummary"}:
        args.setdefault("session_id", _mcp_session_id_var.get())
        args.setdefault("sessions_dir", _mcp_sessions_dir_var.get())
    if name in {"Task", "RunCrew", "ConsultPeer"} and _mcp_parent_config_var.get():
        args.setdefault("parent_config", _mcp_parent_config_var.get())
        args.setdefault("parent_session_id", _mcp_session_id_var.get())
    return args


# ── 路由处理函数 ────────────────────────────────────────────────────────

async def mcp_health():
    return {"status": "ok", "server": "agent-tools"}


async def mcp_list_tools():
    return {"tools": _mcp_collect_tools()}


async def mcp_call_tool(tool_name: str, request: Request):
    body = await request.json()
    arguments = body.get("arguments", {})
    # 管理工具（MCP/Skills）不暴露给外部 MCP 客户端，防递归与特权滥用
    if tool_name in {"MCP", "Skills"}:
        return JSONResponse(status_code=404, content={"error": f"工具 {tool_name} 不允许通过 MCP 调用"})
    func = registry.get_tool_func(tool_name)
    if func is None:
        return JSONResponse(status_code=404, content={"error": f"工具 {tool_name} 不存在"})
    arguments = _mcp_inject_ctx(tool_name, arguments)
    try:
        # 工具调用加超时，防止阻塞协程永久挂起
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, lambda: func(**arguments)),
            timeout=300,
        )
        return {"result": str(result)}
    except asyncio.TimeoutError:
        return JSONResponse(status_code=504, content={"error": f"工具 {tool_name} 执行超时（300s）"})
    except Exception as e:
        logger.exception("工具 %s 执行异常", tool_name)
        return JSONResponse(status_code=500, content={"error": f"执行异常: {e}"})


async def mcp_sse_endpoint(request: Request):
    from mcp.server.sse import SseServerTransport
    from mcp.types import (Tool, TextContent, ListToolsResult, CallToolResult,
                            PaginatedRequestParams, CallToolRequestParams)
    from mcp.server import Server as McpServer

    sse = SseServerTransport("/mcp/messages/")
    srv = McpServer("agent-tools")

    async def handle_list_tools(_ctx, _params=None):
        return ListToolsResult(tools=[
            Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in _mcp_collect_tools()
        ])

    async def handle_call_tool(_ctx, params):
        name = params.name
        # 管理工具（MCP/Skills）不允许外部调用
        if name in {"MCP", "Skills"}:
            return CallToolResult(content=[TextContent(type="text", text=f"[MCP 工具 {name} 不允许通过 MCP 调用]")], isError=True)
        arguments = dict(params.arguments) if params.arguments else {}
        arguments = _mcp_inject_ctx(name, arguments)
        func = registry.get_tool_func(name)
        if func is None:
            return CallToolResult(content=[TextContent(type="text", text=f"[MCP 工具 {name} 不存在]")], isError=True)

        def _run():
            return func(**arguments)

        try:
            result = await asyncio.get_running_loop().run_in_executor(None, _run)
            return CallToolResult(content=[TextContent(type="text", text=str(result))])
        except Exception as e:
            logger.exception("工具 %s 执行异常", name)
            return CallToolResult(content=[TextContent(type="text", text=f"[MCP 工具 {name} 执行异常]: {e}")], isError=True)

    srv.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    srv.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)

    async def event_generator():
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await srv.create_session(streams[0], streams[1])
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def mcp_messages_endpoint(request: Request):
    body = await request.json()
    method = body.get("method", "")
    params = body.get("params", {})
    msg_id = body.get("id")

    if method == "tools/list":
        result = {"tools": _mcp_collect_tools()}
    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        # 管理工具（MCP/Skills）不允许外部调用
        if tool_name in {"MCP", "Skills"}:
            result = {"error": {"code": -32601, "message": f"工具 {tool_name} 不允许通过 MCP 调用"}}
        else:
            arguments = _mcp_inject_ctx(tool_name, arguments)
            func = registry.get_tool_func(tool_name)
            if func is None:
                result = {"error": {"code": -32601, "message": f"工具 {tool_name} 不存在"}}
            else:
                try:
                    result_text = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, lambda: func(**arguments)),
                        timeout=300,
                    )
                    result = {"content": [{"type": "text", "text": str(result_text)}]}
                except asyncio.TimeoutError:
                    result = {"error": {"code": -32604, "message": f"工具 {tool_name} 执行超时（300s）"}}
                except Exception as e:
                    result = {"error": {"code": -32603, "message": str(e)}}
    elif method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agent-tools", "version": "1.0.0"},
        }
    elif method == "notifications/initialized":
        return JSONResponse({"ok": True})
    else:
        result = {"error": {"code": -32601, "message": f"未知方法: {method}"}}

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": result,
    })


def register_mcp_routes(app):
    """将 MCP 路由挂载到 FastAPI app（/mcp 路径前缀）"""
    from fastapi import APIRouter

    router = APIRouter(prefix="/mcp")

    @router.get("/health")
    async def health():
        return await mcp_health()

    @router.get("/tools")
    async def tools():
        return await mcp_list_tools()

    @router.post("/tools/{tool_name}/call")
    async def call(tool_name: str, request: Request):
        return await mcp_call_tool(tool_name, request)

    @router.get("/sse")
    async def sse(request: Request):
        return await mcp_sse_endpoint(request)

    @router.post("/messages/")
    async def messages(request: Request):
        return await mcp_messages_endpoint(request)

    app.include_router(router)
    logger.info("MCP 路由已注册: /mcp/health, /mcp/tools, /mcp/sse, /mcp/messages/")
