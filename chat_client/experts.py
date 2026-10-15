"""
@input: json, os, re, time, typing, yaml
@output: ExpertCatalog / expert_catalog — 本地专家库自动发现（P5）
@position: Expert layer — 启动时扫描 experts/ 目录，自动发现专家（对齐 Octop）
@auto-doc: Update header and folder INDEX.md when this file changes

P5（2026-10-15，boss 反馈「不像 Octop 那样自动发现技能和专家」）：
bt 此前专家只能通过外部市场手动查询（ExpertMarket → GetExpertPrompt），
没有本地专家库、不自动发现、不注入系统提示。本模块补齐：

- 目录结构：experts/<expert-id>/ 内含 manifest.json + SOUL.md + IDENTITY.md（可选 USER.md/skills/）
- 启动/按需扫描：扫描所有 expert 目录，解析 manifest 与人格文件
- 摘要注入：生成紧凑的专家清单，注入系统提示（对齐技能摘要的做法）
- 一键安装：可把市场专家（GetExpertPrompt）落地到本地库（InstallExpert 增强）

manifest.json 字段（对齐 Octop，但更精简）：
{
  "id": "ops-engineer",
  "label": {"zh": "运维工程师", "en": "Ops Engineer"},
  "description": {"zh": "...", "en": "..."},
  "icon": "🛠️",
  "category": "运维",
  "triggers": ["运维", "部署", "排障"],
  "tools": ["Read", "Bash", ...]      // 可选：该专家推荐工具
}
"""
import json
import os
import threading
import time
from typing import Any

import yaml

_EXPERT_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "mtime": 0.0}
_TTL = 30  # 秒


def _experts_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "experts")


def _read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _pick_lang(value: Any, lang: str = "zh") -> str:
    """从 {zh,en} 或字符串中取值，优先中文。"""
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


class Expert:
    """一个本地专家（来自 experts/<id>/ 目录）。"""

    def __init__(self, expert_id: str, directory: str):
        self.id = expert_id
        self.directory = directory
        self.manifest: dict[str, Any] = {}
        self.label = expert_id
        self.description = ""
        self.icon = "🧩"
        self.category = "其他"
        self.triggers: list[str] = []
        self.tools: list[str] = []
        self.soul = ""
        self.identity = ""
        self.user = ""
        self.skills: list[str] = []
        self._load()

    def _load(self):
        mf = os.path.join(self.directory, "manifest.json")
        if os.path.exists(mf):
            try:
                data = json.load(open(mf, encoding="utf-8"))
                if isinstance(data, dict):
                    self.manifest = data
            except Exception:
                pass
        m = self.manifest
        self.label = _pick_lang(m.get("label") or m.get("name")) or self.id
        self.description = _pick_lang(m.get("description") or m.get("desc"))
        self.icon = str(m.get("icon") or m.get("emoji") or "🧩").strip()
        self.category = _pick_lang(m.get("category")) or "其他"
        trg = m.get("triggers") or m.get("trigger") or []
        if isinstance(trg, str):
            trg = [t.strip() for t in trg.split(",") if t.strip()]
        self.triggers = [str(t) for t in trg] if isinstance(trg, list) else []
        tl = m.get("tools") or []
        self.tools = [str(t) for t in tl] if isinstance(tl, list) else []
        self.soul = _read_text(os.path.join(self.directory, "SOUL.md"))
        self.identity = _read_text(os.path.join(self.directory, "IDENTITY.md"))
        self.user = _read_text(os.path.join(self.directory, "USER.md"))
        # 专家专属技能目录
        sdir = os.path.join(self.directory, "skills")
        if os.path.isdir(sdir):
            self.skills = sorted(
                d for d in os.listdir(sdir)
                if os.path.isdir(os.path.join(sdir, d)) and not d.startswith(".")
            )

    def to_dict(self, with_prompt: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "category": self.category,
            "triggers": self.triggers,
            "tools": self.tools,
            "skills": self.skills,
            "directory": self.directory,
        }
        if with_prompt:
            d["soul"] = self.soul
            d["identity"] = self.identity
            d["user"] = self.user
        return d


class ExpertCatalog:
    """专家库：扫描 experts/ 目录，缓存结果。"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self._lock = threading.Lock()

    @property
    def experts_dir(self) -> str:
        return _experts_dir(self.base_dir)

    def _dir_mtime(self) -> float:
        try:
            return os.path.getmtime(self.experts_dir)
        except OSError:
            return 0.0

    def scan(self, force: bool = False) -> list[Expert]:
        """扫描专家库（带 30s 缓存 + 目录 mtime 失效）。"""
        d = self.experts_dir
        now = time.time()
        mt = self._dir_mtime()
        with self._lock:
            if (not force and _EXPERT_CACHE["data"] is not None
                    and now - _EXPERT_CACHE["ts"] < _TTL
                    and _EXPERT_CACHE["mtime"] == mt):
                return _EXPERT_CACHE["data"]
        experts: list[Expert] = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                if not os.path.isdir(p) or name.startswith("."):
                    continue
                # 必须有 manifest.json 或 SOUL.md 才认作专家
                if not (os.path.exists(os.path.join(p, "manifest.json"))
                        or os.path.exists(os.path.join(p, "SOUL.md"))):
                    continue
                try:
                    experts.append(Expert(name, p))
                except Exception:
                    continue
        with self._lock:
            _EXPERT_CACHE["data"] = experts
            _EXPERT_CACHE["ts"] = now
            _EXPERT_CACHE["mtime"] = mt
        return experts

    def get(self, expert_id: str) -> Expert | None:
        for e in self.scan():
            if e.id == expert_id:
                return e
        return None

    def generate_expert_summary(self) -> str:
        """生成紧凑的专家清单（注入系统提示，对齐技能摘要做法）。"""
        experts = self.scan()
        if not experts:
            return ""
        lines = [f"## 本地专家库（共 {len(experts)} 位，可直接调用）"]
        lines.append("当任务匹配某位专家时，用 `InstallExpert` 落地为团队成员，"
                     "或用 `GetExpertPrompt` 取人设提示词；本地专家已自动发现，无需先查市场。")
        lines.append("")
        for e in experts:
            trg = f"（触发：{'/'.join(e.triggers[:4])}）" if e.triggers else ""
            desc = (e.description or "").strip()
            if len(desc) > 100:
                desc = desc[:100] + "…"
            lines.append(f"- {e.icon} **{e.label}** `{e.id}` [{e.category}]{trg}")
            if desc:
                lines.append(f"  {desc}")
        return "\n".join(lines)

    def list_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.scan()]


def get_catalog(base_dir: str) -> ExpertCatalog:
    return ExpertCatalog(base_dir)
