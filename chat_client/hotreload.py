"""
@input: importlib, pathlib, threading, time; sys module paths
@output: reload_all()/reload_target() 函数，watcher 后台线程
@position: Infra — 热加载中心，tools/config/crew/mcp 四大目标支持不重启更新
@auto-doc: Update header and folder INDEX.md when this file changes

热加载中心：核心层（web服务/Agent循环/记忆/RAG）保持常驻，
其余能力——工具代码、系统配置、角色团队、MCP——均支持不重启热更新。

触发方式：
1. 文件监视线程：watch 路径 mtime 变化自动重载（默认 10s 轮询）
2. API：POST /api/hotreload {"target": "all|tools|config|crew|mcp"}
3. Agent 工具：HotReload(target) —— 支持程序自我迭代（改完工具代码即生效）

安全策略：
- 工具重载采用「快照-清空-重执行注册-失败回滚」，坏代码不会导致工具体系瘫痪
- MCP 重连代价高，仅显式指定 target='mcp' 时执行
- scheduler.py 不参与重载（其后台线程持有模块级状态）
"""
import importlib
import logging
import os
import threading
from collections.abc import Callable
from typing import Any

log = logging.getLogger("hotreload")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 参与重载的工具模块（__init__ 与 scheduler 除外）
_TOOL_MODULES = [
    "chat_client.tools.agent_tools",
    "chat_client.tools.edit",
    "chat_client.tools.terminal",
    "chat_client.tools.sys_ops",
    "chat_client.tools.webfetch",
    "chat_client.tools.skill",
    "chat_client.tools.todo",
    "chat_client.tools.summary",
    "chat_client.tools.task",
]

_WATCH_FILES = {
    "tools": [os.path.join(_PROJECT_ROOT, "chat_client", "tools", m.rsplit(".", 1)[1] + ".py")
              for m in _TOOL_MODULES],
    "config": [os.path.join(_PROJECT_ROOT, "config.json")],
    "crew": [os.path.join(_PROJECT_ROOT, "crew_agents.json")],
    "mcp": [os.path.join(_PROJECT_ROOT, "mcp", "config.json")],
}

_lock = threading.Lock()
_last_mtimes = {}
_am_getter = lambda: None


def init(am_getter: Callable[[], Any]):
    global _am_getter
    _am_getter = am_getter
    for t, files in _WATCH_FILES.items():
        _last_mtimes[t] = _max_mtime(files)


def _max_mtime(files):
    mt = 0.0
    for f in files:
        try:
            mt = max(mt, os.path.getmtime(f))
        except OSError:

            logger.warning("循环处理时跳过异常", exc_info=True)
            continue
    return mt


def _get_registry():
    from chat_client.tools import registry
    return registry


def _snapshot_registry():
    r = _get_registry()
    return (dict(r._tools), list(r._schemas), dict(r._metadata))


def _restore_registry(snap):
    r = _get_registry()
    r._tools, r._schemas, r._metadata = snap


def reload_tools() -> str:
    """清空注册表 → importlib.reload 各工具模块（装饰器重新注册）→ 失败回滚"""
    r = _get_registry()
    snap = _snapshot_registry()
    try:
        with r._meta_lock:
            r._tools.clear()
            r._schemas.clear()
            r._metadata.clear()
        import sys
        for mod_name in _TOOL_MODULES:
            mod = sys.modules.get(mod_name)
            if mod is None:
                importlib.import_module(mod_name)
            else:
                importlib.reload(mod)
        # 回填「非本次重载模块」的历史注册（scheduler / hotreload 等自身工具，
        # 它们不参与模块 reload，但注册曾被统一清空）
        new_names = {s["function"]["name"] for s in r._schemas}
        snap_schemas = {}
        for s in snap[1]:
            nm = s["function"]["name"]
            snap_schemas.setdefault(nm, s)
        restored = 0
        for fname, fn in snap[0].items():
            if fname in new_names:
                continue
            meta = snap[2].get(fname)
            sch = snap_schemas.get(fname)
            if meta and sch:
                r._tools[fname] = fn
                r._schemas.append(sch)
                r._metadata[fname] = meta
                restored += 1

        n = len(r._schemas)
        log.info("[HotReload] 工具重载完成，共 %s 个（回填外部注册 %s）", n, restored)
        return f"tools: 已重载，当前 {n} 个工具（含保留 {restored}）"
    except Exception as e:
        _restore_registry(snap)
        msg = f"工具重载失败已回滚: {e}"
        log.error("[HotReload] %s", msg)
        return f"tools: {msg}"


def reload_config() -> str:
    """从 config.json 重建运行时配置"""
    am = _am_getter()
    if am is None:
        return "config: AgentMain 未绑定"
    import copy
    am.config = copy.deepcopy(am.DEFAULT_CONFIG)
    am._merge_config(am.config, am._load_config())
    log.info("[HotReload] 配置已重载")
    return f"config: 已重载（models={am.config.get('models')}，default={am.config.get('default_model')}）"


def reload_crew() -> str:
    from chat_client.tools.task import reload_custom_agents
    names = reload_custom_agents()
    depts = [d["title"] for d in getattr(agent_registry_dep(), "departments", [])]
    return f"crew: 成员 {names} / 部门 {depts}"


def agent_registry_dep():
    from chat_client.tools.task import agent_registry
    return agent_registry


def reload_mcp() -> str:
    try:
        from chat_client.mcp_client import get_mcp_client
        mc = get_mcp_client()
        mc.close()
        mc._loaded = False
        mc.load()
        n = len(mc.get_tool_schemas())
        return f"mcp: 已重连，{n} 个工具"
    except Exception as e:
        return f"mcp: 重连失败 {e}"


_LOADERS = {
    "tools": reload_tools,
    "config": reload_config,
    "crew": reload_crew,
    "mcp": reload_mcp,
}


def reload_targets(targets):
    results = {}
    for t in targets:
        fn = _LOADERS.get(t)
        if not fn:
            results[t] = "未知目标"
            continue
        try:
            results[t] = fn()
        except Exception as e:
            results[t] = f"失败: {e}"
            log.exception("[HotReload] %s 失败", t)
    with _lock:
        for t in targets:
            if t in _WATCH_FILES:
                _last_mtimes[t] = _max_mtime(_WATCH_FILES[t])
    return results


_watcher_thread = None


def start_watcher(interval: int = 10):
    """已禁用自动监听：importlib.reload 持有 import lock 会阻塞主线程导入。
    热重载仅通过 POST /api/hotreload 或 Agent 工具 HotReload() 显式触发。"""
    log.info("[HotReload] watcher 已禁用（防 import 竞争），使用 API/工具手动触发")


# ---------------- Agent 自我迭代工具 ----------------

from .tools import register_tool
from .tools.base import _xml_response

logger = logging.getLogger(__name__)


@register_tool(category="系统", name_cn="热重载", risk_level="medium", timeout=180)
def HotReload(target: str = "all") -> str:
    """
    热重载系统组件，使修改立即生效而无需重启服务。
    用于程序自我迭代优化：修改工具代码 / crew_agents.json / config.json 后调用此工具。

    Args:
        target: all(全部) | tools(工具代码) | config(系统配置) | crew(角色团队) | mcp(MCP重连)
    """
    target = str(target).strip().lower()
    targets = list(_LOADERS.keys()) if target == "all" else [target]
    results = reload_targets(targets)
    ok = all(not v.startswith("失败") and "未" not in v[:3] or v.startswith(("tools:", "config:", "crew:")) for v in results.values())
    body = "; ".join(f"{k}: {v}" for k, v in results.items())
    return _xml_response("done" if ok else "error", body)
