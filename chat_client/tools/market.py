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
import os
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

# ============================================================
# 统一市场缓存层（内存 + 落盘）
#
# 设计：
# - 每个市场维护一个 _xx_cache（内存）+ 一个磁盘 JSON（跨重启）
# - 加载时先查内存（快），再查磁盘（跨重启），最后才联网
# - 联网成功后同时刷新内存 + 磁盘；失败不清旧缓存
# - 专家市场已用 _expert_cache（30min），这里保留；新增落盘
# - SkillMarket/SkillCategories/ExpertScenes 原每次联网，补全缓存
# ============================================================

_MARKET_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "market_cache",
)
_MARKET_CACHE_TTL = 3600  # 1 小时（统一 TTL，可按需调）
_expert_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
_EXPERT_TTL = 1800  # 30 分钟（保留原值）


def _market_cache_path(key: str) -> str:
    return os.path.join(_MARKET_CACHE_DIR, f"{key}.json")


def _market_cache_load(key: str, ttl: int = _MARKET_CACHE_TTL) -> Any:
    """尝试从内存/磁盘加载市场缓存；命中返回数据，否则 None。"""
    global _market_cache_state
    now = time.time()
    # 1) 内存
    mem = _market_cache_state.get(key, {})
    if mem.get("data") is not None and now - mem.get("ts", 0) < ttl:
        return mem["data"]
    # 2) 磁盘
    path = _market_cache_path(key)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved_at = data.get("saved_at", 0)
            if now - float(saved_at) <= ttl:
                # 回写内存
                _market_cache_state[key] = {"ts": now, "data": data.get("data")}
                return data.get("data")
    except Exception:
        pass
    return None


def _market_cache_save(key: str, data: Any) -> None:
    """保存市场缓存到内存 + 磁盘（原子写）。失败静默。"""
    global _market_cache_state
    now = time.time()
    _market_cache_state[key] = {"ts": now, "data": data}
    try:
        os.makedirs(_MARKET_CACHE_DIR, exist_ok=True)
        payload = {"data": data, "saved_at": now}
        tmp = _market_cache_path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _market_cache_path(key))
    except Exception:
        logger.debug(f"市场缓存写入失败 {key}", exc_info=True)


# 内存缓存全局状态（key -> {ts, data}）
_market_cache_state: Dict[str, Dict[str, Any]] = {}


def _get_json(url: str) -> Any:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _load_experts(force: bool = False) -> List[Dict[str, Any]]:
    """拉取专家清单（带缓存：内存 30min + 落盘 1h）。"""
    if not force:
        cached = _market_cache_load("experts", _EXPERT_TTL)
        if cached is not None:
            _expert_cache["data"] = cached
            _expert_cache["ts"] = time.time()
            return cached
    try:
        data = _get_json(f"{EXPERT_BASE}/expert_center.json")
    except Exception:
        # 网络失败：若有旧缓存（内存/磁盘），降级返回旧数据
        fallback = _market_cache_load("experts", ttl=86400)
        if fallback is not None:
            logger.warning("专家市场联网失败，降级使用旧缓存")
            return fallback
        raise
    items = data if isinstance(data, list) else (data.get("experts") or data.get("list") or [])
    _expert_cache["data"] = items
    _expert_cache["ts"] = time.time()
    _market_cache_save("experts", items)
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




def _local_skill_names(limit: int = 20) -> list[str]:
    """返回本地已安装技能名（前 limit 个），供市场工具补充本地信息。"""
    try:
        from chat_client.skills import skill_manager
        return [s.name for s in skill_manager.all_enabled()][:limit]
    except Exception:
        return []


def _local_mcp_servers(limit: int = 20) -> list[str]:
    """返回本地已配置 MCP server 名，供 MCP 市场工具补充本地信息。"""
    import os as _os
    _cfg_path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "mcp", "config.json")
    try:
        with open(_cfg_path, "r", encoding="utf-8") as f:
            _data = __import__("json").load(f)
        return list(_data.get("mcpServers", {}).keys())[:limit]
    except Exception:
        return []


