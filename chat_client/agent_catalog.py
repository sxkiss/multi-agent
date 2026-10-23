"""Agent catalog — 统一 agent 发现层。

扫描多个 root，支持:
- 专家目录（manifest.json + SOUL.md）
- subagent 库（markdown frontmatter）

核心类:
  AgentSummary: 轻量大模型可用摘要
  AgentDefinition: 完整定义（含内容）
  AgentCatalog: 多 root 扫描 + mtime 缓存
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL = 30  # 秒


def _pick_lang(value: Any, lang: str = "zh") -> str:
    if isinstance(value, dict):
        for k in (lang, "zh", "zh_CN", "cn", "en"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in value.values():
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""
    return str(value).strip() if isinstance(value, str) else ""


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析 markdown frontmatter，返回 (metadata_dict, body)。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        import yaml
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:
        meta = {}
    body = text[m.end():]
    return meta, body


@dataclass(frozen=True)
class DivisionMeta:
    id: str
    label: str
    icon: str | None = None
    color: str | None = None
    available_locales: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSummary:
    id: str
    label_zh: str
    label_en: str
    description_zh: str
    description_en: str
    category: str
    icon: str | None
    color: str | None
    tools: tuple[str, ...]
    triggers: tuple[str, ...]
    prompt_files: tuple[str, ...]
    source_path: str = ""
    source: str = "bundled"  # bundled | market
    division: str = ""


@dataclass
class AgentDefinition:
    summary: AgentSummary
    content: dict[str, str] = field(default_factory=dict)
    name: str = ""
    description: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    system_prompt_template: str = ""
    department: str = ""
    skills: list[str] = field(default_factory=list)
    expert_dir: str = ""


class AgentCatalog:
    """统一 agent catalog: 自动发现 experts/ 和 subagents/library/ 目录。"""

    def __init__(self, project_root: str | None = None,
                 expert_dirs: list[str] | None = None,
                 subagent_dirs: list[str] | None = None):
        self._root = Path(project_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._expert_roots = [
            Path(os.path.join(self._root, d))
            for d in (expert_dirs or [
                os.path.join(self._root, "experts"),
                os.path.join(self._root, "expert_market"),
            ])
            if d and Path(os.path.join(self._root, d)).is_dir()
        ]
        self._subagent_roots = [
            Path(os.path.join(self._root, d))
            for d in (subagent_dirs or [
                os.path.join(self._root, "agents"),
            ])
            if d and Path(os.path.join(self._root, d)).is_dir()
        ]
        self._ttl = DEFAULT_TTL
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {"ts": 0.0, "agents": [], "mtime": 0.0}
        self._divisions: dict[str, DivisionMeta] = {}
        self._preload()

    def _preload(self) -> None:
        self._cache["ts"] = time.time()
        self._cache["agents"] = self._scan()
        self._cache["mtime"] = self._max_mtime()

    def _max_mtime(self) -> float:
        candidates = []
        for root in self._expert_roots + self._subagent_roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file():
                    candidates.append(p.stat().st_mtime)
        return max(candidates) if candidates else 0.0

    def refresh(self) -> list[AgentDefinition]:
        with self._lock:
            now = time.time()
            mt = self._max_mtime()
            if now - self._cache["ts"] < self._ttl and mt <= self._cache["mtime"]:
                return self._cache["agents"]
            agents = self._scan()
            self._cache = {"ts": now, "agents": agents, "mtime": mt}
            return agents

    def scan(self) -> list[AgentDefinition]:
        return self.refresh()

    def get(self, agent_id: str) -> AgentDefinition | None:
        for a in self.scan():
            if a.summary.id == agent_id:
                return a
        return None

    def list_summaries(self) -> list[AgentSummary]:
        return [a.summary for a in self.scan()]

    def get_divisions(self) -> dict[str, DivisionMeta]:
        return dict(self._divisions)

    def _scan(self) -> list[AgentDefinition]:
        agents: list[AgentDefinition] = []
        seen_ids: set[str] = set()

        # 扫描 experts/ 目录
        for root in self._expert_roots:
            if not root.is_dir():
                continue
            for d in root.iterdir():
                if not d.is_dir():
                    continue
                mf = d / "manifest.json"
                if not mf.exists():
                    continue
                aid = d.name
                if aid in seen_ids:
                    continue
                seen_ids.add(aid)
                src = "market" if "expert_market" in str(root) else "bundled"
                defn = self._load_expert(aid, str(d), src)
                if defn:
                    agents.append(defn)

        # 扫描 subagents/library/<locale>/<division>/*.md
        for root in self._subagent_roots:
            if not root.is_dir():
                continue
            div_json = root / "divisions.json"
            if div_json.exists():
                self._divisions.update(self._load_divisions(str(div_json)))
            locales_dir = root
            for locale_dir in sorted(locales_dir.iterdir()):
                if not locale_dir.is_dir() or locale_dir.name.startswith("."):
                    continue
                for div_dir in sorted(locale_dir.iterdir()):
                    if not div_dir.is_dir() or div_dir.name.startswith("."):
                        continue
                    for md_file in sorted(div_dir.glob("*.md")):
                        aid = md_file.stem
                        if aid in seen_ids:
                            continue
                        seen_ids.add(aid)
                        defn = self._load_subagent(str(md_file), aid, str(div_dir.parent),
                                                    div_dir.name, locale_dir.name)
                        if defn:
                            agents.append(defn)

        return agents

    def _load_expert(self, aid: str, dir_path: str, source: str) -> AgentDefinition | None:
        mf = os.path.join(dir_path, "manifest.json")
        if not os.path.exists(mf):
            return None
        try:
            data = json.load(open(mf, encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        label_zh = _pick_lang(data.get("label") or data.get("name"))
        label_en = _pick_lang(data.get("label") or data.get("name"), "en")
        desc_zh = _pick_lang(data.get("description") or data.get("desc"))
        desc_en = _pick_lang(data.get("description") or data.get("desc"), "en")
        icon = str(data.get("icon") or data.get("emoji") or "🧩").strip() or None
        color = str(data.get("color") or "").strip() or None
        cat = _pick_lang(data.get("category")) or "其他"
        tools = [str(t) for t in (data.get("tools") or []) if t]
        triggers = [str(t) for t in (data.get("triggers") or []) if t]
        prompt_files = [str(x) for x in (data.get("prompt_files") or []) if x]
        summary = AgentSummary(
            id=aid, label_zh=label_zh, label_en=label_en,
            description_zh=desc_zh, description_en=desc_en,
            category=cat, icon=icon, color=color,
            tools=tuple(tools), triggers=tuple(triggers),
            prompt_files=tuple(prompt_files),
            source_path=dir_path, source=source,
        )
        # 加载内容
        soul = self._read_file(os.path.join(dir_path, "SOUL.md"))
        identity = self._read_file(os.path.join(dir_path, "IDENTITY.md"))
        user = self._read_file(os.path.join(dir_path, "USER.md"))
        content = {"zh": soul, "identity": identity, "user": user}
        system_prompt = soul + "\n\n" + identity if identity else soul
        return AgentDefinition(
            summary=summary, content=content,
            name=aid, description=desc_zh,
            allowed_tools=tools, system_prompt_template=system_prompt,
            department=cat, expert_dir=dir_path,
        )

    def _load_subagent(self, md_path: str, aid: str, div_path: str,
                       division: str, locale: str) -> AgentDefinition | None:
        try:
            text = open(md_path, encoding="utf-8").read()
        except Exception:
            return None
        meta, body = _parse_frontmatter(text)
        name_zh = _pick_lang(meta.get("name"))
        desc_zh = _pick_lang(meta.get("description"))
        emoji = str(meta.get("emoji") or "").strip() or None
        color = str(meta.get("color") or "").strip() or None
        summary = AgentSummary(
            id=aid, label_zh=name_zh or aid, label_en="",
            description_zh=desc_zh or "", description_en="",
            category=division, icon=emoji, color=color,
            tools=(), triggers=(), prompt_files=(),
            source_path=md_path, source="bundled", division=division,
        )
        content = {"zh": body}
        system_prompt = body[:2000]
        return AgentDefinition(
            summary=summary, content=content,
            name=aid, description=desc_zh or "",
            allowed_tools=[], system_prompt_template=system_prompt,
            department=division,
        )

    def _load_divisions(self, path: str) -> dict[str, DivisionMeta]:
        if not os.path.exists(path):
            return {}
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
        raw = data.get("divisions")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, DivisionMeta] = {}
        for div_id, node in raw.items():
            if not isinstance(node, dict):
                continue
            out[str(div_id)] = DivisionMeta(
                id=str(div_id),
                label=str(node.get("label") or div_id),
                icon=node.get("icon"),
                color=node.get("color"),
            )
        return out

    def _read_file(self, path: str) -> str:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read()
        except Exception:
            return ""


# 全局单例
_catalog: AgentCatalog | None = None
_catalog_lock = threading.Lock()


def get_catalog(project_root: str | None = None,
                expert_dirs: list[str] | None = None,
                subagent_dirs: list[str] | None = None) -> AgentCatalog:
    global _catalog
    with _catalog_lock:
        if _catalog is None:
            _catalog = AgentCatalog(project_root, expert_dirs, subagent_dirs)
        return _catalog
