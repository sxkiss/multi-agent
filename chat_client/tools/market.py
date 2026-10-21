"""市场查询工具：专家市场 / 技能市场 / MCP 市场

三个公开市场，均无需认证（实测 200）：
- 专家市场（429 个专家）：腾讯云 COS 直连
  https://acc-1258344699.cos.accelerate.myqcloud.com/workbuddy/expert-marketplace
  - 专家清单：expert_center.json（1.1MB）
  - 精选场景：featuredScenes.json
  - 专家提示词：plugins/{name}/agents/{name}.md
  - 头像：avatars/{Name}.png
- 技能市场（SkillHub）：https://api.skillhub.cn
  - /api/v1/categories（分类）
  - /api/v1/search?q=&limit=（搜索）
  - /api/v1/skills/{slug}（详情）
- MCP 市场：https://cnb.cool/codebuddy/mcp-market（公开）

注：企业网关 /portal/**、/console/** 需 Bearer token，本工具不涉及。

@tool-as-app: 市场发现（只读查询）
@output: 按需返回专家/技能清单、提示词内容或市场概览
@position: Tools layer — 外部市场发现（与本地 Skills 目录互补）
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from . import register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

EXPERT_BASE = "https://acc-1258344699.cos.accelerate.myqcloud.com/workbuddy/expert-marketplace"
SKILLHUB_BASE = "https://api.skillhub.cn"
MCP_MARKET = "https://cnb.cool/codebuddy/mcp-market"

REQUEST_TIMEOUT = 20

# 专家清单缓存：1.1MB，避免每次重复拉取
_expert_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_EXPERT_TTL = 1800  # 30 分钟


def _get_json(url: str) -> Any:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _load_experts(force: bool = False) -> List[Dict[str, Any]]:
    """拉取专家清单（带缓存，TTL 30 分钟）。"""
    now = time.time()
    if not force and _expert_cache["data"] is not None and now - _expert_cache["ts"] < _EXPERT_TTL:
        return _expert_cache["data"]
    data = _get_json(f"{EXPERT_BASE}/expert_center.json")
    items = data if isinstance(data, list) else (data.get("experts") or data.get("list") or [])
    _expert_cache["data"] = items
    _expert_cache["ts"] = now
    return items


def _expert_field(expert: Dict[str, Any], *keys: str) -> str:
    """提取专家字段，支持多语言对象（{en, zh}）。优先中文，其次英文。"""
    for k in keys:
        v = expert.get(k)
        if isinstance(v, dict):
            for lang in ("zh", "zh_CN", "cn", "en"):
                if isinstance(v.get(lang), str) and v[lang].strip():
                    return v[lang].strip()
            # 都没有语言键：取第一个字符串值
            for val in v.values():
                if isinstance(val, str) and val.strip():
                    return val.strip()
        elif isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _expert_id(expert: Dict[str, Any]) -> str:
    """专家标识（用于拼提示词路径 plugins/{agentName}/agents/{agentName}.md）。

    优先 agentName / plugin；回退到 id / displayName。"""
    return (_expert_field(expert, "agentName")
            or _expert_field(expert, "plugin")
            or _expert_field(expert, "id")
            or _expert_field(expert, "name", "Name", "key", "slug"))


@register_tool(category="市场", name_cn="查询专家市场", risk_level="low")
def ExpertMarket(keyword: str = "", category: str = "", limit: int = 20) -> str:
    """
    查询专家市场（429 个专家、15 个分类，腾讯云 COS 公开，无需认证）。

    **何时使用（主动查询，不要等用户给链接）**：
    - 用户提到"专家/角色/人设/ personas"、想找某个领域的专家提示词
    - 需要为任务挑选一个领域专家角色（如"营销专家""Python 专家"）
    - 用户问"有没有 XX 专家""专家市场里有什么"

    - keyword：按名称/描述/标签模糊匹配（留空返回全部或按分类）
    - category：分类过滤（留空不过滤）
    - limit：返回条数上限（默认 20）

    找到目标专家后，用 GetExpertPrompt(name=...) 取完整提示词（.md 原文）。
    """
    try:
        experts = _load_experts()
    except Exception as e:
        return _xml_response("error", f"拉取专家清单失败: {e!s}")

    kw = (keyword or "").strip().lower()
    cat = (category or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    for ex in experts:
        if not isinstance(ex, dict):
            continue
        if cat:
            c = _expert_field(ex, "category", "categoryName", "group", "type").lower()
            tags = " ".join(str(t) for t in (ex.get("tags") or [])).lower()
            if cat not in c and cat not in tags:
                continue
        if kw:
            blob = " ".join([
                _expert_field(ex, "name", "Name", "title", "displayName"),
                _expert_field(ex, "description", "desc", "summary", "intro"),
                " ".join(str(t) for t in (ex.get("tags") or [])),
            ]).lower()
            if kw not in blob:
                continue
        hits.append(ex)
        if len(hits) >= max(1, int(limit or 20)):
            break

    if not hits:
        return _xml_response("done", f"未匹配到专家（keyword={keyword!r} category={category!r}）。可留空 keyword 查看全部。")

    lines = [f"<!-- 专家市场：命中 {len(hits)} 个（共 {len(experts)} 个专家） -->"]
    for i, ex in enumerate(hits, 1):
        name = _expert_field(ex, "displayName", "name", "Name", "title", "profession")
        desc = _expert_field(ex, "description", "desc", "summary", "intro")[:120]
        catg = _expert_field(ex, "categoryId", "categoryName", "category", "group", "type")
        lines.append(f"{i}. **{name}**" + (f" [{catg}]" if catg else ""))
        if desc:
            lines.append(f"   {desc}")
    lines.append("\n用 GetExpertPrompt(name=\"专家标识\") 获取该专家的完整提示词。")
    return _xml_response("done", "\n".join(lines))


def fetch_expert_prompt(name: str) -> tuple[str, str]:
    """拉取专家提示词原文（供 GetExpertPrompt / InstallExpert 复用）。

    返回 (key, text)；失败时抛异常，由调用方转成用户可读错误。
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("需要提供 name（专家标识）")
    # 若传的是完整路径（如 /plugins/content-creator/agents/...），提取相对路径
    prompt_path = key
    if key.startswith("/"):
        prompt_path = key.lstrip("/")
    elif key.startswith(EXPERT_BASE):
        prompt_path = key[len(EXPERT_BASE):].lstrip("/")
    resp = requests.get(f"{EXPERT_BASE}/{prompt_path}", timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        # 回退：直接用 key 拼标准路径
        fallback = f"{EXPERT_BASE}/plugins/{key}/agents/{key}.md"
        resp = requests.get(fallback, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(
            f"未取到专家提示词（HTTP {resp.status_code}）：{key}。"
            "可先用 ExpertMarket 确认准确的 name，或传完整 promptFile 路径。"
        )
    return key, resp.text


def find_expert_meta(name: str) -> Dict[str, Any]:
    """在专家清单中按标识/名称查找专家元数据（找不到返回空 dict）。"""
    target = (name or "").strip().lower()
    if not target:
        return {}
    try:
        experts = _load_experts()
    except Exception:
        return {}
    for ex in experts:
        if not isinstance(ex, dict):
            continue
        if _expert_id(ex).lower() == target:
            return ex
        nm = _expert_field(ex, "displayName", "name", "Name", "title").lower()
        if nm and nm == target:
            return ex
    return {}


@register_tool(category="市场", name_cn="获取专家提示词", risk_level="low")
def GetExpertPrompt(name: str) -> str:
    """
    获取专家市场里某个专家的完整提示词（.md 原文，无需认证）。

    - name：专家标识（来自 ExpertMarket 结果，如 "marketing-expert"）
    路径规则：{base}/plugins/{name}/agents/{name}.md

    提示：若希望把该专家**落地为可复用的团队成员**（而非每次临时取提示词），
    请改用 InstallExpert(name=...) —— 它会写入 crew_agents.json 并立即可派发。
    """
    if not (name or "").strip():
        return _xml_response("error", "需要提供 name（专家标识）")
    try:
        key, text = fetch_expert_prompt(name)
    except ValueError as e:
        return _xml_response("error", str(e))
    except Exception as e:
        return _xml_response("error", f"获取专家提示词失败: {e!s}")
    try:
        from . import PROJECT_ROOT
        from ..experts import get_catalog
        meta = find_expert_meta(key) or find_expert_meta(name)
        get_catalog(PROJECT_ROOT).cache_market_expert(key, text, meta or {})
    except Exception as _cache_err:
        logger.debug(f"专家缓存落盘失败（不影响返回）: {_cache_err!s}")
    if len(text) > 30000:
        text = text[:30000] + "\n\n[...提示词过长已截断...]"
    return _xml_response("done", f"<!-- 专家: {key} -->\n{text}")


@register_tool(category="市场", name_cn="安装专家为成员", risk_level="medium", timeout=90)
def InstallExpert(name: str, department: str = "experts", member_name: str = "",
                  tools: Optional[List[str]] = None, identity: str = "") -> str:
    """
    把专家市场的一个专家**落地为可复用的团队成员**（写入 crew_agents.json，立即生效）。
    与 GetExpertPrompt（临时取提示词）不同：安装后该专家成为集团常驻成员，
    可通过 Task(subagent_type=成员名) 或 RunCrew 反复调用，无需每次重新取提示词。

    **何时使用**：
    - 用户说"把 XX 专家加进团队""招一个营销专家""以后都用这个专家"
    - 某个领域会反复用到同一专家（省去每次 ExpertMarket→GetExpertPrompt 的开销）

    Args:
        name: 专家市场标识（来自 ExpertMarket，如 "marketing-expert"）
        department: 目标部门标识（默认 experts；不存在会自动创建「专家部」）
        member_name: 落地后的成员名（留空则用专家标识规范化生成）
        tools: 授予该成员的工具名列表（留空则用一套通用只读+读写默认集）
        identity: 可选身份补充（名字/定位/风格），会作为身份层拼进成员系统提示词
    """
    import re as _re
    # 0. 优先从**本地专家库**（自动发现）取——本地专家无需联网、含完整人格文件
    local = None
    try:
        from ..experts import get_catalog
        from . import PROJECT_ROOT
        cat = get_catalog(PROJECT_ROOT)
        local = cat.get(name)
    except Exception:
        local = None

    if local is not None:
        key = local.id
        display = local.label
        desc = local.description
        category = local.category
        # 组装 backstory：SOUL + IDENTITY 作为系统提示
        prompt_text = (local.soul or "").strip()
        if local.identity:
            prompt_text += "\n\n" + local.identity.strip()
        if not prompt_text:
            prompt_text = f"你是{display}。{desc}"
        # 本地专家可携带推荐工具
        if not tools and local.tools:
            tools = list(local.tools)
        expert_skills = list(getattr(local, "skills", None) or [])
        expert_dir = str(getattr(local, "directory", "") or "")
    else:
        # 1. 取专家市场提示词 + 元数据
        try:
            key, prompt_text = fetch_expert_prompt(name)
        except ValueError as e:
            return _xml_response("error", str(e))
        except Exception as e:
            return _xml_response("error", f"获取专家提示词失败: {e!s}")

        meta = find_expert_meta(key) or find_expert_meta(name)
        display = _expert_field(meta, "displayName", "name", "Name", "title") if meta else ""
        desc = _expert_field(meta, "description", "desc", "summary", "intro") if meta else ""
        category = _expert_field(meta, "categoryName", "category", "group", "type") if meta else ""
        expert_skills = []
        expert_dir = ""

    # 2. 规范化成员名：优先 member_name，其次专家标识；非法字符转 -
    raw_member = (member_name or key or "").strip().lower()
    member = _re.sub(r"[^a-z0-9_\-]", "-", raw_member).strip("-")
    if not _re.match(r"^[a-z0-9_\-]{2,40}$", member or ""):
        return _xml_response("error", f"成员名不合法（规范化后为 {member!r}）。请用 member_name 指定一个仅含小写字母/数字/_/- 的名字。")
    dept = (department or "experts").strip().lower()

    # 3. 组装 backstory（系统提示词）：专家原文 + 可选身份层
    backstory = prompt_text.strip()
    if len(backstory) > 20000:
        backstory = backstory[:20000] + "\n\n[...专家提示词过长已截断...]"
    id_line = ""
    if identity.strip():
        id_line = f"\n\n[身份设定]\n{identity.strip()}"
        backstory += id_line

    # 4. 工具集：留空给一套通用默认（只读为主 + 常用执行）
    default_tools = [
        "Read", "Glob", "Grep", "Write", "Edit", "Bash",
        "WebSearch", "WebFetch", "TodoWrite", "Skills",
    ]
    grant = tools if tools else default_tools

    # 5. 落地：复用 task.py 的部门/成员持久化逻辑（延迟导入避免循环依赖）
    from .task import _CREW_LOCK, _crew_load_file, _crew_save_file, reload_custom_agents
    with _CREW_LOCK:
        data = _crew_load_file()
        # 自动建部门
        depts = {d.get("name") for d in data["departments"]}
        dept_created = False
        if dept not in depts:
            if len(data["departments"]) >= 20:
                return _xml_response("error", "部门数量已达上限（20），无法自动创建 experts 部。请先指定一个已存在的 department。")
            data["departments"].append({
                "name": dept,
                "title": "专家部" if dept == "experts" else dept,
                "description": "由专家市场安装的领域专家成员",
            })
            dept_created = True
        # 成员查重
        if any(a.get("name") == member for a in data["agents"]):
            return _xml_response("error", f"成员已存在: {member}（可用 member_name 换名，或先卸载）")
        if len(data["agents"]) >= 50:
            return _xml_response("error", "成员数量已达上限（50）")
        # 工具白名单过滤
        from . import registry as _tool_registry
        known = set(_tool_registry._metadata.keys()) | {
            s["function"]["name"] for s in _tool_registry._schemas
        }
        granted = [t for t in grant if t in known]
        dropped = [t for t in grant if t not in known]
        if not granted:
            sample = ", ".join(sorted(known)[:15])
            return _xml_response("error", f"tools 全部无效。系统真实可用工具例如：{sample}。")
        data["agents"].append({
            "name": member,
            "department": dept,
            "description": (desc or f"专家市场安装的专家（{key}）")[:500],
            "backstory": backstory[:20000],
            "tools": granted[:40],
            "model": "",
            "expert_source": key,          # 溯源：记录来源专家标识
            "expert_category": category[:80],
            "identity": identity.strip()[:2000],
            "skills": expert_skills,        # 专家自带技能（随专家目录注入子 agent）
            "expert_dir": expert_dir,       # 专家目录（含 skills/），供 load_agent_skills_block 读取
        })
        _crew_save_file(data)

    reload_custom_agents()

    note = f"（未识别工具已忽略: {dropped}）" if dropped else ""
    dept_note = "（已自动创建专家部）" if dept_created else ""
    return _xml_response(
        "done",
        f"专家「{display or key}」已安装为团队成员：**{member}**（部门 {dept}）{dept_note}\n"
        f"授予工具：{', '.join(granted)} {note}\n"
        f"立即生效：可用 Task(subagent_type='{member}') 派发任务，或在 RunCrew 计划中作为成员调用。\n"
        f"来源专家标识：{key}（记录于 crew_agents.json 的 expert_source 字段，便于溯源/更新）。",
    )


@register_tool(category="市场", name_cn="查询技能市场", risk_level="low")
def SkillMarket(keyword: str = "", category: str = "", limit: int = 20) -> str:
    """
    查询技能市场（SkillHub，api.skillhub.cn，公开接口无需认证）。

    **何时使用（主动查询）**：
    - 本地已装 400 个技能仍不够，需要找外部新技能
    - 用户问"有没有 XX 技能/插件""技能市场里有什么"
    - 想看看某领域（python/excel/seo...）社区有哪些现成技能

    - keyword：搜索关键词（走 /api/v1/search）
    - category：分类过滤（走 /api/v1/categories 后本地过滤）
    - limit：返回条数上限（默认 20）
    """
    try:
        if keyword.strip():
            data = _get_json(f"{SKILLHUB_BASE}/api/v1/search?q={requests.utils.quote(keyword)}&limit={max(1, int(limit))}")
            items = data if isinstance(data, list) else (data.get("skills") or data.get("items") or data.get("results") or [])
        else:
            params = f"page=1&pageSize={max(1, int(limit))}"
            if category.strip():
                params += f"&category={requests.utils.quote(category)}"
            data = _get_json(f"{SKILLHUB_BASE}/api/skills?{params}")
            items = data if isinstance(data, list) else (data.get("skills") or data.get("items") or data.get("data") or [])
    except Exception as e:
        return _xml_response("error", f"技能市场查询失败: {e!s}")

    if not items:
        return _xml_response("done", f"未匹配到技能（keyword={keyword!r} category={category!r}）。")

    lines = [f"<!-- 技能市场：{len(items)} 条 -->"]
    for i, it in enumerate(items[: max(1, int(limit))], 1):
        if not isinstance(it, dict):
            continue
        nm = it.get("name") or it.get("slug") or it.get("title") or ""
        d = (it.get("description") or it.get("summary") or "")[:120]
        slug = it.get("slug") or ""
        lines.append(f"{i}. **{nm}**" + (f"  slug=`{slug}`" if slug else ""))
        if d:
            lines.append(f"   {d}")
    lines.append(f"\n技能市场 Web 端：https://skillhub.cn ｜ MCP 市场：{MCP_MARKET}")
    return _xml_response("done", "\n".join(lines))


@register_tool(category="市场", name_cn="查询技能分类", risk_level="low")
def SkillCategories() -> str:
    """列出技能市场（SkillHub）的全部分类（公开接口，无需认证）。"""
    try:
        data = _get_json(f"{SKILLHUB_BASE}/api/v1/categories")
    except Exception as e:
        return _xml_response("error", f"获取分类失败: {e!s}")
    cats = data if isinstance(data, list) else (data.get("categories") or data.get("data") or [])
    if not cats:
        return _xml_response("done", "未取到分类列表")
    lines = ["<!-- 技能市场分类 -->"]
    for c in cats:
        if isinstance(c, dict):
            nm = c.get("name") or c.get("label") or c.get("slug") or ""
            cnt = c.get("count") or c.get("skillCount") or ""
            lines.append(f"- {nm}" + (f"（{cnt}）" if cnt != "" else ""))
        else:
            lines.append(f"- {c}")
    return _xml_response("done", "\n".join(lines))


@register_tool(category="市场", name_cn="查询专家场景", risk_level="low")
def ExpertScenes() -> str:
    """列出专家市场的精选场景（featuredScenes.json，公开无需认证）。"""
    try:
        data = _get_json(f"{EXPERT_BASE}/featuredScenes.json")
    except Exception as e:
        return _xml_response("error", f"获取精选场景失败: {e!s}")
    items = data if isinstance(data, list) else (data.get("scenes") or data.get("list") or data.get("data") or [])
    if not items:
        return _xml_response("done", "未取到精选场景")
    lines = ["<!-- 专家市场：精选场景 -->"]
    for s in items:
        if isinstance(s, dict):
            nm = s.get("name") or s.get("title") or s.get("sceneName") or ""
            d = (s.get("description") or s.get("desc") or "")[:100]
            lines.append(f"- **{nm}**" + (f"：{d}" if d else ""))
        else:
            lines.append(f"- {s}")
    return _xml_response("done", "\n".join(lines))
