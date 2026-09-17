"""
MCP 工具：list / add / remove / market（不做 test）

安装/移除 MCP server = 修改项目 mcp/config.json 的 mcpServers 配置。
市场 = GitHub 官方 MCP 市场（https://api.mcp.github.com/v0/servers）。
"""
import json

from ..mcp_manager import mcp_manager
from . import register_tool
from .base import _xml_response


def MCP(action: str = "list", name: str = "", config: str = "", overwrite: bool = False):
    """
    MCP Server 配置管理（读写项目 mcp/config.json，不触碰生产配置的其它条目）。

    action:
    - list (默认): 列出 mcp/config.json 已配置的 MCP servers
    - add: 新增一条 server 配置。name=server 名，config=JSON 字符串（如 {"command":"npx","args":["-y","@x/mcp"]}）
    - remove: 移除 name 指定的 server
    - market: 从 GitHub 官方 MCP 市场（api.mcp.github.com）拉取可用 server 清单
      条目包含 packages (stdio: npx/uvx/docker) 或 remotes (http streamable)。
      可通过 install_from_market(name) 一键生成可执行配置。
    """
    action = (action or "list").strip().lower()

    if action == "list":
        r = mcp_manager.list_servers()
        items = r.get("items", [])
        if not items:
            return _xml_response("done", "mcp/config.json 未配置任何 MCP server。")
        lines = [f"MCP servers（共 {len(items)} 个）："]
        for it in items:
            inst = it.get("command") or it.get("url") or ""
            env_tag = " [env]" if it.get("has_env") else ""
            lines.append(f"  - {it['name']} ({it['type']}) -> {inst}{env_tag}")
        lines.append("")
        lines.append(f"配置文件: {mcp_manager.config_path}")
        return _xml_response("done", "\n".join(lines))

    if action == "add":
        if not name:
            return _xml_response("error", "add 需要 name（server 名称）")
        if not config:
            return _xml_response("error", "add 需要 config（JSON 字符串，如 {\"command\":\"npx\",\"args\":[...]}）")
        try:
            cfg = json.loads(config)
            if not isinstance(cfg, dict):
                raise ValueError("config 必须是 JSON 对象")
        except Exception as e:
            return _xml_response("error", f"config 解析失败: {e}")
        r = mcp_manager.add_server(name, cfg, overwrite=overwrite)
        if r.get("status"):
            return _xml_response("done", f"{r.get('msg')}\n配置文件: {mcp_manager.config_path}")
        return _xml_response("error", r.get("msg", "添加失败"))

    if action == "remove":
        if not name:
            return _xml_response("error", "remove 需要 name（server 名称）")
        r = mcp_manager.remove_server(name)
        if r.get("status"):
            return _xml_response("done", r.get("msg", f"MCP server '{name}' 已移除"))
        return _xml_response("error", r.get("msg", "移除失败"))

    if action == "market":
        r = mcp_manager.fetch_market_list()
        if not r.get("status"):
            return _xml_response("error", r.get("msg", "拉取市场失败"))
        items = r.get("items", [])
        if not items:
            return _xml_response("done", "MCP 市场暂无 server。")
        lines = [f"MCP 市场（GitHub 官方，共 {len(items)} 个 server）："]
        for it in items:
            kind = ""
            # stdio 本地启动
            pkg = it.get("_package")
            if isinstance(pkg, dict) and pkg.get("runtime_hint"):
                kind = f" [{pkg['runtime_hint']}]"
            # remote HTTP
            rem = it.get("_remote")
            if isinstance(rem, dict) and rem.get("transport_type"):
                kind = f" [http:{rem['transport_type']}]"
            env_note = f" 🔑{it.get('env_vars','')}" if it.get("env_vars") else ""
            lines.append(f"  - {it['name']}{kind}{env_note}")
            if it.get("description"):
                lines.append(f"      {it['description'][:80]}")
        lines.append("")
        lines.append("一键安装示例：MCP(action='add', name='<name>', config=mcp_manager.install_from_market('<name>')['config'])")
        lines.append("注意：远程 HTTP server（如 github-mcp）需手动补充 Authorization header")
        return _xml_response("done", "\n".join(lines))

    return _xml_response("error", f"未知 action: {action}。可用: list/add/remove/market")


# 注册工具
MCP = register_tool(category="Agent", name_cn="MCP", risk_level="low")(MCP)
