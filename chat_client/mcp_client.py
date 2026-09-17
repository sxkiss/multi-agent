"""
@input: asyncio, json, logging, subprocess, threading; mcp_sdk, chat_client.tools.registry
@output: MCPClient — 异步转同步调用，工具注册到 ToolRegistry
@position: MCP layer — stdio MCP 接入，工具动态注册为普通工具
@auto-doc: Update header and folder INDEX.md when this file changes

MCP (Model Context Protocol) 客户端集成模块。
读取 MCP 配置文件，启动 MCP 服务器，发现工具并注册到 ToolRegistry。

设计说明：
- 整个 Agent 对话流程是同步的，而 MCP Python SDK 是异步的。
- 本模块维护一个后台事件循环线程，所有 MCP 连接/调用都在该线程的事件循环中执行。
- 对外暴露同步接口 call_tool()，内部通过 run_coroutine_threadsafe 等待结果。
- 使用 stdio_client context manager 管理子进程生命周期，保持连接常驻。
"""
import asyncio
import json
import logging
import os
import re
import threading
from typing import Any

from mcp.client.stdio import StdioServerParameters, stdio_client

from chat_client.tools import registry
from mcp import ClientSession

logger = logging.getLogger(__name__)

# MCP 配置文件搜索路径（按优先级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MCP_CONFIG_PATHS = [
    os.path.join(_PROJECT_ROOT, "mcp", "config.json"),
]

# 单次工具调用超时（秒）
TOOL_CALL_TIMEOUT = 120

# 单个 MCP 服务器连接/握手超时（秒）；超时即判定为不可用并跳过，不影响其他服务器
SERVER_CONNECT_TIMEOUT = 30


