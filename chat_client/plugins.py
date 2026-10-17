"""
@input: json, os, importlib, logging, threading, typing
@position: Plugin layer — 从 plugins/ 目录加载插件，插件可注册工具/钩子
@auto-doc: Update header and folder INDEX.md when this file changes

P3-a（2026-10-15）：
插件 = 一个目录，含 plugin.json（元数据）+ main.py（可选的 register(ctx) 入口）。
加载后可通过 ctx.register_tool 注册工具，或 ctx.on(event, cb) 挂载钩子。
不引入重量级依赖，纯标准库 + 现有 ToolRegistry。
"""
import importlib.util
import json
import logging
import os
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PluginContext:
    """传给插件 register(ctx) 的上下文，提供受控的注册能力。"""

    def __init__(self, plugin_id: str, base_dir: str, config: dict | None = None):
        self.plugin_id = plugin_id
        self.base_dir = base_dir
        self.config = config or {}
        self._hooks: dict[str, list[Callable]] = {}
        self._registered_tools: list[str] = []

    def register_tool(self, func: Callable, **meta) -> bool:
        """把函数注册为工具（复用主 ToolRegistry 的 register_tool）。"""
        try:
            # 插件模块是独立加载的（不在 chat_client 包内），只能用绝对导入
            from chat_client.tools import register_tool
            meta.setdefault("category", f"插件:{self.plugin_id}")
            register_tool(**meta)(func)
            self._registered_tools.append(getattr(func, "__name__", str(func)))
            return True
        except Exception as e:
            logger.warning("[Plugin:%s] 注册工具失败: %s", self.plugin_id, e)
            return False

    def on(self, event: str, callback: Callable) -> None:
        """挂载事件钩子（如 'tool_after' / 'chat_start'）。"""
        self._hooks.setdefault(event, []).append(callback)

    def emit(self, event: str, **payload) -> None:
        """触发事件钩子（插件自身可用）。"""
        for cb in self._hooks.get(event, []):
            try:
                cb(**payload)
            except Exception:
                logger.warning("[Plugin:%s] 钩子 %s 执行失败", self.plugin_id, event, exc_info=True)

    @property
    def registered_tools(self) -> list[str]:
        return list(self._registered_tools)


class PluginManager:
    """插件管理器（单例）：扫描 plugins/ 目录，加载/卸载插件。"""

    _instance = None
    _lock = threading.Lock()

    def __init__(self, plugins_dir: str | None = None):
        self.plugins_dir = plugins_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins"
        )
        self._plugins: dict[str, dict[str, Any]] = {}
        os.makedirs(self.plugins_dir, exist_ok=True)

    @classmethod
    def get_instance(cls, plugins_dir: str | None = None) -> "PluginManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(plugins_dir)
        return cls._instance

    def list_plugins(self) -> list[dict[str, Any]]:
        """扫描磁盘上的插件（不加载）。"""
        out = []
        if not os.path.isdir(self.plugins_dir):
            return out
        for name in sorted(os.listdir(self.plugins_dir)):
            pdir = os.path.join(self.plugins_dir, name)
            if not os.path.isdir(pdir) or name.startswith("."):
                continue
            meta = self._read_meta(pdir, name)
            meta["id"] = name
            meta["loaded"] = name in self._plugins
            out.append(meta)
        return out

    def _read_meta(self, pdir: str, name: str) -> dict[str, Any]:
        meta_file = os.path.join(pdir, "plugin.json")
        if os.path.exists(meta_file):
            try:
                with open(meta_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
            except Exception:
                logger.warning("[Plugin] %s 的 plugin.json 解析失败", name, exc_info=True)
        return {"name": name, "version": "", "description": ""}

    def load_plugin(self, name: str) -> dict[str, Any]:
        """加载单个插件（执行其 main.py 的 register(ctx)）。"""
        pdir = os.path.join(self.plugins_dir, name)
        if not os.path.isdir(pdir):
            return {"status": False, "msg": f"插件不存在: {name}"}
        if name in self._plugins:
            return {"status": True, "msg": f"插件已加载: {name}"}
        meta = self._read_meta(pdir, name)
        ctx = PluginContext(name, pdir, meta.get("config") if isinstance(meta.get("config"), dict) else {})
        main_py = os.path.join(pdir, "main.py")
        tools_registered = 0
        if os.path.exists(main_py):
            try:
                spec = importlib.util.spec_from_file_location(f"bt_plugin_{name}", main_py)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                reg = getattr(mod, "register", None)
                if callable(reg):
                    reg(ctx)
                    tools_registered = len(ctx.registered_tools)
            except Exception as e:
                logger.warning("[Plugin] %s 加载失败: %s", name, e, exc_info=True)
                return {"status": False, "msg": f"插件 {name} 加载失败: {e!s}"}
        self._plugins[name] = {"meta": meta, "ctx": ctx, "tools": ctx.registered_tools}
        return {"status": True, "msg": f"插件 {name} 已加载（注册 {tools_registered} 个工具）",
                "tools": ctx.registered_tools}

    def load_all(self) -> dict[str, Any]:
        """加载所有磁盘插件。"""
        results = {}
        for p in self.list_plugins():
            results[p["id"]] = self.load_plugin(p["id"])
        return results

    def unload_plugin(self, name: str) -> dict[str, Any]:
        """卸载插件（仅从管理器移除；已注册的工具需重启/热重载方可彻底移除）。"""
        if name not in self._plugins:
            return {"status": False, "msg": f"插件未加载: {name}"}
        del self._plugins[name]
        return {"status": True, "msg": f"插件 {name} 已卸载（已注册工具将在热重载后移除）"}

    def status(self) -> dict[str, Any]:
        return {
            "plugins_dir": self.plugins_dir,
            "total": len(self.list_plugins()),
            "loaded": list(self._plugins.keys()),
        }


def get_plugin_manager() -> PluginManager:
    return PluginManager.get_instance()
