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
def ExpertMarket(action: str = "list", keyword: str = "", category: str = "", limit: int = 20, name: str = "") -> str:
    """
    查询专家市场（429 个专家、15 个分类，腾讯云 COS 公开，无需认证）。

    **何时使用（主动查询，不要等用户给链接）**：
    - 用户提到"专家/角色/人设/ personas"、想找某个领域的专家提示词
    - 需要为任务挑选一个领域专家角色（如"营销专家""Python 专家"）
    - 用户问"有没有 XX 专家""专家市场里有什么"

    - keyword：按名称/描述/标签模糊匹配（留空返回全部或按分类）
    - category：分类过滤（留空不过滤）
    - limit：返回条数上限（默认 20）

    找到目标专家后，用 ExpertMarket(action="prompt", name=...) 取完整提示词（.md 原文）。
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
        "\n下一步：用 `ExpertMarket(action=\"install\", name=\"<上面反引号里的标识>\")` 直接落地为团队成员，"
        "再用 `Task(subagent_type=\"成员名\")` 派发；只需临时人设时用 `ExpertMarket(action=\"prompt\", name=\"<标识>\")`。"
    )
    return _xml_response("done", "\n".join(lines))


    # action分支
    act = (action or "list").strip().lower()
    if act == "prompt":
        if not name:
            return _xml_response("error", "action=prompt需要name参数")
        try:
            key, text = fetch_expert_prompt(name)
            return _xml_response("done", text[:5000])
        except Exception as e:
            return _xml_response("error", f"获取提示词失败: {e!s}")
    if act == "install":
        if not name:
            return _xml_response("error", "action=install需要name参数")
        return InstallExpert(name=name)
    if act == "scenes":
        return ExpertScenes()
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


@register_tool(category="市场", name_cn="查询技能市场", risk_level="low")
def SkillMarket(action: str = "search", keyword: str = "", category: str = "", limit: int = 20, package_slug: str = "") -> str:
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
    # action分支
    act = (action or "search").strip().lower()
    if act == "packages":
        return SkillPackageMarket(keyword=keyword, category=category, limit=limit)
    if act == "list":
        local = _local_skill_names(999)
        if not local:
            return _xml_response("done", "当前无已装技能。")
        lines = [f"本地已装技能（共{len(local)}个）："]
        for s in sorted(local)[:50]:
            lines.append(f"  - {s}")
        return _xml_response("done", "\n".join(lines))
    if act == "categories":
        return SkillCategories()
    if act == "install":
        if not package_slug:
            return _xml_response("error", "action=install需要package_slug参数")
        return InstallSkillPackage(package_slug=package_slug)
    return _xml_response("done", "\n".join(lines))


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


