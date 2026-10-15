"""
@input: threading, time, collections, json, typing
@output: MetricsRegistry / get_metrics — 进程内轻量指标（P3-b 可观测性）
@position: Observability layer — 工具/LLM 调用的计数与耗时统计
@auto-doc: Update header and folder INDEX.md when this file changes

P3-b（2026-10-15，借鉴 Octop 的可观测性思路）：
不引入 Prometheus 依赖，用纯标准库维护进程内计数器/直方图，
通过 /api/metrics 以 JSON（可转 Prometheus 文本）暴露。
覆盖：工具调用次数/耗时/状态、LLM 调用次数/耗时、错误计数。
"""
import json
import threading
import time
from collections import defaultdict
from typing import Any

_lock = threading.Lock()


class MetricsRegistry:
    """进程内指标注册表（线程安全）。"""

    def __init__(self):
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
                "llm": llm,
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

    def reset(self):
        with _lock:
            self._counters.clear()
            self._tools.clear()
            self._llm.clear()
            self._start_ts = time.time()


_registry: MetricsRegistry | None = None
_reg_lock = threading.Lock()


def get_metrics() -> MetricsRegistry:
    global _registry
    if _registry is None:
        with _reg_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry
