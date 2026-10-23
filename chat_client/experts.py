"""
@input: json, os, threading, time, typing, yaml
@auto-doc: Update header and folder INDEX.md when this file changes

P5（2026-10-15，boss 反馈「不像成熟产品那样自动发现技能和专家」）：
bt 此前专家只能通过外部市场手动查询（ExpertMarket → GetExpertPrompt），
没有本地专家库、不自动发现、不注入系统提示。

扫描**多个 root**，内置 + 市场缓存都自动发现：

- **内置 root**：`experts/`（随仓库分发，版本管理）
  内含 manifest.json + SOUL.md + IDENTITY.md（可选 USER.md/skills/）
- **市场缓存 root**：`expert_market/`（gitignored，运行时落盘）
  市场专家被取用/安装后缓存到此，catalog 自动扫描到 → 之后即算「本地已有」

一致：市场专家**不预加载全部 429 个**（那是市场目录，需按需取用）；
一旦取用即落盘缓存，catalog 自动发现，后续无需联网。

manifest.json 字段：
{
  "id": "ops-engineer",
  "label": {"zh": "运维工程师", "en": "Ops Engineer"},
  "description": {"zh": "...", "en": "..."},
  "icon": "🛠️",
  "category": "运维",
  "triggers": ["运维", "部署", "排障"],
  "tools": ["Read", "Bash", ...],
  "source": "bundled" | "market"     // 可选，缺省按 root 推断
}
"""
import json
import os
import shutil
import threading
import time
from typing import Any

MARKET_CACHE_DIRNAME = "expert_market"
BUNDLED_DIRNAME = "experts"

_EXPERT_CACHE: dict[str, Any] = {"ts": 0.0, "data": None, "mtime": 0.0}
_TTL = 30  # 秒


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
    """一个本地专家（来自 experts/ 或 expert_market/ 目录）。"""

    def __init__(self, expert_id: str, directory: str, source: str = "bundled"):
        self.id = expert_id
        self.directory = directory
        self.source = source  # bundled | market
        self.manifest: dict[str, Any] = {}
        self.label = expert_id
        self.description = ""
        self.icon = "🧩"
        self.color = ""
        self.category = "其他"
        self.welcome_message = ""
        self.quick_prompts: list[dict[str, Any]] = []
        self.triggers: list[str] = []
        self.tools: list[str] = []
        self.prompt_files: list[str] = []
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
        self.icon = str(m.get("icon") or m.get("emoji") or m.get("icon_name") or "🧩").strip()
        self.color = str(m.get("color") or "").strip()
        self.category = _pick_lang(m.get("category")) or "其他"
        self.source = str(m.get("source") or self.source).strip() or self.source
        self.welcome_message = _pick_lang(m.get("welcome_message") or m.get("welcome"))
        qp = m.get("quick_prompts") or []
        if isinstance(qp, list):
            for item in qp:
                if not isinstance(item, dict):
                    continue
                self.quick_prompts.append({
                    "title": _pick_lang(item.get("title")),
                    "description": _pick_lang(item.get("description")),
                    "prompt": _pick_lang(item.get("prompt")),
                    "color": str(item.get("color") or "").strip(),
                    "icon": str(item.get("icon_name") or item.get("icon") or "").strip(),
                })
        # prompt_files（manifest 声明的 persona 文件）
        pf = m.get("prompt_files") or []
        self.prompt_files = [str(x) for x in pf] if isinstance(pf, list) else []
        trg = m.get("triggers") or m.get("trigger") or []
        if isinstance(trg, str):
            trg = [t.strip() for t in trg.split(",") if t.strip()]
        self.triggers = [str(t) for t in trg] if isinstance(trg, list) else []
        tl = m.get("tools") or []
        self.tools = [str(t) for t in tl] if isinstance(tl, list) else []
        # 人格文件：SOUL/IDENTITY/USER，兼容大小写与 prompt 命名
        for attr, names in (
            ("soul", ("SOUL.md", "soul.md", "PROMPT.md", "prompt.md")),
            ("identity", ("IDENTITY.md", "identity.md")),
            ("user", ("USER.md", "user.md")),
        ):
            for n in names:
                p = os.path.join(self.directory, n)
                if os.path.exists(p):
                    setattr(self, attr, _read_text(p))
                    break
        # 若无 SOUL，尝试任意 .md（市场专家有时只有单个 md）
        if not self.soul:
            for n in sorted(os.listdir(self.directory)) if os.path.isdir(self.directory) else []:
                if n.lower().endswith(".md") and n.lower() not in ("identity.md", "user.md"):
                    self.soul = _read_text(os.path.join(self.directory, n))
                    break
        sdir = os.path.join(self.directory, "skills")
        if os.path.isdir(sdir):
            self.skills = sorted(
                d for d in os.listdir(sdir)
                if os.path.isdir(os.path.join(sdir, d)) and not d.startswith(".")
            )
        self.files = self._discover_files()

    def _discover_files(self) -> list[str]:
        """递归列出专家目录下所有文件（相对路径），排除 manifest.json。"""
        out: list[str] = []
        if not os.path.isdir(self.directory):
            return out
        for root, dirs, fnames in os.walk(self.directory):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fn in sorted(fnames):
                if fn.startswith(".") or fn == "manifest.json":
                    continue
                rel = os.path.relpath(os.path.join(root, fn), self.directory)
                out.append(rel.replace(os.sep, "/"))
        return sorted(out)

    def to_dict(self, with_prompt: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "icon": self.icon,
            "color": self.color,
            "category": self.category,
            "welcome_message": self.welcome_message,
            "quick_prompts": self.quick_prompts,
            "triggers": self.triggers,
            "tools": self.tools,
            "skills": self.skills,
            "prompt_files": self.prompt_files,
            "files": self.files,
            "source": self.source,
            "directory": self.directory,
        }
        if with_prompt:
            d["soul"] = self.soul
            d["identity"] = self.identity
            d["user"] = self.user
        return d


