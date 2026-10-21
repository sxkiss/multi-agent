#!/usr/bin/env python3
"""上下文优化回归测试（2026-10-21）。

覆盖两项改动：
1. 前缀缓存：anthropic 请求体的 system / tools 带 cache_control；
   chat 协议分支的 tools 末项带 cache_control，且不污染调用方列表。
2. 单轮内上下文闸门：工具循环中 messages 超预算时精简最旧工具结果，
   最近 N 条保持原样，完整内容可归档召回。
"""
import json
import os
import random
import string
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chat_client.anthropic_client as ac
import chat_client.agent as agent_mod


def _bulk_text(n: int, seed: int = 1) -> str:
    """生成接近真实分布的文本（重复字符会被 tiktoken 高度压缩，不适合做体积测试）。"""
    rnd = random.Random(seed)
    alphabet = string.ascii_letters + string.digits + " \n"
    return "".join(rnd.choice(alphabet) for _ in range(n))


class CacheControlTest(unittest.TestCase):
    """① 前缀缓存标记"""

    def test_anthropic_system_has_cache_control(self):
        req = ac.build_request(
            [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}],
            None, "auto",
        )
        self.assertIsInstance(req["system"], list)
        self.assertEqual(req["system"][0]["type"], "text")
        self.assertEqual(req["system"][0]["text"], "你是助手")
        self.assertEqual(req["system"][0]["cache_control"], {"type": "ephemeral"})

    def test_anthropic_tools_last_has_cache_control(self):
        tools = [
            {"type": "function", "function": {"name": "A", "description": "a",
                                              "parameters": {"type": "object", "properties": {}}}},
            {"type": "function", "function": {"name": "B", "description": "b",
                                              "parameters": {"type": "object", "properties": {}}}},
        ]
        req = ac.build_request([{"role": "user", "content": "hi"}], tools, "auto")
        self.assertEqual(len(req["tools"]), 2)
        self.assertEqual(req["tools"][-1]["cache_control"], {"type": "ephemeral"})
        # 只有最后一个带标记（整个数组构成缓存前缀）
        self.assertNotIn("cache_control", req["tools"][0])

    def test_anthropic_no_tools_no_error(self):
        req = ac.build_request([{"role": "user", "content": "hi"}], None, "auto")
        self.assertNotIn("tools", req)

    def test_chat_tools_cache_control_does_not_mutate_caller(self):
        """chat 分支给 tools 打标记时必须浅拷贝，不能污染调用方列表。"""
        fake = agent_mod.Agent.__new__(agent_mod.Agent)
        fake.model_name = "m"
        fake.temperature = 1.0
        fake.top_p = 0.9
        fake.reasoning_effort = None
        fake.max_output_tokens = 0
        fake.thinking = False
        fake.config = {}
        fake.api_protocol = "chat"

        captured = {}

        class _Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        captured.update(kwargs)
                        return "ok"

        fake.client = _Client()
        tools = [
            {"type": "function", "function": {"name": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "B", "parameters": {}}},
        ]
        original_last = dict(tools[-1])

        fake._create_completion_stream([{"role": "user", "content": "hi"}], tools)

        # 发送出去的最后一个工具带缓存标记
        self.assertEqual(captured["tools"][-1]["cache_control"], {"type": "ephemeral"})
        # 调用方列表未被修改
        self.assertEqual(tools[-1], original_last)
        self.assertNotIn("cache_control", tools[-1])


class InflightCompressTest(unittest.TestCase):
    """② 单轮内上下文闸门"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bt_inflight_")
        self.agent = agent_mod.Agent.__new__(agent_mod.Agent)
        self.agent.config = {"base_dir": self.tmp}
        self.agent.session_id = "inflight_test"
        self.agent.context_window_kb = 256

    def _msgs(self, count: int, chars: int):
        msgs = [{"role": "user", "content": "go"}]
        for i in range(count):
            msgs.append({
                "role": "tool",
                "tool_call_id": f"c{i}",
                "content": [{"type": "text", "text": _bulk_text(chars, seed=i)}],
            })
        return msgs

    def test_no_compress_under_budget(self):
        msgs = self._msgs(3, 1000)
        before = json.dumps(msgs, ensure_ascii=False)
        n = self.agent._shrink_inflight_context(msgs, 256 * 1024)
        self.assertEqual(n, 0)
        self.assertEqual(json.dumps(msgs, ensure_ascii=False), before)

    def test_compress_over_budget(self):
        msgs = self._msgs(50, 20000)
        before_tokens = sum(
            agent_mod.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs
        )
        self.assertGreater(before_tokens, 256 * 1024)

        n = self.agent._shrink_inflight_context(msgs, 256 * 1024)

        after_tokens = sum(
            agent_mod.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs
        )
        self.assertGreater(n, 0)
        self.assertLess(after_tokens, before_tokens)

    def test_recent_tools_untouched(self):
        msgs = self._msgs(50, 20000)
        keep = 6
        recent_before = [
            len(m["content"][0]["text"]) for m in msgs[-keep:]
        ]
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        recent_after = [
            len(m["content"][0]["text"]) for m in msgs[-keep:]
        ]
        self.assertEqual(recent_before, recent_after, "最近 N 条工具结果必须原样保留")

    def test_archive_file_written(self):
        msgs = self._msgs(50, 20000)
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        root = os.path.join(self.tmp, "tool_archives", "inflight_test")
        self.assertTrue(os.path.isdir(root), "精简的完整内容必须落盘归档")
        files = [f for f in os.listdir(root) if f.endswith(".txt")]
        self.assertGreater(len(files), 0)

    def test_keeps_message_shape(self):
        """精简后 content 仍是 [{type:text,text:...}] 结构，不能破坏协议形状。"""
        msgs = self._msgs(50, 20000)
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        for m in msgs[1:]:
            self.assertEqual(m["role"], "tool")
            self.assertIsInstance(m["content"], list)
            self.assertEqual(m["content"][0]["type"], "text")
            self.assertIn("tool_call_id", m)

    def test_zero_budget_disabled(self):
        msgs = self._msgs(50, 20000)
        before = json.dumps(msgs, ensure_ascii=False)
        n = self.agent._shrink_inflight_context(msgs, 0)
        self.assertEqual(n, 0)
        self.assertEqual(json.dumps(msgs, ensure_ascii=False), before)

    def test_no_tool_messages(self):
        msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "ok"}]
        self.assertEqual(self.agent._shrink_inflight_context(msgs, 1), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
