"""
@input: threading, time, collections, json, typing, os
@output: MetricsRegistry / get_metrics — 进程内轻量指标（P3-b 可观测性）+ 跨重启持久化
@position: Observability layer — 工具/LLM 调用的计数与耗时统计
@auto-doc: Update header and folder INDEX.md when this file changes

不引入 Prometheus 依赖，用纯标准库维护进程内计数器/直方图，
通过 /api/metrics 以 JSON（可转 Prometheus 文本）暴露。
覆盖：工具调用次数/耗时/状态、LLM 调用次数/耗时、错误计数。

【持久化（2026-10-21）】
原实现纯进程内，重启即清零 —— 导致「哪些工具从没人用过」这类长期决策
（如 MCP 工具该不该延迟加载）无法判断。现增加累计计数落盘：
- 文件：<base_dir>/metrics/tool_usage.json（原子写）
- 内存中每次调用只累加，按 CALLS_BEFORE_FLUSH 次或 FLUSH_INTERVAL 秒批量落盘，
  避免高频工具（Bash 占 67%）每次调用都写盘。
- 进程启动时载入历史，与本次进程数据合并展示（history / process 分列）。
"""
import json
import os
import threading
import time
from collections import defaultdict
from typing import Any

_lock = threading.Lock()

# 落盘节流：累计多少次调用或多少秒后写盘一次
CALLS_BEFORE_FLUSH = 20
FLUSH_INTERVAL = 60.0