class ExpertCatalog:
    """专家库：扫描多个 root（内置 + 市场缓存），缓存结果。"""

    def __init__(self, base_dir: str, extra_roots: list[str] | None = None):
        self.base_dir = base_dir
        # 内置 root 优先；市场缓存 root 次之（同名以内置为准）
        roots = [os.path.join(base_dir, BUNDLED_DIRNAME)]
        if extra_roots:
            roots.extend(extra_roots)
        else:
            roots.append(os.path.join(base_dir, MARKET_CACHE_DIRNAME))
        self.roots = roots
        self._lock = threading.Lock()

    @property
    def experts_dir(self) -> str:
        return self.roots[0]

    @property
    def market_dir(self) -> str:
        return self.roots[1] if len(self.roots) > 1 else ""

    def _roots_mtime(self) -> float:
        mt = 0.0
        for r in self.roots:
            try:
                mt = max(mt, os.path.getmtime(r))
            except OSError:
                pass
        return mt

    def _scan_root(self, root: str, source: str, seen: set[str]) -> list[Expert]:
        out: list[Expert] = []
        if not os.path.isdir(root):
            return out
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p) or name.startswith("."):
                continue
            if name in seen:
                continue  # 内置优先
            if not (os.path.exists(os.path.join(p, "manifest.json"))
                    or os.path.exists(os.path.join(p, "SOUL.md"))
                    or any(f.lower().endswith(".md") for f in os.listdir(p))):
                continue
            try:
                out.append(Expert(name, p, source=source))
                seen.add(name)
            except Exception:
                continue
        return out

    def scan(self, force: bool = False) -> list[Expert]:
        """扫描全部 root（带 30s 缓存 + 目录 mtime 失效）。"""
        now = time.time()
        mt = self._roots_mtime()
        with self._lock:
            if (not force and _EXPERT_CACHE["data"] is not None
                    and now - _EXPERT_CACHE["ts"] < _TTL
                    and _EXPERT_CACHE["mtime"] == mt):
                return _EXPERT_CACHE["data"]
        experts: list[Expert] = []
        seen: set[str] = set()
        for i, root in enumerate(self.roots):
            src = "bundled" if i == 0 else "market"
            experts.extend(self._scan_root(root, src, seen))
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

    def generate_expert_summary(self, max_market: int = 40) -> str:
        """生成紧凑的专家清单（注入系统提示，对齐技能摘要做法）。"""
        experts = self.scan()
        if not experts:
            return ""
        bundled = [e for e in experts if e.source != "market"]
        market = [e for e in experts if e.source == "market"]
        lines = [f"## 本地专家库（共 {len(experts)} 位，可直接调用）"]
        lines.append("当任务匹配某位专家时，用 `InstallExpert` 落地为团队成员，"
                     "或用 `GetExpertPrompt` 取人设提示词；本地专家已自动发现，无需先查市场。")
        lines.append("")

        def _fmt(e: Expert) -> list[str]:
            trg = f"（触发：{'/'.join(e.triggers[:4])}）" if e.triggers else ""
            desc = (e.description or "").strip()
            if len(desc) > 100:
                desc = desc[:100] + "…"
            r = [f"- {e.icon} **{e.label}** `{e.id}` [{e.category}]{trg}"]
            if desc:
                r.append(f"  {desc}")
            return r

        if bundled:
            lines.append(f"### 内置专家（{len(bundled)}）")
            for e in bundled:
                lines.extend(_fmt(e))
        if market:
            lines.append("")
            lines.append(f"### 市场专家缓存（{len(market)}，已取用落盘）")
            for e in market[:max_market]:
                lines.extend(_fmt(e))
            if len(market) > max_market:
                lines.append(f"  …另有 {len(market) - max_market} 位，"
                             "可用 ExpertMarket(keyword=...) 检索")
        return "\n".join(lines)

    def list_dicts(self) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.scan()]

    def cache_market_expert(self, expert_id: str, prompt_text: str,
                            meta: dict[str, Any] | None = None) -> str:
        """把市场专家提示词落盘到 expert_market/<id>/，使其被自动发现。

        返回落盘目录。已存在则更新提示词文件。
        """
        eid = (expert_id or "").strip()
        if not eid:
            raise ValueError("expert_id 不能为空")
        # 严格拒绝含路径特征/上跳的标识（不静默消毒，避免掩盖恶意输入）
        if any(ch in eid for ch in ("/", "\\", "\x00")) or ".." in eid:
            raise ValueError(f"非法的专家标识: {expert_id!r}")
        # 只允许字母/数字/连字符/下划线
        safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in eid)
        safe = safe.strip("-")
        if not safe or len(safe) > 64:
            raise ValueError(f"非法的专家标识: {expert_id!r}")
        root = self.market_dir
        if not root:
            raise ValueError("未配置市场缓存目录")
        d = os.path.join(root, safe)
        os.makedirs(d, exist_ok=True)
        # 提示词写入 SOUL.md（Expert 会自动读取）
        with open(os.path.join(d, "SOUL.md"), "w", encoding="utf-8") as f:
            f.write(prompt_text or "")
        # 写 manifest（含元数据），便于摘要展示
        manifest: dict[str, Any] = {"id": safe, "source": "market"}
        if meta:
            label = _pick_lang(meta.get("displayName") or meta.get("label")
                               or meta.get("name") or meta.get("profession"))
            desc = _pick_lang(meta.get("description") or meta.get("desc")
                              or meta.get("profession"))
            cat = _pick_lang(meta.get("category") or meta.get("categoryName"))
            tags = meta.get("tags") or []
            triggers = []
            for t in tags:
                v = _pick_lang(t)
                if v:
                    triggers.append(v)
            if label:
                manifest["label"] = {"zh": label, "en": label}
            if desc:
                manifest["description"] = {"zh": desc}
            if cat:
                manifest["category"] = cat
            if triggers:
                manifest["triggers"] = triggers
        with open(os.path.join(d, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        self.scan(force=True)
        return d


def get_catalog(base_dir: str, extra_roots: list[str] | None = None) -> ExpertCatalog:
    return ExpertCatalog(base_dir, extra_roots=extra_roots)
