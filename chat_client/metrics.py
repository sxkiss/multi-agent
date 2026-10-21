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
        self._start_ts = time.time()

        # ---- 持久化（跨重启累计） ----
        self._persist_path = persist_path
        # 历史累计（从磁盘载入，只读参考，不参与本次进程快照的 process 部分）
        self._history: dict[str, dict[str, float]] = {}
        self._dirty_calls = 0
        self._last_flush = time.time()
        self._load_history()

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
            return {
                "uptime_seconds": round(time.time() - self._start_ts, 1),
                "counters": {k: round(v, 2) for k, v in self._counters.items()},
                "tools": tools,
                "tools_lifetime": lifetime,
                "llm": llm,
                "persist_path": self._persist_path,
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
        lines.append(f"bt_uptime_seconds {snap['uptime_seconds']}")
        return "\n".join(lines) + "\n"

    def reset(self, include_history: bool = False):
        """清空统计。默认只清本次进程数据；include_history=True 时连持久化文件一并清空。"""
        with _lock:
            self._counters.clear()
            self._tools.clear()
            self._llm.clear()
            self._start_ts = time.time()
            if include_history:
                self._history.clear()
                self._dirty_calls = 0
                self._flush_locked(force=True)


_registry: MetricsRegistry | None = None
_reg_lock = threading.Lock()


def get_metrics(persist_path: str | None = None) -> MetricsRegistry:
    """取全局指标单例。

    persist_path 仅首次创建时生效（后续调用忽略），指向跨重启累计的
    工具调用统计文件；不传则退化为纯进程内统计（兼容旧行为）。
    """
    global _registry
    if _registry is None:
        with _reg_lock:
            if _registry is None:
                if persist_path is None:
                    try:
                        base = os.environ.get("BT_BASE_DIR") or os.getcwd()
                        persist_path = os.path.join(base, "metrics", "tool_usage.json")
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
