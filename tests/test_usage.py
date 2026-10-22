"""用量统计测试（platform_ext/usage.py）。

覆盖：record_usage 写入 ledger、summary 汇总、window 过滤、成本估算。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """把 ledger 重定向到临时文件。"""
    from platform_ext import usage
    ledger = tmp_path / "usage_ledger.jsonl"
    monkeypatch.setattr(usage, "LEDGER_FILE", str(ledger))
    return ledger


class TestUsage:
    def test_record_appends(self, isolated_ledger):
        from platform_ext import usage
        usage.record_usage(model="gpt-4o", input_tokens=100, output_tokens=50, total_tokens=150)
        rows = usage._read_ledger()
        assert len(rows) == 1
        assert rows[0]["model"] == "gpt-4o"
        assert rows[0]["input_tokens"] == 100
        assert rows[0]["total_tokens"] == 150

    def test_record_zero_skipped(self, isolated_ledger):
        from platform_ext import usage
        usage.record_usage(model="x")
        assert len(usage._read_ledger()) == 0

    def test_cost_estimate(self):
        from platform_ext import usage
        cost = usage._estimate_cost({"model": "gpt-4o", "input_tokens": 1_000_000, "output_tokens": 1_000_000})
        assert abs(cost - 12.5) < 0.01  # 2.5 + 10.0

    def test_summary_total(self, isolated_ledger):
        from platform_ext import usage
        usage.record_usage(model="gpt-4o", input_tokens=1000, output_tokens=500, total_tokens=1500)
        usage.record_usage(model="gpt-4o", input_tokens=2000, output_tokens=1000, total_tokens=3000)
        s = usage._summarize(usage._read_ledger(), "total")
        assert s["summary"]["calls"] == 2
        assert s["summary"]["input_tokens"] == 3000
        assert s["summary"]["output_tokens"] == 1500
        assert s["summary"]["total_tokens"] == 4500

    def test_summary_by_day(self, isolated_ledger):
        from platform_ext import usage
        usage.record_usage(model="x", input_tokens=10, output_tokens=5, total_tokens=15)
        s = usage._summarize(usage._read_ledger(), "by_day")
        assert "by_day" in s
        assert len(s["by_day"]) == 1

    def test_window_filter_today(self, isolated_ledger):
        from platform_ext import usage
        usage.record_usage(model="x", input_tokens=10, output_tokens=5, total_tokens=15)
        bounds = usage._parse_window("today")
        assert bounds is not None
        rows = usage._read_ledger()
        start, end = bounds
        filtered = [r for r in rows if start <= r["ts"] <= end]
        assert len(filtered) == 1

    def test_parse_window_all(self):
        from platform_ext import usage
        assert usage._parse_window("all") is None
        assert usage._parse_window("") is None

    def test_parse_window_range(self):
        from platform_ext import usage
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        bounds = usage._parse_window(f"range:{today}:{today}")
        assert bounds is not None

    def test_router_record_and_summary(self, isolated_ledger):
        from platform_ext import usage
        import asyncio
        usage.record_usage(model="gpt-4o", input_tokens=100, output_tokens=50, total_tokens=150)
        res = asyncio.run(usage.api_usage_summary(window="all"))
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is True
        assert body["data"]["summary"]["calls"] == 1