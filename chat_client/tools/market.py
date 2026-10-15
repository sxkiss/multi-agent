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


@register_tool(category="市场", name_cn="获取专家提示词", risk_level="low")
def GetExpertPrompt(name: str) -> str:
    """
    获取专家市场里某个专家的完整提示词（.md 原文，无需认证）。

    - name：专家标识（来自 ExpertMarket 结果，如 "marketing-expert"）
    路径规则：{base}/plugins/{name}/agents/{name}.md
    """
    if not (name or "").strip():
        return _xml_response("error", "需要提供 name（专家标识）")
    key = name.strip()
    # 若传的是完整路径（如 /plugins/content-creator/agents/...），提取相对路径
    prompt_path = key
    if key.startswith("/"):
        prompt_path = key.lstrip("/")
    elif key.startswith(EXPERT_BASE):
        prompt_path = key[len(EXPERT_BASE):].lstrip("/")
    try:
        resp = requests.get(f"{EXPERT_BASE}/{prompt_path}", timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            # 回退：直接用 key 拼标准路径
            fallback = f"{EXPERT_BASE}/plugins/{key}/agents/{key}.md"
            resp = requests.get(fallback, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return _xml_response("error", f"未取到专家提示词（HTTP {resp.status_code}）：{key}。可先用 ExpertMarket 确认准确的 name，或传完整 promptFile 路径。")
        text = resp.text
    except Exception as e:
        return _xml_response("error", f"获取专家提示词失败: {e!s}")
    if len(text) > 30000:
        text = text[:30000] + "\n\n[...提示词过长已截断...]"
    return _xml_response("done", f"<!-- 专家: {key} -->\n{text}")


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
