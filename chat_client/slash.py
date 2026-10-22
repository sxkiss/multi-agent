"""
@input: json, re, typing
@position: Gateway layer — 斜杠命令注册/解析/分发（轻量版，纯字符串分发）
@auto-doc: Update header and folder INDEX.md when this file changes

+ `dispatcher.py` 的轻量版。

bt 此前没有斜杠命令体系（全靠自然语言）。本模块提供：
- 命令注册表：id/别名/分类/描述/参数提示
- 解析：`/cmd arg1 arg2` → (command, args)
- 分发：调用注册的 handler
- 内置命令：/help /clear /model /tools /context /experts /skills /backup

（bt 是单用户单实例，够用即可）。
"""
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class SlashCommand:
    def __init__(self, name: str, handler: Callable, *, aliases: list[str] | None = None,
                 category: str = "通用", description: str = "", usage: str = "",
                 args_hint: str = ""):
        self.name = name
        self.handler = handler
        self.aliases = aliases or []
        self.category = category
        self.description = description
        self.usage = usage or f"/{name} {args_hint}".strip()
        self.args_hint = args_hint

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "aliases": self.aliases, "category": self.category,
            "description": self.description, "usage": self.usage,
        }


class SlashCatalog:
    """斜杠命令注册表 + 解析 + 分发。"""

    def __init__(self):
        self._commands: dict[str, SlashCommand] = {}
        self._aliases: dict[str, str] = {}

    def register(self, name: str, handler: Callable, *, aliases: list[str] | None = None,
                 category: str = "通用", description: str = "", usage: str = "",
                 args_hint: str = "") -> SlashCommand:
        cmd = SlashCommand(name, handler, aliases=aliases, category=category,
                           description=description, usage=usage, args_hint=args_hint)
        self._commands[name] = cmd
        for a in cmd.aliases:
            self._aliases[a] = name
        return cmd

    def parse(self, text: str) -> tuple[str, list[str]] | None:
        """解析 `/cmd arg1 arg2`，返回 (command_name, args) 或 None（非命令）。"""
        if not text or not text.lstrip().startswith("/"):
            return None
        body = text.lstrip()[1:].strip()
        if not body:
            return None
        parts = body.split()
        name = parts[0].lower()
        name = self._aliases.get(name, name)
        return name, parts[1:]

    def dispatch(self, text: str, **ctx) -> dict[str, Any] | None:
        """解析并执行命令。非命令返回 None；未知命令返回错误 dict。"""
        parsed = self.parse(text)
        if parsed is None:
            return None
        name, args = parsed
        cmd = self._commands.get(name)
        if not cmd:
            return {"status": False, "error": f"未知命令: /{name}",
                    "hint": "输入 /help 查看可用命令"}
        try:
            result = cmd.handler(args, **ctx)
            return {"status": True, "command": name, "result": result}
        except Exception as e:
            logger.warning("斜杠命令 /%s 执行失败: %s", name, e)
            return {"status": False, "command": name, "error": str(e)}

    def list_commands(self) -> list[dict[str, Any]]:
        out = [c.to_dict() for c in self._commands.values()]
        out.sort(key=lambda c: (c["category"], c["name"]))
        return out


_catalog: SlashCatalog | None = None


def get_slash_catalog() -> SlashCatalog:
    """取全局斜杠命令目录（单例，首次调用注册内置命令）。"""
    global _catalog
    if _catalog is None:
        _catalog = SlashCatalog()
        _register_builtins(_catalog)
    return _catalog


def _register_builtins(cat: SlashCatalog) -> None:
    """注册内置命令（handler 接收 (args, **ctx)，ctx 可含 session_id/base_dir 等）。"""

    def _help(args, **ctx):
        lines = ["可用命令："]
        by_cat: dict[str, list] = {}
        for c in cat.list_commands():
            by_cat.setdefault(c["category"], []).append(c)
        for c in cat.list_commands():
            lines.append(f"  {c['usage']:28} {c['description']}")
        return "\n".join(lines)

    def _clear(args, **ctx):
        return {"action": "clear", "msg": "已请求清空当前会话上下文"}

    def _model(args, **ctx):
        if not args:
            return {"action": "model", "current": ctx.get("model", "")}
        return {"action": "model", "set": args[0]}

    def _tools(args, **ctx):
        try:
            from chat_client.tools import registry
            names = sorted(registry._metadata.keys())
            return {"action": "tools", "count": len(names), "tools": names}
        except Exception as e:
            return {"action": "tools", "error": str(e)}

    def _context(args, **ctx):
        try:
            from chat_client.session_tools import analyze_context
            base = ctx.get("base_dir", ".")
            sid = ctx.get("session_id", "")
            return analyze_context(base, sid)
        except Exception as e:
            return {"error": str(e)}

    def _experts(args, **ctx):
        try:
            from chat_client.experts import get_catalog
            base = ctx.get("base_dir", ".")
            cat2 = get_catalog(base)
            return {"count": len(cat2.scan()),
                    "experts": [e.to_dict() for e in cat2.scan()]}
        except Exception as e:
            return {"error": str(e)}

    def _skills(args, **ctx):
        try:
            from chat_client.skills import skill_manager
            return {"count": len(skill_manager.all_enabled())}
        except Exception as e:
            return {"error": str(e)}

    def _backup(args, **ctx):
        return {"action": "backup", "msg": "已请求创建备份"}

    cat.register("help", _help, aliases=["h", "?"], category="通用",
                 description="显示所有可用命令")
    cat.register("clear", _clear, aliases=["cls"], category="会话",
                 description="清空当前会话上下文")
    cat.register("model", _model, category="配置",
                 description="查看/切换模型", args_hint="[模型名]")
    cat.register("tools", _tools, category="配置",
                 description="列出已注册工具")
    cat.register("context", _context, aliases=["ctx"], category="会话",
                 description="查看当前会话上下文占用")
    cat.register("experts", _experts, category="能力",
                 description="列出本地专家库")
    cat.register("skills", _skills, category="能力",
                 description="列出已启用技能数")
    cat.register("backup", _backup, category="运维",
                 description="创建备份")