def _local_packages(limit: int = 10) -> list[str]:
    """返回本地 packages/ 目录中已安装专家包（zip 文件名）。"""
    import os as _os
    _pkg_dir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))), "packages")
    if not _os.path.isdir(_pkg_dir):
        return []
    return sorted(f.replace(".zip", "") for f in _os.listdir(_pkg_dir) if f.endswith(".zip"))[:limit]


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
        # 必须输出**专家标识**：InstallExpert/GetExpertPrompt 只认 slug，
        # 只给中文显示名会导致「查到却装不上」，链路在此断掉。
        eid = _expert_id(ex)
        desc = _expert_field(ex, "description", "desc", "summary", "intro")[:120]
        catg = _expert_field(ex, "categoryId", "categoryName", "category", "group", "type")
        head = f"{i}. **{name}**"
        if eid and eid != name:
            head += f" `{eid}`"
        lines.append(head + (f" [{catg}]" if catg else ""))
        if desc:
            lines.append(f"   {desc}")
    lines.append(
        "\n下一步：用 `InstallExpert(name=\"<上面反引号里的标识>\")` 直接落地为团队成员，"
        "再用 `Task(subagent_type=\"成员名\")` 派发；只需临时人设时用 `GetExpertPrompt(name=\"<标识>\")`。"
    )
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

    - keyword：搜索关键词（走 /api/v1/search，响应无 total）
    - category：分类过滤（走 /api/skills?page=1&pageSize=N[&category=]，响应 data.total 为在线总数）
    - limit：返回条数上限（默认 20）
    """
    try:
        # 带缓存：搜索关键词（缓存 key = "search_{keyword}_{limit}"，TTL 1h）
        if keyword.strip():
            cache_key = f"skill_search_{keyword.strip().lower()[:50]}_{limit}"
            cached = _market_cache_load(cache_key)
            if cached is not None:
                data = cached
            else:
                data = _get_json(f"{SKILLHUB_BASE}/api/v1/search?q={requests.utils.quote(keyword)}&limit={max(1, int(limit))}")
                _market_cache_save(cache_key, data)
            items = data if isinstance(data, list) else (data.get("skills") or data.get("items") or data.get("results") or [])
            online_total = 0  # 端点 A 无 total 字段
        else:
            # 分类浏览（缓存 key = "skills_list_{category}_{limit}"）
            cache_key = f"skill_list_{category.strip().lower()[:30]}_{limit}"
            cached = _market_cache_load(cache_key)
            if cached is not None:
                data = cached
            else:
                params = f"page=1&pageSize={max(1, int(limit))}"
                if category.strip():
                    params += f"&category={requests.utils.quote(category)}"
                data = _get_json(f"{SKILLHUB_BASE}/api/skills?{params}")
                _market_cache_save(cache_key, data)
            # 端点 B 返回 {"code":0,"data":{"skills":[...],"total":N}}，嵌套两层 data
            inner = data.get("data") if isinstance(data, dict) else data
            total = inner.get("total", 0) if isinstance(inner, dict) else 0
            items = inner.get("skills") or [] if isinstance(inner, dict) else []
            online_total = total
    except Exception as e:
        return _xml_response("error", f"技能市场查询失败: {e!s}")

    if not items and online_total == 0:
        return _xml_response("done", f"未匹配到技能（keyword={keyword!r} category={category!r}）。")

    local_set = set(_local_skill_names(999))
    # 从本地技能中筛出匹配 kw 的
    local_hits = []
    if local_set and (keyword.strip() or category.strip()):
        kw = (keyword or "").strip().lower()
        cat = (category or "").strip().lower()
        from chat_client.skills import skill_manager as _sm
        for sk in _sm.all_enabled():
            nm = sk.name
            desc = (sk.description or "").lower()
            blob = f"{nm} {desc}"
            if kw and kw not in blob:
                continue
            if cat and cat not in blob:
                continue
            local_hits.append({"name": nm, "slug": nm, "description": sk.description})
    lines = []
    if local_hits:
        lines.append(f"<!-- 本地已装技能：命中 {len(local_hits)} 个 -->")
        for idx, lh in enumerate(local_hits[:limit], 1):
            nm = lh.get("name") or lh.get("slug") or ""
            d = (lh.get("description") or "")[:150]
            lines.append(f"{idx}. **{nm}** 📦本地已装")
            if d:
                lines.append(f"   {d}")
        if len(local_hits) > limit:
            lines.append(f"  ...等，共 {len(local_hits)} 个本地匹配项")
        lines.append("")

    if keyword.strip():
        lines.append(f"<!-- 技能市场：{len(items)} 条 -->")
    elif category.strip():
        lines.append(f"<!-- 技能市场[{category}]：{len(items)} 条（该分类在线共 {online_total} 个） -->")
    else:
        lines.append(f"<!-- 技能市场：{len(items)} 条（在线共 {online_total} 个技能） -->")
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
        # 带缓存（key="categories"，TTL 1h）
        cached = _market_cache_load("categories")
        if cached is not None:
            data = cached
        else:
            data = _get_json(f"{SKILLHUB_BASE}/api/v1/categories")
            _market_cache_save("categories", data)
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
        # 带缓存（key="scenes"，TTL 1h）
        cached = _market_cache_load("scenes")
        if cached is not None:
            data = cached
        else:
            data = _get_json(f"{EXPERT_BASE}/featuredScenes.json")
            _market_cache_save("scenes", data)
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


# ============================================================
# 专家包市场（SkillHub SkillSets）
#
# 数据源：
# - 列表：https://api.skillhub.cn/api/v1/skillsets
#   响应 {"skillSets":[{slug,displayName,summary,scene,subScene,skillSlugs,...}], "total":N}
# - 单包详情：https://api.skillhub.cn/api/v1/skillsets/{slug}
#   响应含 content（YAML frontmatter，orchestration.children 为 skill slug 列表）
# - 单技能详情：https://api.skillhub.cn/api/v1/skills/{slug}
#   响应含 skill.sourceUrl / skill.upstream_url 等链接
# ============================================================

SKILLHUB_SKILLSETS_BASE = f"{SKILLHUB_BASE}/api/v1/skillsets"
SKILLHUB_SKILL_BASE = f"{SKILLHUB_BASE}/api/v1/skills"


def _parse_yaml_front_matter(text: str) -> Dict[str, Any]:
    """粗解析 YAML frontmatter（--- 包裹的头部），返回 dict。"""
    if not text:
        return {}
    import re as _re
    m = _re.match(r"^---\s*\n(.*?)\n---", text, _re.S)
    if not m:
        return {}
    yaml_block = m.group(1)
    result: Dict[str, Any] = {}
    # 逐行解析 key: value / key:>-\n  多行 / key:\n  - item
    current_key = None
    for raw_line in yaml_block.splitlines():
        line = raw_line.rstrip()
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 列表项（属于上一个 key）
        if stripped.startswith("- ") and current_key:
            item = stripped[2:].strip()
            if isinstance(result[current_key], list):
                result[current_key].append(item)
            else:
                result[current_key] = [item]
            continue
        # 普通 key: value
        if ":" in stripped:
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            # 跳过嵌套对象（非顶层需求）
            if indent > 0 and current_key:
                continue
            if not v or v.startswith((">", "|")):
                result[k] = []
                current_key = k
            else:
                result[k] = v.strip()
                current_key = k
    return result


def _skillset_children(skillset: Dict[str, Any]) -> List[str]:
    """从 skillset 对象/详情中提取技能 slug 列表。"""
    # 列表接口直接带 skillSlugs
    slugs = skillset.get("skillSlugs") or []
    if slugs:
        return [str(s) for s in slugs]
    # 详情接口：从 content 的 YAML frontmatter 提取 orchestration.children
    content = skillset.get("content") or ""
    meta = _parse_yaml_front_matter(content)
    orch = meta.get("orchestration")
    if isinstance(orch, dict):
        children = orch.get("children") or []
        if isinstance(children, list):
            return [str(c) for c in children]
    return []


@register_tool(category="市场", name_cn="查询专家包市场", risk_level="low")
def SkillPackageMarket(keyword: str = "", category: str = "", limit: int = 20) -> str:
    """
    查询专家包市场（SkillHub SkillSets，api.skillhub.cn，公开接口无需认证）。

    **何时使用（主动查询）**：
    - 用户想要"一整套工作流"而非单个技能（如「自动化测试专家包」「数据分析专家包」）
    - 需要为任务挑选一个专家包（meta-skill，内含多个子技能的编排流程）

    - keyword：按 displayName/slug/summary 本地模糊过滤
    - category：按 scene/subScene 本地过滤
    - limit：返回条数上限（默认 20）
    """
    try:
        cache_key = f"skill_package_{keyword.strip().lower()[:30]}_{category.strip().lower()[:30]}_{limit}"
        cached = _market_cache_load(cache_key)
        if cached is not None:
            data = cached
        else:
            data = _get_json(SKILLHUB_SKILLSETS_BASE + "?pageSize=100")
            _market_cache_save(cache_key, data)
    except Exception as e:
        return _xml_response("error", f"拉取专家包市场失败: {e!s}")

    items = data if isinstance(data, list) else (data.get("skillSets") or data.get("items") or data.get("results") or [])
    if not items:
        return _xml_response("done", "未取到专家包列表")

    kw = (keyword or "").strip().lower()
    cat = (category or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    for pkg in items:
        if not isinstance(pkg, dict):
            continue
        if cat:
            scene = " ".join([
                str(pkg.get("scene") or ""),
                str(pkg.get("subScene") or ""),
                str(pkg.get("displayNameEn") or ""),
            ]).lower()
            if cat not in scene:
                continue
        if kw:
            blob = " ".join([
                str(pkg.get("displayName") or ""),
                str(pkg.get("displayNameEn") or ""),
                str(pkg.get("slug") or ""),
                str(pkg.get("summary") or ""),
                str(pkg.get("summaryEn") or ""),
            ]).lower()
            if kw not in blob:
                continue
        hits.append(pkg)
        if len(hits) >= max(1, min(int(limit or 20), 100)):
            break

    if not hits:
        return _xml_response("done", f"未匹配到专家包（keyword={keyword!r} category={category!r}）。可留空 keyword 查看全部。")

    local_pkgs = _local_packages(10)
    lines = []
    if local_pkgs:
        lines.append(f"<!-- 本地已装专家包 {len(local_pkgs)} 个 -->\n" + "\n".join(f"- {n}" for n in local_pkgs))
        lines.append("")
    lines.append(f"<!-- 专家包市场：命中 {len(hits)} 个 -->")
    for i, pkg in enumerate(hits, 1):
        name = pkg.get("displayName") or pkg.get("slug") or f"包{i}"
        slug = pkg.get("slug") or ""
        scene = pkg.get("scene") or ""
        sub = pkg.get("subScene") or ""
        summary = str(pkg.get("summary") or pkg.get("summaryEn") or "")[:150]
        child_slugs = _skillset_children(pkg)[:6]
        lines.append(f"{i}. **{name}** `{slug}`" + (f" [{scene}/{sub}]" if scene else ""))
        if summary:
            lines.append(f"   {summary}")
        if child_slugs:
            lines.append(f"   技能: {'、'.join(child_slugs)}")
    lines.append("\n下一步：用 `InstallSkillPackage(package_slug=\"<上面反引号里的 slug>\")` 安装专家包。")
    return _xml_response("done", "\n".join(lines))


@register_tool(category="市场", name_cn="安装专家包", risk_level="medium")
def InstallSkillPackage(package_slug: str = "") -> str:
    """
    安装 SkillHub 专家包（meta-skill）。

    从 /api/v1/skillsets/{slug} 获取包详情（含 skillSlugs），
    对每个技能调用 /api/v1/skills/{slug} 获取详情并尝试取 zipUrl/downloadUrl 下载链接。
    若无直接下载链接，输出提示从官网手动下载安装。

    - package_slug：专家包标识（SkillPackageMarket 输出中的 slug）
    """
    slug = (package_slug or "").strip().strip("/")
    if not slug:
        return _xml_response("error", "需要提供 package_slug（专家包标识，如 tech-test-automation）")

    # 1. 获取包详情（不带缓存，安装需要最新数据）
    try:
        detail = _get_json(f"{SKILLHUB_SKILLSETS_BASE}/{slug}")
    except Exception as e:
        return _xml_response("error", f"获取专家包详情失败: {e!s}")
    if not isinstance(detail, dict) or detail.get("error"):
        return _xml_response("error", f"未找到专家包: {slug}")

    pkg_name = detail.get("displayName") or slug
    child_slugs = _skillset_children(detail)
    if not child_slugs:
        return _xml_response("error", f"专家包 {slug} 未包含任何技能（skillSlugs 为空）。")

    lines = [f"<!-- 安装专家包：{pkg_name}（{slug}） -->"]
    lines.append(f"专家包包含 {len(child_slugs)} 个技能，逐个获取下载链接：")

    installed = 0
    no_link = 0
    for i, skill_slug in enumerate(child_slugs, 1):
        skill_slug = str(skill_slug).strip()
        lines.append(f"\n[{i}/{len(child_slugs)}] **{skill_slug}**")
        try:
            sdata = _get_json(f"{SKILLHUB_SKILL_BASE}/{skill_slug}")
        except Exception as e:
            lines.append(f"   ❌ 获取详情失败: {e!s}")
            no_link += 1
            continue
        if not isinstance(sdata, dict):
            lines.append("   ❌ 响应格式异常")
            no_link += 1
            continue

        # 查找下载链接：zipUrl / downloadUrl / sourceUrl / upstream_url
        skill = sdata.get("skill") if isinstance(sdata.get("skill"), dict) else sdata
        url = ""
        for k in ("zipUrl", "downloadUrl", "download_url", "zip_url"):
            v = skill.get(k) or sdata.get(k)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                url = v
                break
        if not url:
            for k in ("sourceUrl", "upstream_url", "homepage"):
                v = skill.get(k) or sdata.get(k)
                if isinstance(v, str) and v.startswith(("http://", "https://")):
                    url = v
                    break
        if url:
            display = skill.get("displayName") or sdata.get("displayName") or skill_slug
            lines.append(f"   - 技能名：{display}")
            lines.append(f"   - 下载链接：{url}")
            lines.append("   - 安装：用 `Skills(action=\"install\", source=\"<下载链接>\")` 将技能安装到本地 skills/ 目录。")
            installed += 1
        else:
            no_link += 1
            lines.append("   - 未找到直接下载链接，请手动安装。")

    lines.append(f"\n完成：{installed} 个技能找到下载链接，{no_link} 个需手动处理。")
    if no_link:
        lines.append(f"\n提示：请手动从 https://skillhub.cn/skillspackage/{slug} 下载后安装。")
    lines.append("\n安装后用 `Skills(action=\"list\")` 确认技能已就绪。")
    return _xml_response("done", "\n".join(lines))


# ============================================================
# MCP 服务器市场（Smithery）
#
# 数据源：https://api.smithery.ai/servers
# 响应 {"servers":[{qualifiedName, displayName, description, useCount, remote, ...}], ...}
# 支持 ?q= 搜索；本地也可按 name/description 过滤
# ============================================================

SMITHERY_BASE = "https://api.smithery.ai"


@register_tool(category="市场", name_cn="查询MCP市场", risk_level="low")
def McpMarket(keyword: str = "", limit: int = 20) -> str:
    """
    查询 MCP 服务器市场（Smithery，api.smithery.ai，公开接口无需认证）。

    **何时使用（主动查询）**：
    - 用户想找一个 MCP 服务器（如 GitHub、Slack、数据库等）
    - 需要了解当前 MCP 生态有哪些可用的服务器

    - keyword：按 name/description 本地模糊过滤（也走 Smithery ?q= 参数）
    - limit：返回条数上限（默认 20，最大 50）
    """
    try:
        n = max(1, min(int(limit or 20), 100))
        cache_key = f"mcp_market_{keyword.strip().lower()[:30]}_{n}"
        cached = _market_cache_load(cache_key)
        if cached is not None:
            data = cached
        else:
            url = f"{SMITHERY_BASE}/servers?limit={n}"
            if keyword.strip():
                url += f"&q={requests.utils.quote(keyword.strip())}"
            data = _get_json(url)
            _market_cache_save(cache_key, data)
    except Exception as e:
        return _xml_response("error", f"拉取 MCP 市场失败: {e!s}")

    items = data if isinstance(data, list) else (data.get("servers") or data.get("items") or data.get("results") or [])
    if not items:
        return _xml_response("done", "未取到 MCP 服务器列表")

    kw = (keyword or "").strip().lower()
    hits: List[Dict[str, Any]] = []
    for s in items:
        if not isinstance(s, dict):
            continue
        if kw:
            blob = " ".join([
                str(s.get("qualifiedName") or ""),
                str(s.get("displayName") or ""),
                str(s.get("name") or ""),
                str(s.get("description") or ""),
            ]).lower()
            if kw not in blob:
                continue
        hits.append(s)
        if len(hits) >= n:
            break

    if not hits:
        return _xml_response("done", f"未匹配到 MCP 服务器（keyword={keyword!r}）。可留空 keyword 查看全部。")

    local_mcp = _local_mcp_servers(10)
    lines = []
    if local_mcp:
        lines.append(f"<!-- 本地已配置 MCP server {len(local_mcp)} 个 -->\n" + "\n".join(f"- {n}" for n in local_mcp))
        lines.append("")
    lines.append(f"<!-- MCP 市场：命中 {len(hits)} 个 -->")
    for i, s in enumerate(hits, 1):
        name = s.get("displayName") or s.get("qualifiedName") or s.get("name") or f"服务器{i}"
        qname = s.get("qualifiedName") or s.get("id") or ""
        desc = str(s.get("description") or "")[:150]
        use = s.get("useCount") or s.get("downloads") or 0
        gh = s.get("githubUrl") or ""
        if not gh:
            # 由 qualifiedName 拼出可能存在的 GitHub 仓库链接
            qn = str(qname or "")
            if "/" in qn:
                gh = f"https://github.com/{qn}"
        head = f"{i}. **{name}**"
        if qname and qname != name:
            head += f" `{qname}`"
        lines.append(head + (f"（下载量 {use}）" if use else ""))
        if desc:
            lines.append(f"   {desc}")
        if gh:
            lines.append(f"   GitHub: {gh}")
    lines.append("\n下一步：用 `InstallMcpServer(server_slug=\"<上面反引号里的 qualifiedName>\")` 查看安装步骤。")
    return _xml_response("done", "\n".join(lines))


# ============================================================
# WorkBuddy 连接器市场（443 个连接器，已集成进 BT connector_hub）
#
# 数据源：https://static.workbuddy.cn/connectors-config-v2/connectors-config.zip
# 已导入 BT catalog（connector_hub/catalog.json），本组工具直接读本地 catalog：
# - WBConnectorMarket：搜索/分类浏览
# - WBConnectorDetail：查看单个连接器详情（含 mcp.json 与凭据要求）
# - WBConnectorInstall：在 BT catalog 中创建实例（无凭据直接安装，需凭据提示用户）
# ============================================================

_CATALOG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "connector_hub",
    "catalog.json",
)


