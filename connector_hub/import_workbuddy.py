"""从 WorkBuddy 连接器目录导入 443 个连接器到 BT catalog。

数据源（已解压到 /tmp/connectors-extracted/）：
- .codebuddy-connector/connectors.json — 443 个连接器元数据
- connectors/<id>/mcp.json           — 每个连接器的 MCP Server 配置

本脚本将 WB 条目映射为 BT connector_hub/catalog.json 的 CatalogEntry 格式：
- kind = id
- name.zh / name.en = name_zh / name_en
- description.zh / .en = description_zh / description_en
- auth_kind 映射：token→api_key，oauth→oauth，server-side→none，null→none
- mcp_mode：有 remote url → "remote"；特殊适配器 → "gateway"；type=cli → "cli"
- category 按 id/描述关键词推断
- credential_fields：解析 mcp.json 的 headers 中 ${VAR} 占位符

幂等：以现有 catalog 为基准合并，按 kind 去重，已有条目优先保留。
用法：
    python3 -m connector_hub.import_workbuddy            # 输出 catalog.json.new
    python3 -m connector_hub.import_workbuddy --apply   # 合并后写入 catalog.json.new 并 rename
    python3 -m connector_hub.import_workbuddy --cleanup # 额外删除 /tmp/connectors-extracted
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import OrderedDict
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_CATALOG_FILE = os.path.join(_HERE, "catalog.json")
_NEW_CATALOG_FILE = os.path.join(_HERE, "catalog.json.new")

# 解压产物目录
_SRC_ROOT = "/tmp/wbconn"
_SRC_CATALOG = os.path.join(_SRC_ROOT, ".codebuddy-connector", "connectors.json")
_SRC_CONNECTORS_DIR = os.path.join(_SRC_ROOT, "connectors")

# 允许的 category 取值（与现有 catalog 保持一致）
_VALID_CATEGORIES = {
    "office", "knowledge", "media", "productivity",
    "travel", "professional", "self_hosted",
}

# auth_mode → auth_kind 映射
_AUTH_MODE_MAP = {
    "token": "api_key",
    "oauth": "oauth",
    "server-side": "none",
    "oneid-token": "api_key",
    "mcp": "none",
    None: "none",
    "none": "none",
}

# gateway 模式（in-process Python 适配器）由 connectors_catalog.GATEWAY_KINDS 定义，
# 这里从现有 catalog 里继承；新增的 WB 连接器若无特殊适配器，一律 remote/cli。
# 注意：此处仅用于本脚本生成，实际判定仍以 connectors_catalog 为准。

# ── 分类关键词推断 ──────────────────────────────────────
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("office", (
        "doc", "文档", "office", "word", "sheet", "表格", "excel", "ppt", "幻灯片",
        "tencent", "feishu", "飞书", "notion", "youdao", "有道", "wps", "金山",
        "wiki", "知识库", "语雀", "石墨", "processon", "confluence", "问卷", "survey",
        "表单", "form", "calendar", "日历", "邮件", "mail", "outlook", "写",
        "editor", "编辑", "云盘", "网盘", "drive", "协作文档",
    )),
    ("travel", (
        "地图", "map", "flight", "航班", "hotel", "酒店", "travel", "出行",
        "携程", "ctrip", "美团", "meituan", "滴滴", "didi", "fliggy", "飞猪",
        "交通", "导航", "定位", "weather", "天气", "打车", "车票", "火车",
    )),
    ("professional", (
        "股票", "财经", "金融", "证券", "投资", "finance", "stock", "market",
        "行情", "tushare", "通达信", "tdx", "基金", "银行", "bank", "保险",
        "法律", "律师", "法院", "工商", "税务", "会计", "企业", "工商查询",
        "credit", "征信", "招聘", "hr", "社保", "公积金",
    )),
    ("media", (
        "图片", "视频", "图像", "image", "video", "音乐", "music", "媒体",
        "photo", "相机", "剪辑", "设计", "logo", "海报", "图标", "图标库",
        "壁纸", "素材", "ai生成", "生成图片", "绘图", "绘画", "3d", "三维",
    )),
    ("productivity", (
        "社交", "微信", "微博", "内容", "social", "blog", "创作", "社区",
        "todo", "任务", "日程", "提醒", "笔记", "效率", "工具", "telegram",
        "slack", "钉钉", "im", "即时通讯", "发布", "排版", "文案",
    )),
    ("knowledge", (
        "数据库", "开发", "dev", "api", "代码", "github", "git", "编程",
        "数据", "data", "爬虫", "search", "检索", "文档库", "学习", "教程",
        "education", "教育", "学术", "论文", "research", "研究", "科学",
        "knowledge", "百科", "ai", "人工智能", "大模型",
    )),
]

_DEFAULT_CATEGORY = "knowledge"


def _infer_category(entry: dict[str, Any]) -> str:
    """根据 id / name / description 关键词推断 category。"""
    text = " ".join([
        entry.get("id", ""),
        entry.get("name", "") or "",
        entry.get("name_zh", "") or "",
        entry.get("name_en", "") or "",
        entry.get("description", "") or "",
        entry.get("description_zh", "") or "",
        entry.get("description_en", "") or "",
    ]).lower()
    for category, keywords in _CATEGORY_RULES:
        for kw in keywords:
            if kw.lower() in text:
                return category
    return _DEFAULT_CATEGORY


def _clean_text(s: str | None, fallback: str = "") -> str:
    if not s:
        return fallback
    s = str(s).strip()
    return s or fallback


def _extract_env_vars(headers: dict[str, Any]) -> list[tuple[str, str]]:
    """从 mcp.json headers 中提取 ${VAR} 环境变量占位符。

    返回 [(key, var_name), ...]，key 为 BT credential_fields 的键名（变量名小写化），
    var_name 为原始环境变量名（用于 label 展示）。
    """
    found: list[tuple[str, str]] = []
    if not isinstance(headers, dict):
        return found
    for header_name, header_value in headers.items():
        if not isinstance(header_value, str):
            continue
        for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", header_value):
            var = m.group(1)
            key = var.lower().replace("_", "_")
            # 保证 key 唯一（同一变量可能出现多次）
            if var not in [v for _, v in found]:
                found.append((key, var))
    return found


def _auth_hint(auth_kind: str, mcp: dict[str, Any] | None) -> dict[str, str]:
    """根据 auth_kind 生成简短的 auth_hint。"""
    if auth_kind == "oauth":
        return {
            "en": "Connect with OAuth. Click authorize and sign in with the provider.",
            "zh": "使用 OAuth 授权登录，点击授权并登录对应服务商账户",
        }
    if auth_kind == "api_key":
        headers = (mcp or {}).get("mcpServers", {})
        # 尝试找到 headers 里的变量名作为提示
        var_names: list[str] = []
        for _srv in headers.values():
            if isinstance(_srv, dict):
                for v in _extract_env_vars(_srv.get("headers", {})):
                    if v[1] not in var_names:
                        var_names.append(v[1])
        if var_names:
            names = " / ".join(var_names)
            return {
                "en": f"Provide API key ({names}) obtained from the provider.",
                "zh": f"输入服务商提供的 API 密钥（{names}）",
            }
        return {
            "en": "Provide the API key / token from the provider.",
            "zh": "输入服务商提供的 API 密钥 / Token",
        }
    return {
        "en": "No additional credentials required. This connector works out of the box.",
        "zh": "无需额外凭据，该连接器开箱即用",
    }


def _load_wb_catalog() -> list[dict[str, Any]]:
    with open(_SRC_CATALOG, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("connectors", [])


def _load_mcp_config(connector_id: str) -> dict[str, Any] | None:
    path = os.path.join(_SRC_CONNECTORS_DIR, connector_id, "mcp.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _load_token_schema(connector_id: str) -> dict[str, Any] | None:
    """读取 token-schema.json，其中含真正的「如何获取凭证」指引（docUrl/docLabel/fields）。"""
    path = os.path.join(_SRC_CONNECTORS_DIR, connector_id, "token-schema.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _auth_kind_of(entry: dict[str, Any]) -> str:
    return _AUTH_MODE_MAP.get(entry.get("auth_mode"), "none")


def _mcp_mode_of(entry: dict[str, Any], mcp: dict[str, Any] | None) -> str:
    """判定 mcp_mode。

    - cli       → cli（带 preAuth=cli 的本地 CLI）
    - remote    → 有 HTTP/S SE url 的远程 MCP
    - stdio     → 有 command（npx/uvx/node/python 等）的本地 stdio MCP
    - gateway   → 兜底（旧 28 个进程内 Python 适配器用，由 GATEWAY_KINDS 显式定义）
    """
    ctype = entry.get("type")
    if ctype == "cli":
        return "cli"
    if mcp:
        for srv in mcp.get("mcpServers", {}).values():
            if not isinstance(srv, dict):
                continue
            if srv.get("url"):
                return "remote"
            if srv.get("command"):
                return "stdio"
    # 无 url 也无 command → gateway（兜底，通常是已有进程内适配器的 28 个）
    return "gateway"


def _credential_fields(auth_kind: str, mcp: dict[str, Any] | None,
                       token_schema: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """生成 credential_fields。

    优先使用 token-schema.json 的 fields（含真实的 label/placeholder/description），
    否则回退到从 mcp.json headers 解析 ${VAR} 占位符。
    """
    if token_schema:
        fields: list[dict[str, Any]] = []
        for f in token_schema.get("fields", []):
            key = str(f.get("key", "")).strip()
            if not key:
                continue
            label_en = _clean_text(f.get("label_en"), _clean_text(f.get("label"), key))
            label_zh = _clean_text(f.get("label"), label_en)
            placeholder_en = _clean_text(f.get("placeholder_en"), _clean_text(f.get("placeholder"), ""))
            placeholder_zh = _clean_text(f.get("placeholder"), placeholder_en)
            desc_en = _clean_text(f.get("description_en"), _clean_text(f.get("description"), ""))
            desc_zh = _clean_text(f.get("description"), desc_en)
            ftype = str(f.get("type", "text")).lower()
            if ftype == "password":
                ftype = "password"
            elif ftype in ("url", "tags"):
                pass
            else:
                ftype = "text"
            fields.append({
                "key": key,
                "label": {"en": label_en, "zh": label_zh},
                "required": bool(f.get("required", True)),
                "secret": ftype == "password",
                "field_type": ftype,
                "placeholder": {
                    "en": placeholder_en,
                    "zh": placeholder_zh,
                },
                "help": {
                    "en": desc_en,
                    "zh": desc_zh,
                },
            })
        if fields:
            return fields

    fields = []
    if auth_kind == "api_key":
        headers_vars: list[tuple[str, str]] = []
        if mcp:
            for srv in mcp.get("mcpServers", {}).values():
                if isinstance(srv, dict):
                    headers_vars.extend(_extract_env_vars(srv.get("headers", {})))
        # 去重（按 var 名）
        seen: set[str] = set()
        for key, var in headers_vars:
            if var in seen:
                continue
            seen.add(var)
            label = var.replace("_", " ").title()
            fields.append({
                "key": key,
                "label": {"en": label, "zh": label},
                "required": True,
                "secret": True,
                "placeholder": {
                    "en": f"Paste {var} here",
                    "zh": f"粘贴 {var}",
                },
                "help": {
                    "en": f"Obtain {var} from the provider",
                    "zh": f"从服务商处获取 {var}",
                },
            })
        if not fields:
            # 没有明确变量名的 api_key，给一个通用 token 字段
            fields.append({
                "key": "token",
                "label": {"en": "API Token", "zh": "API Token"},
                "required": True,
                "secret": True,
                "placeholder": {"en": "Paste API token here", "zh": "粘贴 API Token"},
                "help": {"en": "API token from the provider", "zh": "服务商提供的 API Token"},
            })
    return fields


def _to_catalog_entry(wb: dict[str, Any]) -> dict[str, Any]:
    """将单条 WB 连接器映射为 BT catalog 条目。"""
    kind = wb.get("id", "")
    name_zh = _clean_text(wb.get("name_zh"), _clean_text(wb.get("name"), kind))
    name_en = _clean_text(wb.get("name_en"), _clean_text(wb.get("name"), kind))
    desc_zh = _clean_text(wb.get("description_zh"), _clean_text(wb.get("description"), ""))
    desc_en = _clean_text(wb.get("description_en"), _clean_text(wb.get("description"), ""))
    mcp = _load_mcp_config(kind)
    token_schema = _load_token_schema(kind)
    auth_kind = _auth_kind_of(wb)
    mcp_mode = _mcp_mode_of(wb, mcp)

    # 从 mcp.json 提取 MCP URL / stdio 命令 / headers 模板
    mcp_url = ""
    headers_template: dict[str, str] = {}
    stdio_command = ""
    stdio_args: list[str] = []
    stdio_env: dict[str, str] = {}
    if mcp:
        for srv in mcp.get("mcpServers", {}).values():
            if isinstance(srv, dict):
                u = str(srv.get("url", "") or "").strip()
                if u:
                    mcp_url = u
                    h = srv.get("headers")
                    if isinstance(h, dict):
                        headers_template = {str(k): str(v) for k, v in h.items()}
                    break
                cmd = str(srv.get("command", "") or "").strip()
                if cmd:
                    stdio_command = cmd
                    stdio_args = [str(a) for a in srv.get("args", [])]
                    env = srv.get("env")
                    if isinstance(env, dict):
                        stdio_env = {str(k): str(v) for k, v in env.items()}
                    break

    # 获取凭证的真实链接：优先 token-schema.docUrl；否则回退 CDN 目录
    real_doc_url = ""
    if token_schema:
        real_doc_url = _clean_text(token_schema.get("docUrl"), "")
    cdn_url = f"https://static.workbuddy.cn/connectors-config-v2/connectors/{kind}/"
    doc_url = real_doc_url or cdn_url
    guide_url = real_doc_url or cdn_url
    manual_url = real_doc_url or cdn_url

    # auth_hint：优先用 token-schema 的 docLabel（如「如何获取 Tushare Token？」）
    auth_hint = _auth_hint(auth_kind, mcp)
    if token_schema:
        doc_label_en = _clean_text(token_schema.get("docLabel_en"), _clean_text(token_schema.get("docLabel"), ""))
        doc_label_zh = _clean_text(token_schema.get("docLabel"), doc_label_en)
        if doc_label_zh:
            auth_hint = {
                "en": doc_label_en or auth_hint.get("en", ""),
                "zh": doc_label_zh or auth_hint.get("zh", ""),
            }

    entry: dict[str, Any] = {
        "kind": kind,
        "name": {"en": name_en, "zh": name_zh},
        "description": {"en": desc_en, "zh": desc_zh},
        "auth_kind": auth_kind,
        "doc_url": doc_url,
        "icon": kind,
        "color": "#1e80ff",
        "phase": "available",
        "mcp_mode": mcp_mode,
        "category": _infer_category(wb),
        "quick_auth_url": real_doc_url,
        "guide_url": guide_url,
        "manual_url": manual_url,
        "auth_hint": auth_hint,
        "allowed_tools": [],
        "mcp_url": mcp_url,
        "headers_template": headers_template,
        "stdio_command": stdio_command,
        "stdio_args": stdio_args,
        "stdio_env": stdio_env,
        "credential_fields": _credential_fields(auth_kind, mcp, token_schema),
    }
    return entry


def load_existing_catalog() -> list[dict[str, Any]]:
    """读取现有 catalog.json。"""
    if not os.path.exists(_CATALOG_FILE):
        return []
    with open(_CATALOG_FILE, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("connectors", [])


def merge_catalogs(
    existing: list[dict[str, Any]],
    wb_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 kind 合并。

    策略：**已有条目优先保留**（它们是人工精调过的：oauth2 的 manual_token 字段、
    personal_token 的精确字段 key、auth_hint 文案等）。WB 条目只用于补充
    已有条目缺失的字段（mcp_url / headers_template / 真实 docUrl 等），
    不覆盖已有值。WB 独有的新连接器直接追加。
    """
    by_kind: dict[str, dict[str, Any]] = {e["kind"]: e for e in existing}

    # 需要从 WB 条目补充的字段（仅当已有条目为空/缺失时才填）
    _FILL_KEYS = (
        "mcp_url", "headers_template",
        "quick_auth_url", "guide_url", "manual_url", "doc_url",
    )

    for wb in wb_entries:
        kind = wb["kind"]
        cur = by_kind.get(kind)
        if cur is None:
            by_kind[kind] = wb
            continue
        # 已有条目：只补空缺，不覆盖
        for k in _FILL_KEYS:
            if not cur.get(k) and wb.get(k):
                cur[k] = wb[k]
        # stdio 型：旧条目被误判为 gateway，但 WB 里是有 command 的本地 MCP。
        # 覆盖 mcp_mode 并补上 stdio 启动字段（command/args/env）。
        if wb.get("stdio_command") and cur.get("mcp_mode") in ("gateway", "internal"):
            cur["mcp_mode"] = "stdio"
            cur["stdio_command"] = wb["stdio_command"]
            cur["stdio_args"] = wb.get("stdio_args", [])
            cur["stdio_env"] = wb.get("stdio_env", {})
            # 更新 credential_fields：以 WB 的 token-schema 字段为准
            if wb.get("credential_fields"):
                cur["credential_fields"] = wb["credential_fields"]
        # oauth 型：确保 mcp_url 已填
        if wb.get("auth_kind") == "oauth":
            if not cur.get("mcp_url") and wb.get("mcp_url"):
                cur["mcp_url"] = wb["mcp_url"]
        # manual_url / guide_url / doc_url 若仍是 CDN 死链，用 WB 的真实 docUrl 替换
        for k in ("manual_url", "guide_url", "doc_url"):
            if _is_cdn_dead_link(cur.get(k)) and wb.get(k) and not _is_cdn_dead_link(wb.get(k)):
                cur[k] = wb[k]
        # credential_fields 若已有条目为空才补
        if not cur.get("credential_fields") and wb.get("credential_fields"):
            cur["credential_fields"] = wb["credential_fields"]
        # auth_hint 若为空才补
        if not cur.get("auth_hint") and wb.get("auth_hint"):
            cur["auth_hint"] = wb["auth_hint"]
        # 若 WB 的 mcp_url/headers_template 含 ${VAR} 占位符，而旧条目字段没有该 key，
        # 则把该变量对应的字段补进 credential_fields，保证「填什么 → 替换什么」对齐。
        _align_placeholder_fields(cur, wb)

    return list(by_kind.values())