class MetricsRegistry:
    """进程内指标注册表（线程安全）。"""

    def __init__(self, persist_path: str | None = None):
        # 计数器：name -> value
        self._counters: dict[str, float] = defaultdict(float)
        # 工具维度统计：tool -> {calls, errors, blocked, total_ms, max_ms}
        self._tools: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "blocked": 0, "total_ms": 0.0, "max_ms": 0.0}
        )
        # LLM 维度：model -> {calls, errors, total_ms}
        self._llm: dict[str, dict[str, float]] = defaultdict(
            lambda: {"calls": 0, "errors": 0, "total_ms": 0.0}
        )
        # 技能维度：skill_name -> {loads, installs, uninstalls, errors}
        # 工具级只记到 "Skills" 这一个名字，无法回答「哪个技能最常用 /
        # 哪些技能从未被加载」——而这正是决定技能摘要该不该瘦身的依据。
        self._skills: dict[str, dict[str, float]] = defaultdict(
            lambda: {"loads": 0, "installs": 0, "uninstalls": 0, "errors": 0}
        )
        self._start_ts = time.time()

        # ---- 持久化（跨重启累计） ----
        self._persist_path = persist_path
        # 技能统计独立落盘：与工具统计语义不同（技能是「被加载的资源」，
        # 不是「被执行的能力」），混在一个文件里会让 tool 排行被技能名污染。
        self._skill_persist_path = (
            persist_path.replace("tool_usage.json", "skill_usage.json")
            if persist_path else None
        )
        # 历史累计（从磁盘载入，只读参考，不参与本次进程快照的 process 部分）
        self._history: dict[str, dict[str, float]] = {}
        self._skill_history: dict[str, dict[str, float]] = {}
        self._dirty_calls = 0
        self._dirty_skill_calls = 0
        self._last_flush = time.time()
        self._last_skill_flush = time.time()
        self._load_history()
        self._load_skill_history()

    # ---------- 持久化 ----------
    def _load_history(self):
        """载入磁盘上的历史累计统计。损坏/缺失一律降级为空，不影响主流程。"""
        if not self._persist_path or not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            tools = data.get("tools") or {}
            for name, t in tools.items():
                if not isinstance(t, dict):
                    continue
                self._history[name] = {
                    "calls": int(t.get("calls") or 0),
                    "errors": int(t.get("errors") or 0),
                    "blocked": int(t.get("blocked") or 0),
                    "total_ms": float(t.get("total_ms") or 0.0),
                    "max_ms": float(t.get("max_ms") or 0.0),
                }
        except Exception:
            self._history = {}

    def _load_skill_history(self):
        """载入技能维度历史统计。损坏/缺失一律降级为空，不影响主流程。"""
        if not self._skill_persist_path or not os.path.exists(self._skill_persist_path):
            return
        try:
            with open(self._skill_persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            skills = data.get("skills") or {}
            for name, s in skills.items():
                if not isinstance(s, dict):
                    continue
                self._skill_history[name] = {
                    "loads": int(s.get("loads") or 0),
                    "installs": int(s.get("installs") or 0),
                    "uninstalls": int(s.get("uninstalls") or 0),
                    "errors": int(s.get("errors") or 0),
                }
        except Exception:
            self._skill_history = {}

    def _flush_skill_locked(self, force: bool = False):
        """技能统计落盘（与工具统计同样的节流 + 原子写 + 合并语义）。

        技能调用频次远低于工具（历史 18 次 / 数天），但仍沿用节流逻辑，
        以免未来技能自动加载等场景变成高频写入。
        """
        if not self._skill_persist_path:
            return
        now = time.time()
        if not force:
            if (self._dirty_skill_calls < CALLS_BEFORE_FLUSH
                    and (now - self._last_skill_flush) < FLUSH_INTERVAL):
                return

        merged: dict[str, dict[str, float]] = {}
        for name, s in self._skill_history.items():
            merged[name] = dict(s)
        for name, s in self._skills.items():
            cur = merged.setdefault(
                name, {"loads": 0, "installs": 0, "uninstalls": 0, "errors": 0}
            )
            for k in ("loads", "installs", "uninstalls", "errors"):
                cur[k] += s[k]

        payload = {
            "version": 1,
            "updated_at": now,
            "skills": {
                n: {k: int(v[k]) for k in ("loads", "installs", "uninstalls", "errors")}
                for n, v in merged.items()
            },
        }
        try:
            os.makedirs(os.path.dirname(self._skill_persist_path), exist_ok=True)
            tmp = self._skill_persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._skill_persist_path)
            self._skill_history = merged
            for s in self._skills.values():
                for k in ("loads", "installs", "uninstalls", "errors"):
                    s[k] = 0
            self._dirty_skill_calls = 0
            self._last_skill_flush = now
        except Exception:
            # 写盘失败不影响主流程，下次再试
            pass

    def _flush_locked(self, force: bool = False):
        """把「历史 + 本次进程」合并写盘。调用方必须已持有 _lock。

        节流：非 force 时，距上次落盘不足 FLUSH_INTERVAL 秒且累计调用不足
        CALLS_BEFORE_FLUSH 次则跳过，避免高频工具拖慢执行。
        """
        if not self._persist_path:
            return
        now = time.time()
        if not force:
            if (self._dirty_calls < CALLS_BEFORE_FLUSH
                    and (now - self._last_flush) < FLUSH_INTERVAL):
                return

        merged: dict[str, dict[str, float]] = {}
        for name, t in self._history.items():
            merged[name] = dict(t)
        for name, t in self._tools.items():
            cur = merged.setdefault(
                name, {"calls": 0, "errors": 0, "blocked": 0, "total_ms": 0.0, "max_ms": 0.0}
            )
            cur["calls"] += t["calls"]
            cur["errors"] += t["errors"]
            cur["blocked"] += t["blocked"]
            cur["total_ms"] += t["total_ms"]
            cur["max_ms"] = max(cur["max_ms"], t["max_ms"])

        payload = {
            "version": 1,
            "updated_at": now,
            "tools": {
                n: {
                    "calls": int(v["calls"]),
                    "errors": int(v["errors"]),
                    "blocked": int(v["blocked"]),
                    "total_ms": round(v["total_ms"], 2),
                    "max_ms": round(v["max_ms"], 2),
                }
                for n, v in merged.items()
            },
        }
        try:
            os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
            tmp = self._persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._persist_path)
            # 落盘后把本次进程计数并入 history 并清零，避免重复累加
            self._history = merged
            for t in self._tools.values():
                t["calls"] = 0
                t["errors"] = 0
                t["blocked"] = 0
                t["total_ms"] = 0.0
                t["max_ms"] = 0.0
            self._dirty_calls = 0
            self._last_flush = now
        except Exception:
            # 写盘失败不影响主流程，下次再试
            pass

    def flush(self):
        """强制落盘（进程退出时调用）。"""
        with _lock:
            self._flush_locked(force=True)
            self._flush_skill_locked(force=True)

    # ---------- 计数器 ----------
    def inc(self, name: str, value: float = 1.0):
        with _lock:
            self._counters[name] += value

    # ---------- 工具调用 ----------
    def record_tool(self, tool: str, duration_ms: float, status: str = "ok"):
        with _lock:
            t = self._tools[tool or "unknown"]
            t["calls"] += 1
            t["total_ms"] += duration_ms
            t["max_ms"] = max(t["max_ms"], duration_ms)
            if status in ("error",):
                t["errors"] += 1
            elif status.startswith("blocked"):
                t["blocked"] += 1
            self._counters["tool_calls_total"] += 1
            if status == "error":
                self._counters["tool_errors_total"] += 1
            self._dirty_calls += 1
            # 节流落盘：累计够量或超时才写，避免高频工具每次调用都 fsync
            self._flush_locked()

    # ---------- 技能加载 ----------
    def record_skill(self, name: str, action: str = "load", status: str = "ok"):
        """记录一次技能操作（技能名粒度，而非工具名粒度）。

        工具统计只能记到 "Skills" 这一层，无法回答「哪个技能真被用过」。
        这里按 skill name 累计，为「技能摘要该保留哪些 / 该延迟加载哪些」
        提供长期数据支撑。
        """
        with _lock:
            s = self._skills[name or "unknown"]
            action = (action or "load").strip().lower()
            if action == "install":
                s["installs"] += 1
            elif action == "uninstall":
                s["uninstalls"] += 1
            elif action == "load":
                s["loads"] += 1
            # list / market 属于「查看」，不计入技能使用，避免噪声
            if status == "error":
                s["errors"] += 1
            self._dirty_skill_calls += 1
            self._flush_skill_locked()

    # ---------- LLM 调用 ----------
    def record_llm(self, model: str, duration_ms: float, status: str = "ok"):
        with _lock:
            m = self._llm[model or "unknown"]
            m["calls"] += 1
            m["total_ms"] += duration_ms
            if status == "error":
                m["errors"] += 1
            self._counters["llm_calls_total"] += 1
            if status == "error":
                self._counters["llm_errors_total"] += 1

    # ---------- 导出 ----------
    def snapshot(self) -> dict[str, Any]:
        with _lock:
            # 本次进程统计
            tools = {}
            for name, t in self._tools.items():
                calls = t["calls"] or 1
                tools[name] = {
                    "calls": int(t["calls"]),
                    "errors": int(t["errors"]),
                    "blocked": int(t["blocked"]),
                    "avg_ms": round(t["total_ms"] / calls, 2),
                    "max_ms": round(t["max_ms"], 2),
                }
            # 累计统计（历史落盘 + 本次进程未落盘部分）
            lifetime = {}
            names = set(self._history) | set(self._tools)
            for name in names:
                h = self._history.get(name) or {}
                t = self._tools.get(name) or {}
                calls = int(h.get("calls", 0)) + int(t.get("calls", 0))
                if calls <= 0:
                    continue
                total_ms = float(h.get("total_ms", 0.0)) + float(t.get("total_ms", 0.0))
                lifetime[name] = {
                    "calls": calls,
                    "errors": int(h.get("errors", 0)) + int(t.get("errors", 0)),
                    "blocked": int(h.get("blocked", 0)) + int(t.get("blocked", 0)),
                    "avg_ms": round(total_ms / calls, 2),
                    "max_ms": round(max(float(h.get("max_ms", 0.0)),
                                        float(t.get("max_ms", 0.0))), 2),
                }
            llm = {}
            for name, m in self._llm.items():
                calls = m["calls"] or 1
                llm[name] = {
                    "calls": int(m["calls"]),
                    "errors": int(m["errors"]),
                    "avg_ms": round(m["total_ms"] / calls, 2),
                }
            # 技能维度：本次进程 + 跨重启累计
            skills = {
                n: {k: int(s[k]) for k in ("loads", "installs", "uninstalls", "errors")}
                for n, s in self._skills.items()
                if int(s["loads"]) or int(s["installs"]) or int(s["uninstalls"]) or int(s["errors"])
            }
            skill_lifetime = {}
            for name in set(self._skill_history) | set(self._skills):
                h = self._skill_history.get(name) or {}
                s = self._skills.get(name) or {}
                total = sum(int(h.get(k, 0)) + int(s.get(k, 0))
                            for k in ("loads", "installs", "uninstalls"))
                if total <= 0:
                    continue
                skill_lifetime[name] = {
                    k: int(h.get(k, 0)) + int(s.get(k, 0))
                    for k in ("loads", "installs", "uninstalls", "errors")
                }
            return {
                "uptime_seconds": round(time.time() - self._start_ts, 1),
                "counters": {k: round(v, 2) for k, v in self._counters.items()},
                "tools": tools,
                "tools_lifetime": lifetime,
                "skills": skills,
                "skills_lifetime": skill_lifetime,
                "llm": llm,
                "persist_path": self._persist_path,
                "skill_persist_path": self._skill_persist_path,
            }

    def to_prometheus(self) -> str:
        """导出为 Prometheus 文本格式（便于接入抓取）。"""
        snap = self.snapshot()
        lines = []
        for k, v in snap["counters"].items():
            lines.append(f"# TYPE bt_{k} counter")
            lines.append(f"bt_{k} {v}")
        for tool, t in snap["tools"].items():
            safe = tool.replace("-", "_").replace(".", "_")
            lines.append(f'bt_tool_calls_total{{tool="{tool}"}} {t["calls"]}')
            lines.append(f'bt_tool_errors_total{{tool="{tool}"}} {t["errors"]}')
            lines.append(f'bt_tool_avg_ms{{tool="{tool}"}} {t["avg_ms"]}')
        for model, m in snap["llm"].items():
            lines.append(f'bt_llm_calls_total{{model="{model}"}} {m["calls"]}')
            lines.append(f'bt_llm_errors_total{{model="{model}"}} {m["errors"]}')
            lines.append(f'bt_llm_avg_ms{{model="{model}"}} {m["avg_ms"]}')
        # 技能加载：用 lifetime 而非本次进程，技能使用频率天级，进程级无参考价值
        for skill, s in snap.get("skills_lifetime", {}).items():
            lines.append(f'bt_skill_loads_total{{skill="{skill}"}} {s["loads"]}')
            lines.append(f'bt_skill_errors_total{{skill="{skill}"}} {s["errors"]}')
        lines.append(f"bt_uptime_seconds {snap['uptime_seconds']}")
        return "\n".join(lines) + "\n"

    def reset(self, include_history: bool = False):
        """清空统计。默认只清本次进程数据；include_history=True 时连持久化文件一并清空。"""
        with _lock:
            self._counters.clear()
            self._tools.clear()
            self._llm.clear()
            self._skills.clear()
            self._start_ts = time.time()
            if include_history:
                self._history.clear()
                self._skill_history.clear()
                self._dirty_calls = 0
                self._dirty_skill_calls = 0
                self._flush_locked(force=True)
                self._flush_skill_locked(force=True)