def _resolve_env_vars(value: Any) -> Any:
    """递归解析 ${input:xxx} 占位符，替换为空字符串"""
    if isinstance(value, str):
        return re.sub(r"\$\{input:[^}]+\}", "", value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def load_mcp_config(config_path: str) -> dict | None:
    """加载 MCP 配置文件，返回标准化的 mcpServers 字典"""
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "mcpServers" in data:
            return data["mcpServers"]
        if "servers" in data:
            return data["servers"]
        if data and all(isinstance(v, dict) for v in data.values()):
            return data
        return None
    except Exception as e:
        logger.warning(f"加载 MCP 配置失败 {config_path}: {e}")
        return None


def find_mcp_config() -> str | None:
    """按优先级查找 MCP 配置文件"""
    for path in MCP_CONFIG_PATHS:
        path = os.path.expanduser(path)
        if os.path.exists(path):
            logger.info(f"找到 MCP 配置文件: {path}")
            return path
    return None


def _tool_schema_from_mcp(mcp_tool: Any) -> dict[str, Any]:
    """将 MCP Tool 转换为 OpenAI function-calling schema"""
    props = {}
    required = []
    input_schema = getattr(mcp_tool, "input_schema", {}) or {}
    for prop_name, prop_def in input_schema.get("properties", {}).items():
        p = {"type": prop_def.get("type", "string")}
        if "description" in prop_def:
            p["description"] = prop_def["description"]
        props[prop_name] = p
    for r in input_schema.get("required", []):
        if r not in required:
            required.append(r)
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.name,
            "description": mcp_tool.description or f"MCP tool: {mcp_tool.name}",
            # MCP 工具的 input_schema 是任意 JSON Schema，未必满足 OpenAI strict 模式
            # （缺 additionalProperties:false、含嵌套/anyOf 等），强制 strict=True 会导致
            # 整批 tools 请求被 API 拒绝并返回空响应。故 MCP 工具统一用 strict=False。
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


class MCPClient:
    """MCP 客户端：后台事件循环 + 同步调用接口"""

    def __init__(self, config_path: str | None = None):
        self._config_path = config_path or find_mcp_config()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._stdio_clients: list = []   # 保持 stdio_client context managers 常驻
        self._session_cms: list = []     # 保持 ClientSession context managers 常驻
        self._session_by_name: dict[str, ClientSession] = {}  # server_name -> session
        self._tool_schemas: list[dict[str, Any]] = []
        self._tool_servers: dict[str, str] = {}  # tool_name -> server_name
        self._loaded = False

    def set_config_path(self, path: str):
        """动态设置配置路径"""
        if path and os.path.exists(path):
            self._config_path = path

    # ---------- 后台事件循环管理 ----------
    def _start_loop(self):
        if self._loop is not None:
            return
        self._loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._loop_thread = threading.Thread(target=_run, name="mcp-event-loop", daemon=True)
        self._loop_thread.start()
        logger.info("MCP 后台事件循环已启动")

    def _run_coro(self, coro, timeout: float = 60):
        """在后台事件循环中运行协程并等待结果（同步接口）"""
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("MCP 事件循环未运行")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ---------- 服务器连接 ----------
    async def _connect_server(self, name: str, cfg: dict):
        """异步连接单个 MCP 服务器并发现工具"""
        if cfg.get("type") == "http" or "url" in cfg or "baseUrl" in cfg:
            logger.info(f"跳过 HTTP/SSE MCP 服务器: {name}（当前仅支持 stdio）")
            return
        command = cfg.get("command")
        if not command:
            logger.warning(f"MCP 服务器 {name} 缺少 command 字段，跳过")
            return
        args = cfg.get("args", [])
        env = _resolve_env_vars(cfg.get("env", {}))
        cwd = cfg.get("cwd")
        params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)

        # stdio_client 是 async context manager，必须保持 __aenter__ 常驻
        stdio_cm = stdio_client(params)
        read_stream, write_stream = await stdio_cm.__aenter__()
        self._stdio_clients.append(stdio_cm)  # 保持 stdio_cm 活跃

        # ClientSession 也是 async context manager，需保持 __aenter__ 常驻
        session_cm = ClientSession(read_stream, write_stream)
        try:
            session = await session_cm.__aenter__()
        except Exception:
            # 会话握手失败时必须释放已建立的 stdio 子进程，避免资源泄漏
            await stdio_cm.__aexit__(None, None, None)
            self._stdio_clients.remove(stdio_cm)
            raise
        self._session_cms.append(session_cm)  # 保持 session_cm 活跃

        await session.initialize()
        tools_resp = await session.list_tools()
        mcp_tools = tools_resp.tools if hasattr(tools_resp, "tools") else []
        logger.info(f"MCP 服务器 {name} 已连接，发现 {len(mcp_tools)} 个工具")
        self._session_by_name[name] = session

        def _make_sync_call(_tool, _server, _display_name):
            def _call(**kwargs):
                return self.call_tool(_tool, kwargs, server_name=_server)
            _call.__name__ = _display_name
            return _call

        for mcp_tool in mcp_tools:
            schema = _tool_schema_from_mcp(mcp_tool)
            tool_name = mcp_tool.name
            self._tool_schemas.append(schema)
            self._tool_servers[tool_name] = name

            func = _make_sync_call(tool_name, name, tool_name)
            registry._register_func(func, tool_name, "mcp", f"MCP: {tool_name} ({name})", "medium")

    def load(self):
        """同步加载所有 MCP 服务器。

        不可用的服务器（命令缺失 / 连接超时 / 握手失败 / 启动异常）会被单独跳过，
        不影响其余可用服务器的加载；仅当整体加载流程异常时才保留未加载状态以允许后续重试。
        """
        if self._loaded:
            return
        if not self._config_path:
            logger.info("未找到 MCP 配置文件，跳过 MCP 集成")
            self._loaded = True
            return
        config = load_mcp_config(self._config_path)
        if not config:
            logger.info(f"MCP 配置文件 {self._config_path} 无有效服务器配置")
            self._loaded = True
            return
        logger.info(f"加载 MCP 服务器配置: {list(config.keys())}")
        self._start_loop()

        async def _connect_all():
            connected = []
            for name, cfg in config.items():
                if not isinstance(cfg, dict):
                    continue
                try:
                    # 逐服务器独立超时：单个不可用（挂起/握手失败）只跳过它，不拖累其他
                    await asyncio.wait_for(
                        self._connect_server(name, cfg),
                        timeout=SERVER_CONNECT_TIMEOUT,
                    )
                    connected.append(name)
                except asyncio.TimeoutError:
                    logger.warning(f"[MCP] 服务器 {name} 连接超时（不可用），已跳过")
                except (FileNotFoundError, PermissionError) as e:
                    logger.warning(f"[MCP] 服务器 {name} 启动失败（命令不可用: {e}），已跳过")
                except Exception as e:
                    logger.warning(f"[MCP] 服务器 {name} 连接失败（不可用），已跳过: {e}")
            return connected

        try:
            connected = self._run_coro(
                _connect_all(),
                timeout=SERVER_CONNECT_TIMEOUT * max(1, len(config)) + 15,
            )
            skipped = [n for n in config if n not in connected]
            logger.info(
                f"MCP 集成完成：已加载 {len(connected)}/{len(config)} 个服务器"
                + (f"（跳过不可用: {skipped}）" if skipped else "")
                + f"，注册 {len(self._tool_schemas)} 个工具"
            )
        except Exception as e:
            logger.error(f"MCP 加载失败（保留未加载状态，允许后续重试）: {e}")
            self._loaded = False
            return
        self._loaded = True

    def call_tool(self, tool_name: str, arguments: dict[str, Any], server_name: str | None = None) -> str:
        """同步调用 MCP 工具（线程安全）"""
        server = server_name or self._tool_servers.get(tool_name)
        if not server or server not in self._session_by_name:
            return f"[MCP 工具 {tool_name} 未连接]"

        session = self._session_by_name[server]

        async def _do_call():
            result = await session.call_tool(tool_name, arguments=arguments or {})
            parts = []
            if hasattr(result, "content"):
                for block in result.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                    elif isinstance(block, dict) and "text" in block:
                        parts.append(block["text"])
            if result.is_error:
                return f"[MCP 工具 {tool_name} 执行错误]: " + "\n".join(parts)
            return "\n".join(parts) if parts else str(result)

        try:
            return self._run_coro(_do_call(), timeout=TOOL_CALL_TIMEOUT)
        except Exception as e:
            return f"[MCP 工具 {tool_name} 调用异常]: {e}"

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return self._tool_schemas

    def is_loaded(self) -> bool:
        return self._loaded

    def close(self):
        """关闭所有 MCP 服务器"""
        async def _close():
            # 先关闭 sessions
            for session_cm in self._session_cms:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    logger.warning("未处理的异常", exc_info=True)
            # 再关闭 stdio clients
            for cm in self._stdio_clients:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    logger.warning("未处理的异常", exc_info=True)
        if self._loop and self._loop.is_running():
            try:
                self._run_coro(_close(), timeout=10)
            except Exception:
                logger.warning("未处理的异常", exc_info=True)
        self._stdio_clients.clear()
        self._session_cms.clear()
        self._session_by_name.clear()
        self._tool_schemas.clear()
        self._tool_servers.clear()
        self._loaded = False


# 全局 MCP 客户端单例
_mcp_client: MCPClient | None = None
_mcp_init_lock = threading.Lock()


def get_mcp_client() -> MCPClient:
    global _mcp_client
    if _mcp_client is None:
        with _mcp_init_lock:
            if _mcp_client is None:
                _mcp_client = MCPClient()
    return _mcp_client


def ensure_mcp_loaded() -> MCPClient:
    """同步确保 MCP 已加载（幂等，线程安全）"""
    client = get_mcp_client()
    if not client.is_loaded():
        with _mcp_init_lock:
            if not client.is_loaded():
                client.load()
    return client
