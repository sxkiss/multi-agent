
import os

from ..skills import skill_manager
from . import register_tool
from .base import _xml_response

# 配置常量：skill 元数据占上下文窗口的上限比例
SKILL_METADATA_RATIO_LIMIT = 0.02  # 2%

# 从 config.json 动态读取上下文窗口大小，默认 256K tokens
# 注意：context_window_kb 单位是 K tokens（不是 KB），256 = 256K tokens
def _load_context_window_tokens() -> int:
    """读取 config.json 中的 context_window_kb（单位：K tokens），返回 token 数"""
    import json as _json
    _config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config.json")
    try:
        with open(_config_path, "r", encoding="utf-8") as f:
            _cfg = _json.load(f)
        k_tokens = _cfg.get("context_window_kb", 256)
        return int(k_tokens) * 1024  # K tokens -> tokens
    except Exception:
        return 256 * 1024  # 默认 256K tokens

DEFAULT_CONTEXT_WINDOW_TOKENS = _load_context_window_tokens()


def _estimate_tokens(text: str) -> int:
    """粗略估算 token 数（中英文混合：保守按 2 字符/token，与 agent.py 保持一致）"""
    return len(text) // 2


def _get_skill_doc(max_context_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS) -> str:
    """
    动态生成文档字符串，包含可用 skills
    实现 2% 上限：只加载最重要的 skill 元数据
    """
    skills = skill_manager.all_enabled()

    if not skills:
        return "Load a specialized skill that provides domain-specific instructions and workflows. No skills are currently available."

    # 计算可用 token 预算
    max_tokens = int(max_context_tokens * SKILL_METADATA_RATIO_LIMIT)
    
    # 按优先级排序（启用状态 > 描述长度）
    # 这里简单按描述长度排序，描述越长通常越重要
    sorted_skills = sorted(skills, key=lambda s: len(s.description), reverse=True)
    
    # 选择最重要的 skill，确保不超过 token 预算
    selected_skills = []
    used_tokens = 0
    
    for skill in sorted_skills:
        # 每个 skill 的元数据约 100 tokens（name + description）
        skill_tokens = _estimate_tokens(skill.name) + _estimate_tokens(skill.description) + 10
        
        if used_tokens + skill_tokens > max_tokens:
            break
        
        selected_skills.append(skill)
        used_tokens += skill_tokens
    
    if not selected_skills:
        # 至少包含第一个 skill
        selected_skills = sorted_skills[:1]
    
    skill_list = "\n".join([
        f"  <skill>\n    <name>{s.name}</name>\n    <description>{s.description}</description>\n  </skill>"
        for s in selected_skills
    ])

    examples = ", ".join([f"'{s.name}'" for s in selected_skills[:3]])
    hint = f" (e.g., {examples}, ...)" if examples else ""
    
    # 添加预算信息
    budget_info = f"({len(selected_skills)}/{len(skills)} skills loaded, {used_tokens}/{max_tokens} tokens used)"

    return f"""Load a specialized skill that provides domain-specific instructions and workflows.

When you recognize that a task matches one of the available skills listed below, use this tool to load the full skill instructions.

The skill will inject detailed instructions, workflows, and access to bundled resources (scripts, references, templates) into the conversation context.

Tool output includes a `<skill_content name="...">` block with the loaded content.

The following skills provide specialized sets of instructions for particular tasks {budget_info}
Invoke this tool to load a skill when a task matches one of the available skills listed below:

<available_skills>
{skill_list}
</available_skills>

Actions (action 参数):
- load (default): 加载指定 name 的技能。可加 resource='scripts/x.py' 读附属文件
- install: 安装技能到项目 skills/ 目录。source 可为 GitHub 仓库链接 / zip 直链 / 本地目录（zip 或文件夹）
- uninstall: 卸载 name 指定的技能
- list: 列出当前已装技能
- market: 从配置的技能市场（多源，config.json skills_market）拉取可用技能清单

Args:
    name: The name of the skill from available_skills{hint}
    action: load | install | uninstall | list | market (default: load)
    source: install 时的来源（URL / 本地路径）
    overwrite: install 时是否覆盖已存在技能 (default: false)
    resource: load 时按需加载的附属文件路径
"""