def _placeholder_vars(obj: Any) -> list[str]:
    """从字符串/字典中递归提取 ${VAR} 变量名。"""
    vars_seen: list[str] = []
    if isinstance(obj, str):
        for m in re.finditer(r"\$\{([^}]+)\}", obj):
            v = m.group(1).strip()
            if v not in vars_seen:
                vars_seen.append(v)
    elif isinstance(obj, dict):
        for v in obj.values():
            vars_seen.extend(_placeholder_vars(v))
    elif isinstance(obj, list):
        for item in obj:
            vars_seen.extend(_placeholder_vars(item))
    return vars_seen


def _align_placeholder_fields(cur: dict[str, Any], wb: dict[str, Any]) -> None:
    """把 WB mcp_url/headers_template 里的 ${VAR} 对齐到 credential_fields。

    仅当旧条目**没有任何 credential_fields** 时，才从占位符生成字段；
    若旧条目已有字段（如 token / api_key），说明用户按旧字段填写，
    service 层 _resolve_var 会做大小写不敏感匹配 + token/api_key 兜底，
    无需再添加冗余字段。
    """
    if cur.get("credential_fields"):
        return
    mcp_url = wb.get("mcp_url") or ""
    headers = wb.get("headers_template") or {}
    vars_needed = _placeholder_vars(mcp_url) + _placeholder_vars(headers)
    if not vars_needed:
        return
    cur_fields = []
    for var in vars_needed:
        label = var.replace("_", " ").title()
        cur_fields.append({
            "key": var,
            "label": {"en": label, "zh": label},
            "required": True,
            "secret": True,
            "placeholder": {"en": f"Paste {var} here", "zh": f"粘贴 {var}"},
            "help": {"en": f"Obtain {var} from the provider", "zh": f"从服务商处获取 {var}"},
        })
    if cur_fields:
        cur["credential_fields"] = cur_fields


