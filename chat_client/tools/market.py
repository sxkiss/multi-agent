"""市场查询工具：专家市场 / 技能市场 / MCP 市场

三个公开市场，均无需认证（实测 200）：
- 专家市场（429 个专家）：腾讯云 COS，3 个镜像自动降级
  https://acc-1258344699.cos.accelerate.myqcloud.com/workbuddy/expert-marketplace
  - 专家清单：expert_center.json（1.1MB）
  - 精选场景：featuredScenes.json
  - 专家提示词：早期散装 plugins/{name}/agents/{name}.md，新增版整包
    bundles/{plugin}.tar.gz（包内 agents/*.md + skills/）
  - 头像：avatars/{Name}.png
  - 另聚合 2 个第三方公开市场仓库（.codebuddy-plugin/marketplace.json），
    失败静默跳过、不拖累主源
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

import io
import json
import logging
import os
import posixpath
import re
import tarfile
import time
from typing import Any, Dict, List, Optional

import requests

from . import register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

EXPERT_BASE = "https://acc-1258344699.cos.accelerate.myqcloud.com/workbuddy/expert-marketplace"
# 2026-11-09 多源：同源多镜像（实测三者对清单/场景/散装md/bundle 均 200）。
# 任一镜像挂掉自动降级到下一个；EXPERT_BASE 保留为首选，兼容既有引用。
EXPERT_BASES = [
    EXPERT_BASE,
    "https://acc-1258344699.cos.ap-guangzhou.myqcloud.com/workbuddy/expert-marketplace",
    "https://acc-1258344699.cos.accelerate.tencentcos.cn/workbuddy/expert-marketplace",
]
SKILLHUB_BASE = "https://api.skillhub.cn"
MCP_MARKET = "https://cnb.cool/codebuddy/mcp-market"

REQUEST_TIMEOUT = 20

# 第三方公开专家市场仓库（.codebuddy-plugin/marketplace.json，直连可用、无需代理）。
# 结构与 COS expert_center.json 不同：条目 source 指向包目录，提示词入口由包内
# plugin.json 的 teamInfo.leadAgent / agents[0] 声明。失败一律静默跳过，不拖累主源。
_THIRD_PARTY_MARKETPLACES = [
    ("jinliangliu/workbuddy-experts",
     "https://raw.githubusercontent.com/jinliangliu/workbuddy-experts/main"),
    ("jianfengmacn/workbuddy-experts",
     "https://raw.githubusercontent.com/jianfengmacn/workbuddy-experts/main"),
]

# 2026-11-09：专家提示词上游有两种发布形态。早期专家在 COS 上有散装
# /plugins/{name}/agents/{name}.md；新增/改版专家只发整包
# /bundles/{plugin}.tar.gz，清单里的 promptFile 只是「解压后包内相对路径」。
# 单包上限 20MB、单文件上限 2MB，防止外部 tar.gz 撑爆内存。
_BUNDLE_MAX_BYTES = 20 * 1024 * 1024
_BUNDLE_MEMBER_MAX_BYTES = 2 * 1024 * 1024


def _safe_tar_member(name: str) -> str | None:
    """校验 tar 成员路径：拒绝绝对路径、上跳、盘符、非法字符。

    返回规范化后的安全相对路径；不安全返回 None。外部包不可信，绝不落盘到
    目标目录之外（路径穿越 / zip-slip 防护）。
    """
    n = (name or "").replace("\\", "/")
    if not n or n.startswith("/") or "\x00" in n:
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or ".." in parts or ":" in parts[0]:
        return None
    return "/".join(parts)


def _extract_bundle_prompt(raw: bytes) -> tuple[str, dict[str, bytes]]:
    """从 bundles/<plugin>.tar.gz 中提取主提示词 + skills/ 文件（全内存、不落盘）。

    返回 (prompt_text, skills_files)，skills_files 的键为相对 skills/ 的
    安全路径。找不到 agents/*.md 时抛 ValueError。
    """
    if len(raw) > _BUNDLE_MAX_BYTES:
        raise ValueError("专家包超过大小上限")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise ValueError(f"专家包解压失败: {e!s}") from e

    prompt = ""
    skills: dict[str, bytes] = {}
    with tf:
        for m in tf.getmembers():
            safe = _safe_tar_member(m.name)
            if not safe or not m.isfile():
                continue
            if m.size > _BUNDLE_MEMBER_MAX_BYTES:
                continue
            # 主提示词：优先 agents/{name}.md，其次任意 agents/*.md
            if not prompt and safe.startswith("agents/") and safe.endswith(".md"):
                f = tf.extractfile(m)
                if f is not None:
                    prompt = f.read(_BUNDLE_MEMBER_MAX_BYTES).decode("utf-8", "replace")
            # 专家自带技能（skills/<技能名>/...），原样保留相对结构
            if safe.startswith("skills/"):
                rel = posixpath.relpath(safe, "skills")
                if rel and rel != "." and not rel.startswith(".."):
                    f = tf.extractfile(m)
                    if f is not None:
                        skills[rel] = f.read(_BUNDLE_MEMBER_MAX_BYTES)
    if not prompt:
        raise ValueError("专家包内未找到 agents/*.md 提示词")
    return prompt, skills


def _cache_bundle_assets(key: str, skills: dict[str, bytes]) -> int:
    """把 bundle 内的 skills/ 落到 expert_market/<id>/skills/，返回写入文件数。

    与 experts.cache_market_expert 的落盘目录一致（专家自动发现会扫描该目录）；
    任何落盘异常都只记录、不影响提示词取用（技能是增强项，非必需）。
    """
    if not skills:
        return 0
    try:
        from .. import experts as _experts
        base = os.path.dirname(_MARKET_CACHE_DIR)
        catalog = _experts.get_catalog(base)
        root = catalog.market_dir
        if not root:
            return 0
        # 复用 cache_market_expert 的标识消毒规则，避免自行实现产生不一致
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in key).strip("-")[:64]
        if not safe:
            return 0
        dest_root = os.path.join(root, safe, "skills")
        written = 0
        for rel, data in skills.items():
            safe_rel = _safe_tar_member(rel)
            if not safe_rel or written >= 200:
                continue
            dest = os.path.join(dest_root, *safe_rel.split("/"))
            # 双保险：确保最终路径仍在 dest_root 之内
            if os.path.commonpath([os.path.abspath(dest_root), os.path.abspath(dest)]) != os.path.abspath(dest_root):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            written += 1
        if written:
            catalog.scan(force=True)
        return written
    except Exception as e:  # noqa: BLE001 - 技能落盘失败不应阻断取词
        logger.warning("专家包技能落盘失败（忽略）: %s", e)
        return 0

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


def _expert_get(rel: str, timeout: int | None = None):
    """同源多镜像 GET：按 EXPERT_BASES 顺序尝试，返回首个可用 Response。

    降级仅针对「镜像不可用」——网络异常 / 5xx / 429 才切下一个；
    404/400 视为「对象不存在或请求本身有误」，同源镜像结果一致，立即返回，
    避免每个不存在的散装 .md 都打满 3 个镜像。全部失败时返回最后的 Response
    （可能是 None）。
    """
    last = None
    for base in EXPERT_BASES:
        url = f"{base}/{rel.lstrip('/')}"
        try:
            r = requests.get(url, timeout=timeout or REQUEST_TIMEOUT)
            last = r
            if r.status_code == 200:
                return r
            if r.status_code < 500 and r.status_code != 429:
                return r  # 4xx（含 404）：对象级结果，不换镜像
        except Exception:
            continue  # 网络异常：换下一个镜像
    return last


def _expert_get_json(rel: str) -> Any:
    """走镜像降级的 JSON 拉取（清单 / 场景 / plugin.json）。"""
    r = _expert_get(rel)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"所有专家镜像均不可用: {rel}")
    return r.json()


def _normalize_third_party(market_name: str, raw_base: str, plugins: list) -> list:
    """把第三方 marketplace.json 的 plugins[] 归一化成统一专家条目。

    保留私有标记（下划线前缀，不与上游字段冲突），供 find_expert_meta /
    fetch_expert_prompt 识别来源。提示词入口延迟到取词时再解析 plugin.json，
    避免拉清单阶段对每个包多打一次请求。
    """
    out = []
    for p in plugins:
        if not isinstance(p, dict):
            continue
        name = (p.get("name") or "").strip()
        if not name:
            continue
        raw_src = (p.get("source") or f"./plugins/{name}").strip()
        # 外部清单不可信：在剥前导斜杠「之前」判断，否则 /etc/x 会被误当相对路径。
        # 拒绝上跳 / 绝对路径 / 绝对 URL / 协议 / 盘符。
        if (raw_src.startswith("/") or "://" in raw_src
                or "\\" in raw_src or ":" in raw_src.split("/")[0]
                or ".." in raw_src.replace("\\", "/").split("/")):
            continue
        src = raw_src[2:] if raw_src.startswith("./") else raw_src
        if not src:
            continue
        desc = p.get("description") or ""
        out.append({
            "agentName": name,
            "plugin": name,
            "displayName": {"zh": name, "en": name},
            "description": {"zh": desc, "en": desc},
            "categoryId": "16-ThirdParty",
            "categoryName": {"zh": "第三方市场", "en": "Third-party"},
            "tags": [{"zh": market_name, "en": market_name}],
            "_source": "third_party",
            "_market": market_name,
            "_raw_base": raw_base,
            "_source_rel": src,
        })
    return out


def _load_third_party_experts() -> list:
    """聚合第三方公开市场（带 1h 缓存、逐源容错、短超时）。

    任一源失败/超时都静默跳过，绝不影响主源；全部失败返回旧缓存或 []。
    """
    cached = _market_cache_load("third_party_experts", ttl=3600)
    if cached is not None:
        return cached
    merged: list = []
    for market_name, raw_base in _THIRD_PARTY_MARKETPLACES:
        try:
            r = requests.get(
                f"{raw_base}/.codebuddy-plugin/marketplace.json", timeout=6)
            if r.status_code != 200:
                continue
            d = r.json()
            plugins = d.get("plugins") or d.get("experts") or []
            merged.extend(_normalize_third_party(market_name, raw_base, plugins))
        except Exception as e:  # noqa: BLE001 - 第三方源失败一律静默
            logger.debug("第三方专家源 %s 加载失败: %s", market_name, e)
    if merged:
        _market_cache_save("third_party_experts", merged)
    return merged


def _fetch_third_party_prompt(meta: dict) -> str:
    """从第三方 Git 市场包取提示词原文。

    入口由包内 .codebuddy-plugin/plugin.json 的 teamInfo.leadAgent /
    agents[0] 声明；都不给时回退 agents/{name}.md。
    """
    raw_base = meta.get("_raw_base") or ""
    rel = (meta.get("_source_rel") or "").rstrip("/")
    name = _expert_field(meta, "agentName") or _expert_field(meta, "plugin")
    if not raw_base or not rel:
        raise ValueError("第三方专家条目缺少来源信息")
    pkg = f"{raw_base}/{rel}"
    md_rel = ""
    try:
        pj = requests.get(f"{pkg}/.codebuddy-plugin/plugin.json", timeout=REQUEST_TIMEOUT)
        if pj.status_code == 200:
            d = pj.json()
            lead = (d.get("teamInfo") or {}).get("leadAgent") or ""
            agents = d.get("agents") or []
            if lead:
                md_rel = f"agents/{lead}.md"
            elif agents:
                md_rel = str(agents[0]).lstrip("./")
    except Exception:
        pass
    candidates = [c for c in [md_rel, f"agents/{name}.md"] if c]
    tried = []
    for c in candidates:
        u = f"{pkg}/{c.lstrip('/')}"
        tried.append(c)
        try:
            r = requests.get(u, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200 and r.text.strip():
                return r.text
        except Exception:
            continue
    raise RuntimeError(
        f"第三方专家包未取到提示词：{name}（已尝试 {tried}）"
    )


def _load_experts(force: bool = False, include_third_party: bool = True) -> List[Dict[str, Any]]:
    """拉取专家清单（带缓存：内存 30min + 落盘 1h；多镜像降级）。

    主源（COS 429 专家，走镜像降级）失败时回退旧缓存；第三方公开市场作为
    增强层合并进来（同名以主源为准），其失败静默、绝不拖累主源。
    """
    if not force:
        cached = _market_cache_load("experts", _EXPERT_TTL)
        if cached is not None:
            _expert_cache["data"] = cached
            _expert_cache["ts"] = time.time()
            return cached
    try:
        data = _expert_get_json("expert_center.json")
    except Exception:
        # 所有镜像均不可用：若有旧缓存（内存/磁盘），降级返回旧数据
        fallback = _market_cache_load("experts", ttl=86400)
        if fallback is not None:
            logger.warning("专家市场镜像全部不可用，降级使用旧缓存")
            return fallback
        raise
    items = data if isinstance(data, list) else (data.get("experts") or data.get("list") or [])
    if include_third_party:
        try:
            seen = {_expert_id(x) for x in items if isinstance(x, dict)}
            for tp in _load_third_party_experts():
                if _expert_id(tp) not in seen:
                    items.append(tp)
                    seen.add(_expert_id(tp))
        except Exception as e:  # noqa: BLE001 - 第三方源一律不影响主源
            logger.debug("合并第三方专家源失败（忽略）: %s", e)
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
    查询专家市场（主源 429 个专家 + 第三方公开市场，COS 3 镜像自动降级，无需认证）。

    **何时使用（主动查询，不要等用户给链接）**：
    - 用户提到"专家/角色/人设/ personas"、想找某个领域的专家提示词
    - 需要为任务挑选一个领域专家角色（如"营销专家""Python 专家"）
    - 用户问"有没有 XX 专家""专家市场里有什么"

    - action=list（默认）：搜索专家清单
    - action=prompt：获取指定专家的完整提示词（.md 原文）
    - action=install：把专家落地为可复用团队成员
    - action=scenes：列出精选场景

    Args:
        action: 操作类型（list/prompt/install/scenes）
        keyword: 按名称/描述/标签模糊匹配（action=list 时有效）
        category: 分类过滤（action=list 时有效）
        limit: 返回条数上限（action=list 时有效）
        name: 专家标识（action=prompt/install 时必需）
    """
    # ── action 分发：install / prompt / scenes 是独立操作，先处理 ─────────────
    act = (action or "list").strip().lower()
    if act == "prompt":
        if not name:
            return _xml_response("error", "action=prompt 需要提供 name 参数（专家标识）")
        try:
            key, text = fetch_expert_prompt(name)
            return _xml_response("done", text[:8000])
        except Exception as e:
            return _xml_response("error", f"获取专家提示词失败: {e!s}")
    if act == "install":
        if not name:
            return _xml_response("error", "action=install 需要提供 name 参数（专家标识）")
        return InstallExpert(name=name)
    if act == "scenes":
        return ExpertScenes()

    # ── action=list（默认）：搜索专家清单 ───────────────────────────────────────
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
        ename = _expert_field(ex, "displayName", "name", "Name", "title", "profession")
        # 必须输出**专家标识**：InstallExpert/ExpertMarket(action=prompt) 只认 slug，
        # 只给中文显示名会导致「查到却装不上」，链路在此断掉。
        eid = _expert_id(ex)
        desc = _expert_field(ex, "description", "desc", "summary", "intro")[:120]
        catg = _expert_field(ex, "categoryId", "categoryName", "category", "group", "type")
        head = f"{i}. **{ename}**"
        if eid and eid != ename:
            head += f" `{eid}`"
        lines.append(head + (f" [{catg}]" if catg else ""))
        if desc:
            lines.append(f"   {desc}")
    lines.append(
        "\n下一步：用 `ExpertMarket(action=\"install\", name=\"<上面反引号里的标识>\")` 直接落地为团队成员，"
        "再用 `Task(subagent_type=\"成员名\")` 派发；只需临时人设时用 `ExpertMarket(action=\"prompt\", name=\"<标识>\")`。"
    )
    return _xml_response("done", "\n".join(lines))


def fetch_expert_prompt(name: str) -> tuple[str, str]:
    """拉取专家提示词原文（供 GetExpertPrompt / InstallExpert 复用）。

    返回 (key, text)；失败时抛异常，由调用方转成用户可读错误。

    2026-11-09 上游双形态兼容：
    - 早期专家：COS 上有散装 plugins/{name}/agents/{name}.md → 优先直取（快、省流量）；
    - 新增/改版专家：只发整包 bundles/{plugin}.tar.gz，清单 promptFile 只是包内
      相对路径 → 散装 404 时查清单拿 plugin 字段、下包解压取 agents/*.md，
      并把包内 skills/ 落地到专家缓存目录供自动发现。
    bundle 名必须用 plugin 字段（与 agentName 不一定相同，如团队专家）。
    """
    key = (name or "").strip()
    if not key:
        raise ValueError("需要提供 name（专家标识）")

    # 0) 第三方市场源：包在 Git 仓库、不在 COS，走独立解析路径
    meta = find_expert_meta(key)
    if isinstance(meta, dict) and meta.get("_source") == "third_party":
        try:
            return key, _fetch_third_party_prompt(meta)
        except Exception as e:
            raise RuntimeError(
                f"第三方专家源取词失败：{key}（{meta.get('_market', '')}）：{e!s}"
            ) from e

    # 1) 散装 .md 优先：兼容既有逻辑与"传完整路径/URL"的用法；走镜像降级
    prompt_path = key
    if key.startswith("/"):
        prompt_path = key.lstrip("/")
    else:
        for _b in EXPERT_BASES:
            if key.startswith(_b):
                prompt_path = key[len(_b):].lstrip("/")
                break
    resp = _expert_get(prompt_path)
    if (resp is None or resp.status_code != 200) and prompt_path == key:
        # 传的是纯标识时，再试标准散装路径
        resp = _expert_get(f"plugins/{key}/agents/{key}.md")
    if resp is not None and resp.status_code == 200:
        return key, resp.text

    # 2) 散装不存在 → 走 bundle。查清单拿 plugin（bundle 真实名）与 promptFile
    bundle_name = ""
    if isinstance(meta, dict):
        # plugin 是 bundle 名；缺则用清单里的 agentName / key 兜底
        plugin = _expert_field(meta, "plugin")
        bundle_name = plugin or _expert_field(meta, "agentName") or key
        # 若清单给了 promptFile，可从 /plugins/<x>/... 里提取更可靠的包名
        pf = meta.get("promptFile")
        if isinstance(pf, str) and pf.startswith("/plugins/"):
            seg = pf.split("/")
            if len(seg) > 2 and seg[2]:
                bundle_name = seg[2]
    if not bundle_name:
        bundle_name = key

    # 3) 下载整包（候选：清单包名 → 标识本身），解压取提示词；走镜像降级
    tried = []
    last_err = f"散装 .md HTTP {resp.status_code if resp is not None else '无响应'}"
    for bn in dict.fromkeys([bundle_name, key]):
        if not bn:
            continue
        tried.append(bn)
        br = _expert_get(f"bundles/{bn}.tar.gz")
        if br is None:
            last_err = "bundle 所有镜像均无响应"
            continue
        if br.status_code != 200:
            last_err = f"bundle HTTP {br.status_code}"
            continue
        try:
            text, skills = _extract_bundle_prompt(br.content)
        except ValueError as e:
            last_err = str(e)
            continue
        # 4) 技能落盘（增强项，失败不影响取词）
        if skills:
            _cache_bundle_assets(bundle_name, skills)
        logger.info("[Expert] 通过 bundle 取到提示词 %s（候选 %s）", key, bn)
        return key, text

    raise RuntimeError(
        f"未取到专家提示词：{key}。已尝试散装 .md 及 bundle 候选 {tried}；"
        f"最后原因：{last_err}。可先用 ExpertMarket 确认准确的 name。"
    )


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
    key = (name or "").strip()
    if not key:
        return _xml_response("error", "需要提供 name（专家标识）")

    # 1. 取专家提示词 + 元数据：本地专家库优先（离线可用、无需联网）
    local_expert = None
    try:
        from .. import experts as _experts
        _base = os.path.dirname(_MARKET_CACHE_DIR)  # bt 项目根目录
        local_expert = _experts.get_catalog(_base).get(key)
    except Exception as e:  # noqa: BLE001 - 本地库异常不影响市场回退
        logger.debug("本地专家库查询失败（回退市场）: %s", e)
        local_expert = None

    local_tools: List[str] = []
    if local_expert is not None:
        key = local_expert.id or key
        prompt_text = (local_expert.soul or "").strip()
        if local_expert.identity.strip():
            prompt_text = (prompt_text + "\n\n" + local_expert.identity.strip()).strip()
        if not prompt_text:
            prompt_text = (local_expert.description or local_expert.label or key).strip()
        local_tools = list(local_expert.tools or [])
        meta: Dict[str, Any] = {
            "displayName": local_expert.label,
            "description": local_expert.description,
            "category": local_expert.category,
        }
    else:
        try:
            key, prompt_text = fetch_expert_prompt(key)
        except ValueError as e:
            return _xml_response("error", str(e))
        except Exception as e:
            return _xml_response("error", f"获取专家提示词失败: {e!s}")
        meta = find_expert_meta(key) or find_expert_meta(name)
    display = _expert_field(meta, "displayName", "name", "Name", "title") if meta else ""
    desc = _expert_field(meta, "description", "desc", "summary", "intro") if meta else ""
    category = _expert_field(meta, "categoryName", "category", "group", "type") if meta else ""

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
    grant = tools if tools else (local_tools or default_tools)

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





@register_tool(category="市场", name_cn="查询专家场景", risk_level="low")
def ExpertScenes() -> str:
    """列出专家市场的精选场景（featuredScenes.json，公开无需认证）。"""
    try:
        data = _expert_get_json("featuredScenes.json")
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


def SkillPackageMarket(keyword: str = "", category: str = "", limit: int = 20) -> str:
    """技能包浏览（SkillHub /api/skills 分类列表，供 SkillMarket action=packages 复用）。"""
    try:
        if keyword.strip():
            data = _get_json(f"{SKILLHUB_BASE}/api/v1/search?q={requests.utils.quote(keyword)}&limit={max(1, int(limit))}")
            items = data if isinstance(data, list) else (data.get("skills") or data.get("items") or data.get("results") or [])
            total = len(items)
            inner = None
        else:
            params = f"page=1&pageSize={max(1, int(limit))}"
            if category.strip():
                params += f"&category={requests.utils.quote(category)}"
            data = _get_json(f"{SKILLHUB_BASE}/api/skills?{params}")
            inner = data.get("data") if isinstance(data, dict) else data
            items = (inner.get("skills") if isinstance(inner, dict) else []) or []
            total = (inner.get("total", 0) if isinstance(inner, dict) else 0)
    except Exception as e:
        return _xml_response("error", f"技能包列表查询失败: {e!s}")

    if not items:
        return _xml_response("done", f"未匹配到技能包（keyword={keyword!r} category={category!r}）。")

    lines = [f"<!-- 技能包：{len(items)} 条（在线共 {total} 个） -->"]
    for i, it in enumerate(items[: max(1, int(limit))], 1):
        nm = it.get("name") or it.get("slug") or ""
        slug = it.get("slug") or ""
        d = str(it.get("summary") or it.get("description") or "")[:120]
        lines.append(f"{i}. **{nm}**" + (f"  slug=`{slug}`" if slug else ""))
        if d:
            lines.append(f"   {d}")
    lines.append("\n安装：`SkillMarket(action=\"install\", package_slug=\"<上面反引号里的 slug>\")`")
    return _xml_response("done", "\n".join(lines))


def InstallSkillPackage(package_slug: str, overwrite: bool = False) -> str:
    """从 SkillHub 在线安装技能包到本地 skills 目录。

    流程：详情接口取 namespace → download 接口 302 到 COS zip → 交给
    skill_manager.install_from_zip 落盘。显式传 name=slug，规避中文 frontmatter
    name 无法匹配 [A-Za-z0-9_-] 的问题。
    """
    slug = (package_slug or "").strip()
    if not slug:
        return _xml_response("error", "需要 package_slug（技能标识）")
    if not re.match(r"^[A-Za-z0-9_.\-]{1,128}$", slug):
        return _xml_response("error", f"非法 package_slug: {slug!r}")

    try:
        detail = _get_json(f"{SKILLHUB_BASE}/api/v1/skills/{requests.utils.quote(slug)}")
    except Exception as e:
        return _xml_response("error", f"获取技能详情失败: {e!s}")

    ns = ""
    if isinstance(detail, dict):
        ns_obj = detail.get("namespace") or {}
        if isinstance(ns_obj, dict):
            ns = str(ns_obj.get("handle") or ns_obj.get("displayName") or "")

    params = f"slug={requests.utils.quote(slug)}"
    if ns:
        params += f"&namespace={requests.utils.quote(ns)}"
    try:
        resp = requests.get(f"{SKILLHUB_BASE}/api/v1/download?{params}",
                            timeout=REQUEST_TIMEOUT, allow_redirects=True)
    except Exception as e:
        return _xml_response("error", f"下载技能包失败: {e!s}")
    if resp.status_code != 200:
        return _xml_response("error", f"下载技能包失败: HTTP {resp.status_code}")
    if not resp.content or len(resp.content) < 100:
        return _xml_response("error", "下载内容为空或异常（可能不是 zip）")

    try:
        from chat_client.skills import skill_manager
    except Exception as e:
        return _xml_response("error", f"加载技能管理器失败: {e!s}")

    result = skill_manager.install_from_zip(resp.content, overwrite=overwrite, name=slug)
    if not result.get("status"):
        return _xml_response("error", f"安装失败: {result.get('msg', '未知错误')}")
    return _xml_response("done", f"✅ 技能 {slug} 安装成功（{result.get('msg', '')}）\n路径: {result.get('path', '')}")


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


