#!/usr/bin/env python3
"""MCP 按需加载 + 工具调用持久化统计 回归测试（2026-10-21）。

覆盖：
1. MetricsRegistry 跨重启持久化（落盘 / 载入 / 累计 / 节流 / reset）
2. MCP 工具延迟加载过滤（默认不下发、解锁后下发、关闭开关回退全量）
3. ToolSearch 搜索 / 去重 / 解锁 / 空结果回退
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_client.metrics as metrics_mod


class MetricsPersistenceTest(unittest.TestCase):
    """工具调用统计跨重启持久化"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bt_metrics_")
        self.path = os.path.join(self.tmp, "metrics", "tool_usage.json")

    def test_flush_writes_file(self):
        r = metrics_mod.MetricsRegistry(persist_path=self.path)
        for _ in range(25):
            r.record_tool("Bash", 100, "ok")
        r.flush()
        self.assertTrue(os.path.exists(self.path))
        data = json.load(open(self.path, encoding="utf-8"))
        self.assertEqual(data["tools"]["Bash"]["calls"], 25)

    def test_survives_restart(self):
        r1 = metrics_mod.MetricsRegistry(persist_path=self.path)
        for _ in range(10):
            r1.record_tool("Read", 20, "ok")
        r1.flush()

        # 模拟进程重启：新实例从同一文件载入
        r2 = metrics_mod.MetricsRegistry(persist_path=self.path)
        snap = r2.snapshot()
        self.assertEqual(snap["tools_lifetime"]["Read"]["calls"], 10)
        # 本次进程尚无调用
        self.assertEqual(snap["tools"], {})

    def test_accumulates_across_restarts(self):
        r1 = metrics_mod.MetricsRegistry(persist_path=self.path)
        for _ in range(5):
            r1.record_tool("Grep", 10, "ok")
        r1.flush()

        r2 = metrics_mod.MetricsRegistry(persist_path=self.path)
        for _ in range(3):
            r2.record_tool("Grep", 10, "ok")
        snap = r2.snapshot()
        self.assertEqual(snap["tools_lifetime"]["Grep"]["calls"], 8)

    def test_throttle_does_not_write_every_call(self):
        """未达阈值时不应每次调用都落盘（避免高频工具拖慢执行）。"""
        r = metrics_mod.MetricsRegistry(persist_path=self.path)
        r.record_tool("Bash", 10, "ok")
        # 1 次调用 < CALLS_BEFORE_FLUSH(20)，且距上次落盘不足 60s → 不写
        self.assertFalse(os.path.exists(self.path))
        r.flush()
        self.assertTrue(os.path.exists(self.path))

    def test_corrupt_file_degrades_gracefully(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")
        r = metrics_mod.MetricsRegistry(persist_path=self.path)
        self.assertEqual(r.snapshot()["tools_lifetime"], {})
        # 仍可正常记录
        r.record_tool("X", 1, "ok")
        self.assertEqual(r.snapshot()["tools"]["X"]["calls"], 1)

    def test_reset_history(self):
        r = metrics_mod.MetricsRegistry(persist_path=self.path)
        for _ in range(5):
            r.record_tool("A", 1, "ok")
        r.flush()
        r.reset(include_history=True)
        self.assertEqual(r.snapshot()["tools_lifetime"], {})

    def test_no_persist_path_is_process_only(self):
        r = metrics_mod.MetricsRegistry(persist_path=None)
        r.record_tool("A", 1, "ok")
        r.flush()  # 不应抛异常
        self.assertEqual(r.snapshot()["tools"]["A"]["calls"], 1)


class McpDeferTest(unittest.TestCase):
    """MCP 工具延迟加载"""

    def _agent(self, config=None):
        import chat_client.agent as A
        a = A.Agent.__new__(A.Agent)
        a.config = config or {}
        a.session_id = "defer_test"
        return a

    def test_enabled_by_default(self):
        a = self._agent()
        self.assertTrue(a._mcp_defer_enabled())

    def test_can_disable_via_config(self):
        a = self._agent({"mcp_tool_defer": False})
        self.assertFalse(a._mcp_defer_enabled())

    def test_can_disable_via_string(self):
        a = self._agent({"mcp_tool_defer": "off"})
        self.assertFalse(a._mcp_defer_enabled())

    def test_env_override(self):
        a = self._agent()
        old = os.environ.get("AI_AGENT_MCP_DEFER")
        try:
            os.environ["AI_AGENT_MCP_DEFER"] = "0"
            self.assertFalse(a._mcp_defer_enabled())
            os.environ["AI_AGENT_MCP_DEFER"] = "1"
            self.assertTrue(a._mcp_defer_enabled())
        finally:
            if old is None:
                os.environ.pop("AI_AGENT_MCP_DEFER", None)
            else:
                os.environ["AI_AGENT_MCP_DEFER"] = old


class ToolSearchTest(unittest.TestCase):
    """ToolSearch 搜索与解锁"""

    def setUp(self):
        from chat_client.tools import registry, _current_job_local
        self.registry = registry
        self.job_local = _current_job_local
        # 伪造一批 MCP 工具（含点号原名 + 清洗别名，验证去重）
        self.added = []
        for raw in ("doc.copy_format", "doc.create", "slide.add_slide", "sheet.set_cell"):
            self._fake_register(raw)
        self._fake_register("plain_tool")

        class J:
            session_id = "ts_unit"
        self._old_job = getattr(self.job_local, "job", None)
        self.job_local.job = J()

    def tearDown(self):
        from chat_client.tools import clear_unlocked_mcp_tools
        clear_unlocked_mcp_tools("ts_unit")
        for name in self.added:
            self.registry._tools.pop(name, None)
            self.registry._metadata.pop(name, None)
        self.job_local.job = self._old_job

    def _fake_register(self, name):
        from chat_client.tools import sanitize_tool_name
        safe = sanitize_tool_name(name)
        for n in {name, safe}:
            if n in self.registry._metadata:
                continue
            self.registry._metadata[n] = {
                "id": name,
                "name": name,
                "name_cn": "",
                "category": "mcp",
                "risk_level": "medium",
                "description": f"tool {name}",
                "show": True,
                "timeout": None,
            }
            self.added.append(n)

    def test_search_matches_keyword(self):
        from chat_client.tools import ToolSearch, get_unlocked_mcp_tools
        out = ToolSearch(query="slide", limit=5)
        self.assertIn("slide.add_slide", out)
        self.assertIn("slide.add_slide", get_unlocked_mcp_tools("ts_unit"))

    def test_dedup_alias(self):
        """含点号工具不应因其清洗别名被重复计数/重复列出。"""
        from chat_client.tools import ToolSearch
        out = ToolSearch(query="doc", limit=50)
        # doc.copy_format 只出现一次（不因 doc_copy_format 别名再出现一次）
        self.assertEqual(out.count("doc.copy_format"), 1)

    def test_empty_query_lists_all(self):
        from chat_client.tools import ToolSearch
        out = ToolSearch(query="", limit=3)
        self.assertIn("已解锁 3 个", out)

    def test_chinese_query_falls_back_to_list(self):
        """中文查询若无法匹配任何工具名或描述，应回退列出全部而非返回空。"""
        from chat_client.tools import ToolSearch
        out = ToolSearch(query="飞控发动机", limit=3)
        self.assertIn("没有找到", out)
        self.assertIn("已解锁", out)

    def test_limit_clamped(self):
        from chat_client.tools import ToolSearch
        out = ToolSearch(query="", limit=999)
        self.assertIn("已解锁", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