def _is_cdn_dead_link(url: Any) -> bool:
    """WorkBuddy CDN 的目录链接实际 404，不是有效的凭证获取页。"""
    return isinstance(url, str) and "static.workbuddy.cn/connectors-config-v2/" in url


def build_new_catalog() -> dict[str, Any]:
    """执行导入并返回新 catalog 字典。"""
    existing = load_existing_catalog()
    wb = _load_wb_catalog()
    wb_entries = [_to_catalog_entry(c) for c in wb]
    merged = merge_catalogs(existing, wb_entries)
    return {
        "version": 1,
        "connectors": merged,
    }


def write_catalog(catalog: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(catalog, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main() -> int:
    apply = "--apply" in sys.argv
    cleanup = "--cleanup" in sys.argv

    catalog = build_new_catalog()
    write_catalog(catalog, _NEW_CATALOG_FILE)
    print(f"已生成 {_NEW_CATALOG_FILE}：共 {len(catalog['connectors'])} 条连接器")

    if apply:
        os.replace(_NEW_CATALOG_FILE, _CATALOG_FILE)
        print(f"已应用为 {_CATALOG_FILE}（共 {len(catalog['connectors'])} 条）")

    if cleanup:
        import shutil
        if os.path.exists(_SRC_ROOT):
            shutil.rmtree(_SRC_ROOT)
            print("已删除 /tmp/connectors-extracted/")
    return 0


if __name__ == "__main__":
    sys.exit(main())