_registry: MetricsRegistry | None = None
_reg_lock = threading.Lock()


def default_metrics_dir() -> str:
    """统计文件所在目录。

    优先级：BT_METRICS_DIR（测试/多实例隔离用）> BT_BASE_DIR/metrics > cwd/metrics。
    测试必须设置 BT_METRICS_DIR，否则会写进生产统计文件、污染长期决策数据。
    """
    explicit = os.environ.get("BT_METRICS_DIR")
    if explicit:
        return explicit
    base = os.environ.get("BT_BASE_DIR") or os.getcwd()
    return os.path.join(base, "metrics")


def default_persist_path() -> str:
    """工具统计默认落盘路径（技能统计由它派生为同目录 skill_usage.json）。"""
    return os.path.join(default_metrics_dir(), "tool_usage.json")


def get_metrics(persist_path: str | None = None) -> MetricsRegistry:
    """取全局指标单例。

    persist_path 仅首次创建时生效（后续调用忽略），指向跨重启累计的
    工具调用统计文件；不传则用 default_persist_path()。

    注意单例语义：谁先调用谁定路径。工具执行入口会先带上项目内路径创建它，
    因此测试若想隔离，必须在**首次调用前**设置 BT_METRICS_DIR 并 reset_metrics()，
    否则会写进生产统计文件。
    """
    global _registry
    if _registry is None:
        with _reg_lock:
            if _registry is None:
                if persist_path is None:
                    try:
                        persist_path = default_persist_path()
                    except Exception:
                        persist_path = None
                _registry = MetricsRegistry(persist_path=persist_path)
                # 进程退出时强制落盘，避免最后不足 CALLS_BEFORE_FLUSH 次的调用丢失
                try:
                    import atexit
                    atexit.register(_registry.flush)
                except Exception:
                    pass
    return _registry


def reset_metrics() -> None:
    """丢弃全局单例（仅测试用）。

    单例一旦创建路径就固定，测试要切换隔离目录必须先调用本函数，
    否则 BT_METRICS_DIR 不会生效。
    """
    global _registry
    with _reg_lock:
        _registry = None


def has_history(min_calls: int = 1) -> bool:
    """是否有跨重启的历史统计数据（任意工具调用次数 >= min_calls）。"""
    try:
        m = get_metrics()
        return any(v.get("calls", 0) >= min_calls for v in m._history.values())
    except Exception:
        return False