def Skills(name: str = "", action: str = "load", source: str = "", overwrite: bool = False, resource: str = ""):
    """
    Placeholder docstring, will be replaced dynamically.

    技能工具，支持加载与生命周期管理（安装/卸载/市场）。
    - action="load"（默认）：渐进式披露加载 SKILL.md（Layer 2）或附属文件（Layer 3）
    - action="install"：从 source 安装技能到项目 skills/ 目录
        source 可为 GitHub 仓库页/zip 直链（install_from_url），
        或本地 zip 包 / 本地目录（install_from_dir）
    - action="uninstall"：卸载 name 指定的已装技能
    - action="list"：列出当前已装技能
    - action="market"：从配置的技能市场（多源，config.json skills_market）拉取可用技能清单
    """
    action = (action or "load").strip().lower()

    # ---- 安装 / 卸载（高风险，需二次确认由 risk_level 控制）----
    if action == "install":
        if not source:
            return _xml_response("error", "install 需要提供 source（GitHub 仓库链接 / zip 直链 / 本地目录）")
        if source.lower().startswith(("http://", "https://")):
            result = skill_manager.install_from_url(source, overwrite=overwrite)
        else:
            # 本地路径：zip 包 or 目录
            src = os.path.abspath(os.path.expanduser(source.strip()))
            if src.lower().endswith(".zip") and os.path.isfile(src):
                import zipfile as _zf
                try:
                    with open(src, "rb") as f:
                        data = f.read()
                except Exception as e:
                    return _xml_response("error", f"读取 zip 失败: {e!s}")
                if not data.startswith(b"PK"):
                    return _xml_response("error", "文件不是有效的 ZIP 包")
                result = skill_manager.install_from_zip(data, overwrite=overwrite)
            else:
                result = skill_manager.install_from_dir(src, overwrite=overwrite)
        if result.get("status"):
            return _xml_response("done", f"{result.get('msg')}\n技能目录: {result.get('path')}\n安装后无需重启，下一轮对话即可加载。")
        return _xml_response("error", result.get("msg", "安装失败"))

    if action == "uninstall":
        if not name:
            return _xml_response("error", "uninstall 需要提供 name（技能名）")
        result = skill_manager.uninstall(name)
        if result.get("status"):
            return _xml_response("done", result.get("msg", f"技能 {name} 已卸载"))
        return _xml_response("error", result.get("msg", "卸载失败"))

    if action == "list":
        skills = skill_manager.all()
        if not skills:
            return _xml_response("done", "当前无已装技能。")
        lines = [f"已装技能（共 {len(skills)} 个）："]
        for s in skills:
            lines.append(f"  - {s.name}")
        return _xml_response("done", "\n".join(lines))

    if action == "market":
        result = skill_manager.fetch_market_list()
        if not result.get("status"):
            return _xml_response("error", result.get("msg", "拉取市场失败"))
        items = result.get("items", [])
        if not items:
            return _xml_response("done", "市场暂无技能。")
        lines = [f"技能市场（共 {len(items)} 个，* 为已安装）："]
        for it in items:
            mark = "*" if it.get("installed") else " "
            repo_tag = f" <{it.get('repo')}>" if it.get("repo") else ""
            lines.append(f"  [{mark}] {it['name']}{repo_tag}")
        lines.append("")
        lines.append("安装示例：Skills(action='install', source='<市场技能 url>')")
        return _xml_response("done", "\n".join(lines))

    # ---- 默认：加载（Layer 2 / Layer 3）----
    if not name:
        return _xml_response("error", "load 需要提供 name（技能名）。可用 action: load/install/uninstall/list/market")
    skill_obj = skill_manager.get_enabled(name)

    if not skill_obj:
        target_skill = skill_manager.get(name)
        if target_skill and not skill_manager.is_enabled(name):
            return _xml_response("error", f"Skill '{name}' is disabled.")
        available = ", ".join([s.name for s in skill_manager.all_enabled()])
        return _xml_response("error", f"Skill '{name}' not found. Available skills: {available or 'none'}")

    skill_dir = os.path.dirname(skill_obj.location)

    # Layer 3: 按需加载附属文件
    if resource:
        return _load_skill_resource(skill_obj, skill_dir, resource)

    # Layer 2: 加载 SKILL.md body + 文件列表
    files = skill_manager.list_files(skill_dir)
    file_list_str = "\n".join([f"<file>{f}</file>" for f in files])

    output = [
        f"<skill_content name=\"{skill_obj.name}\">",
        f"# Skill: {skill_obj.name}",
        "",
        skill_obj.content.strip(),
        "",
        f"Base directory for this skill: {skill_dir}",
        "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
        "To load a specific file, use: resource='scripts/example.py' or 'references/rules.md'",
        "",
        "<skill_files>",
        file_list_str,
        "</skill_files>",
        "</skill_content>"
    ]

    return _xml_response("done", "\n".join(output))


def _load_skill_resource(skill_obj, skill_dir: str, resource: str) -> str:
    """
    Layer 3: 加载 skill 附属文件
    支持: scripts/, references/, assets/
    """
    # 安全检查：防止路径穿越
    resource = resource.strip().lstrip("/")
    if ".." in resource or resource.startswith("/"):
        return _xml_response("error", "Invalid resource path: path traversal detected")
    
    resource_path = os.path.join(skill_dir, resource)
    
    # 检查文件是否存在
    if not os.path.exists(resource_path):
        return _xml_response("error", f"Resource not found: {resource}")
    
    if not os.path.isfile(resource_path):
        return _xml_response("error", f"Resource is not a file: {resource}")
    
    # 检查文件大小（防止加载过大文件）
    file_size = os.path.getsize(resource_path)
    max_size = 100 * 1024  # 100KB
    if file_size > max_size:
        return _xml_response("error", f"Resource too large ({file_size // 1024}KB). Max: {max_size // 1024}KB")
    
    # 读取文件内容
    try:
        with open(resource_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return _xml_response("error", f"Failed to read resource: {e}")
    
    output = [
        f"<skill_resource name=\"{skill_obj.name}\" path=\"{resource}\">",
        f"# {resource}",
        "",
        content,
        "</skill_resource>"
    ]
    
    return _xml_response("done", "\n".join(output))

# 更新文档字符串
Skills.__doc__ = _get_skill_doc()

# 注册工具
Skills = register_tool(category="Agent", name_cn="Skills", risk_level="low")(Skills)
