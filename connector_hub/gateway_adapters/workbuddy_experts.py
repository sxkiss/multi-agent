"""WorkBuddy Expert Marketplace — 聚合连接器，从 COS 拉取专家列表并展示。"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

EXPERT_BASE = "https://acc-1258344699.cos.accelerate.myqcloud.com/workbuddy/expert-marketplace"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "market_cache")
_TOOL_NAME = "expert_marketplace_browse"

TOOLS: list[dict[str, Any]] = [
    {
        "name": _TOOL_NAME,
        "description": (
            "Browse WorkBuddy Expert Marketplace. Returns a categorized list of all available AI experts. "
            "No credentials needed — all data is public. Use this to discover experts before installing them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category ID (e.g. '06-ContentCreative') or leave empty for all"},
                "keyword": {"type": "string", "description": "Filter by name/description keyword (case-insensitive)"},
                "limit": {"type": "integer", "description": "Max results, default 20, max 100", "default": 20},
            },
            "required": [],
        },
    },
]


def list_tools() -> list[dict[str, Any]]:
    return TOOLS


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    if name != _TOOL_NAME:
        return json.dumps({"error": f"unknown tool: {name}"}, ensure_ascii=False)

    import requests
    try:
        resp = requests.get(f"{EXPERT_BASE}/expert_center.json", timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return json.dumps({"error": f"failed to fetch expert center: {e}"}, ensure_ascii=False)

    experts = data.get("experts", [])
    categories = data.get("categories", [])
    cat_map = {c["id"]: c for c in categories}

    # Filter
    cat_filter = (args.get("category") or "").strip()
    keyword = (args.get("keyword") or "").strip().lower()
    limit = min(int(args.get("limit", 20) or 20), 100)

    results = []
    for exp in experts:
        if cat_filter and exp.get("categoryId") != cat_filter:
            continue
        if keyword:
            blob = " ".join([
                exp.get("id", ""),
                exp.get("displayName", {}).get("zh", ""),
                exp.get("displayName", {}).get("en", ""),
                exp.get("profession", {}).get("zh", ""),
                exp.get("description", {}).get("zh", ""),
                exp.get("description", {}).get("en", ""),
            ]).lower()
            if keyword not in blob:
                continue
        results.append({
            "id": exp.get("id"),
            "name_zh": exp.get("displayName", {}).get("zh", ""),
            "name_en": exp.get("displayName", {}).get("en", ""),
            "profession_zh": exp.get("profession", {}).get("zh", ""),
            "category_id": exp.get("categoryId"),
            "category_name_zh": cat_map.get(exp.get("categoryId"), {}).get("name", {}).get("zh", ""),
            "description_zh": exp.get("description", {}).get("zh", "")[:120],
            "prompt_file": exp.get("promptFile", ""),
        })
        if len(results) >= limit:
            break

    return json.dumps({
        "total": len(experts),
        "returned": len(results),
        "experts": results,
        "categories": [
            {"id": c["id"], "name_zh": c["name"].get("zh", ""), "name_en": c["name"].get("en", "")}
            for c in categories
        ]
    }, ensure_ascii=False)


def probe_credentials(creds: dict[str, Any]) -> None:
    pass  # No auth required
