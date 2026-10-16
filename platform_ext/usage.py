"""用量统计（对齐 Octop api/routers/usage.py，轻量版）。

BT 是单用户、无 DB，因此用「追加式 JSONL ledger」记录每次对话的
token 用量与成本，提供汇总 API。

  POST /api/v2/usage/record     （内部）记录一次用量
  GET  /api/v2/usage/summary   汇总：window=today|yesterday|last_7d|last_30d|all|day:YYYY-MM-DD|range:A:B
                                 granularity=total|by_day|by_model

成本估算：按模型 token 单价（每百万 token 美元），可在 config.json
platform_ext.usage 段配置单价；未配置时只返回 token 数。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import public
from platform_ext import PROJECT_ROOT

router = APIRouter()

LEDGER_FILE = os.path.join(PROJECT_ROOT, "usage_ledger.jsonl")
_lock = threading.Lock()

# 默认模型单价（每百万 token，美元）—— 可被 config 覆盖
_DEFAULT_PRICES: dict[str, dict[str, float]] = {
    # model 前缀 → {"input": $/1M, "output": $/1M}
    "gpt-4o": {"input": 2.5, "output": 10.0},
    "gpt-4": {"input": 30.0, "output": 60.0},
    "gpt-3.5": {"input": 0.5, "output": 1.5},
    "claude": {"input": 3.0, "output": 15.0},
    "deepseek": {"input": 0.27, "output": 1.10},
    "qwen": {"input": 0.5, "output": 2.0},
    "glm": {"input": 0.5, "output": 2.0},
    "auto": {"input": 1.0, "output": 3.0},
}

DEFAULT_PRICE = {"input": 1.0, "output": 3.0}


def _price_for(model: str) -> dict[str, float]:
    key = (model or "").lower()
    for prefix, price in _DEFAULT_PRICES.items():
        if key.startswith(prefix):
            return price
    return DEFAULT_PRICE


def _estimate_cost(usage: dict[str, Any]) -> float:
    """按模型单价估算成本（美元）。"""
    price = _price_for(str(usage.get("model") or ""))
    inp = float(usage.get("input_tokens") or 0) / 1_000_000 * price["input"]
    out = float(usage.get("output_tokens") or 0) / 1_000_000 * price["output"]
    return round(inp + out, 6)


def record_usage(
    *,
    session_id: str = "",
    model: str = "",
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int = 0,
    source: str = "chat",
) -> None:
    """追加一条用量记录到 ledger（线程安全，失败静默）。"""
    if total_tokens <= 0 and input_tokens <= 0 and output_tokens <= 0:
        return
    entry = {
        "ts": int(time.time()),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "session_id": session_id,
        "model": model,
        "input_tokens": max(0, input_tokens),
        "output_tokens": max(0, output_tokens),
        "total_tokens": max(0, total_tokens) or (max(0, input_tokens) + max(0, output_tokens)),
        "source": source,
        "cost_usd": _estimate_cost({
            "model": model, "input_tokens": input_tokens, "output_tokens": output_tokens,
        }),
    }
    try:
        with _lock, open(LEDGER_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _read_ledger() -> list[dict[str, Any]]:
    if not os.path.exists(LEDGER_FILE):
        return []
    rows = []
    with open(LEDGER_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _parse_window(window: str) -> tuple[int, int] | None:
    """返回 (start_ts, end_ts)，None 表示全部。"""
    today = datetime.now().strftime("%Y-%m-%d")
    if window in ("", "all"):
        return None
    if window == "today":
        start = datetime.strptime(today, "%Y-%m-%d")
        return int(start.timestamp()), int(time.time())
    if window == "yesterday":
        day = datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)
        return int(day.timestamp()), int(day.timestamp() + 86400)
    if window == "last_7d":
        start = datetime.now() - timedelta(days=7)
        return int(start.timestamp()), int(time.time())
    if window == "last_30d":
        start = datetime.now() - timedelta(days=30)
        return int(start.timestamp()), int(time.time())
    if window.startswith("day:"):
        try:
            d = datetime.strptime(window[4:], "%Y-%m-%d")
            return int(d.timestamp()), int(d.timestamp() + 86400)
        except ValueError:
            return None
    if window.startswith("range:"):
        try:
            a, b = window[6:].split(":")
            da = datetime.strptime(a, "%Y-%m-%d")
            db = datetime.strptime(b, "%Y-%m-%d")
            return int(da.timestamp()), int(db.timestamp() + 86400)
        except ValueError:
            return None
    return None


def _summarize(rows: list[dict[str, Any]], granularity: str) -> dict[str, Any]:
    total_in = sum(r.get("input_tokens") or 0 for r in rows)
    total_out = sum(r.get("output_tokens") or 0 for r in rows)
    total_all = sum(r.get("total_tokens") or 0 for r in rows)
    cost = round(sum(r.get("cost_usd") or 0 for r in rows), 6)
    calls = len(rows)
    by_model: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    )
    by_day: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    )
    for r in rows:
        m = r.get("model") or "unknown"
        bm = by_model[m]
        bm["calls"] += 1
        bm["input_tokens"] += r.get("input_tokens") or 0
        bm["output_tokens"] += r.get("output_tokens") or 0
        bm["total_tokens"] += r.get("total_tokens") or 0
        bm["cost_usd"] = round(bm["cost_usd"] + (r.get("cost_usd") or 0), 6)
        day = r.get("date") or ""
        bd = by_day[day]
        bd["calls"] += 1
        bd["input_tokens"] += r.get("input_tokens") or 0
        bd["output_tokens"] += r.get("output_tokens") or 0
        bd["total_tokens"] += r.get("total_tokens") or 0
        bd["cost_usd"] = round(bd["cost_usd"] + (r.get("cost_usd") or 0), 6)

    summary: dict[str, Any] = {
        "window": {"calls": calls, "input_tokens": total_in, "output_tokens": total_out,
                   "total_tokens": total_all, "cost_usd": cost},
        "by_model": dict(by_model),
        "by_day": dict(sorted(by_day.items())),
    }
    if granularity == "by_day":
        return {"summary": summary["window"], "by_day": summary["by_day"]}
    if granularity == "by_model":
        return {"summary": summary["window"], "by_model": summary["by_model"]}
    return {"summary": summary["window"], "by_day": summary["by_day"], "by_model": summary["by_model"]}


def _ok(data: Any = None, msg: str = "") -> JSONResponse:
    return JSONResponse(public.return_data(True, data=data, msg=msg))


def _err(msg: str) -> JSONResponse:
    return JSONResponse(public.returnMsg(False, msg))


@router.post("/usage/record")
async def api_usage_record(request: Request) -> JSONResponse:
    """内部：记录一次用量（供 agent 在 stop 事件调用）。"""
    try:
        body = await request.json() if request is not None else {}
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    record_usage(
        session_id=str(body.get("session_id") or ""),
        model=str(body.get("model") or ""),
        input_tokens=int(body.get("input_tokens") or 0),
        output_tokens=int(body.get("output_tokens") or 0),
        total_tokens=int(body.get("total_tokens") or 0),
        source=str(body.get("source") or "chat"),
    )
    return _ok({"recorded": True})


@router.get("/usage/summary")
async def api_usage_summary(window: str = "last_30d", granularity: str = "total") -> JSONResponse:
    rows = _read_ledger()
    bounds = _parse_window(window)
    if bounds is not None:
        start, end = bounds
        rows = [r for r in rows if start <= (r.get("ts") or 0) <= end]
    return _ok(_summarize(rows, granularity))


@router.get("/usage/ledger")
async def api_usage_ledger(limit: int = 100, window: str = "last_7d") -> JSONResponse:
    rows = _read_ledger()
    bounds = _parse_window(window)
    if bounds is not None:
        start, end = bounds
        rows = [r for r in rows if start <= (r.get("ts") or 0) <= end]
    rows = rows[-int(limit):] if limit else rows
    return _ok({"entries": list(reversed(rows))})