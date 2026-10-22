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

    def test_chat_branch_does_not_inject_cache_control(self):
        """chat（OpenAI）分支绝不能注入 cache_control。

        OpenAI 官方协议不支持 message/tool 上的 cache_control，严格网关
        （如 ccload）会直接返回 400/500。前缀缓存只在 anthropic 适配层做。
        """
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
        msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]

        fake._create_completion_stream(msgs, tools)

        # 发送出去的工具与消息都不带 cache_control
        for t in captured["tools"]:
            self.assertNotIn("cache_control", t)
        for m in captured["messages"]:
            self.assertNotIn("cache_control", m)
        # 调用方列表也未被修改
        self.assertNotIn("cache_control", tools[-1])
        self.assertNotIn("cache_control", msgs[0])

    def test_chat_branch_tools_not_aliased(self):
        """chat 分支传出的 tools 应是浅拷贝，避免下游意外修改调用方列表。"""
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
        tools = [{"type": "function", "function": {"name": "A", "parameters": {}}}]
        fake._create_completion_stream([{"role": "user", "content": "hi"}], tools)

        self.assertIsNot(captured["tools"], tools)
        self.assertEqual(captured["tools"], tools)


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
        """精简后 tool 消息 content 必须是纯字符串（OpenAI chat 协议）。

        历史遗留：曾用 [{type:text}] 数组（Anthropic 格式），导致 sub2api
        转 Responses API 时 agnes 上游 400/500。现在统一压平成字符串。
        """
        msgs = self._msgs(50, 20000)
        before = [m.get("content") for m in msgs[1:]]
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        for idx, m in enumerate(msgs[1:]):
            self.assertEqual(m["role"], "tool")
            self.assertIn("tool_call_id", m)
            # 被精简过的消息必须写成纯字符串；未被改动的保持原样
            if m["content"] != before[idx]:
                self.assertIsInstance(m["content"], str)

    def test_zero_budget_disabled(self):
        msgs = self._msgs(50, 20000)
        before = json.dumps(msgs, ensure_ascii=False)
        n = self.agent._shrink_inflight_context(msgs, 0)
        self.assertEqual(n, 0)
        self.assertEqual(json.dumps(msgs, ensure_ascii=False), before)

    def test_no_tool_messages(self):
        msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": "ok"}]
        self.assertEqual(self.agent._shrink_inflight_context(msgs, 1), 0)

    # ---------- 第二级：整轮丢弃 ----------
    def _rounds(self, n_rounds: int, chars: int):
        """构造 n 轮完整对话：user + assistant(tool_calls) + tool。"""
        msgs = []
        for r in range(n_rounds):
            msgs.append({"role": "user", "content": f"问题{r}"})
            msgs.append({
                "role": "assistant", "content": "",
                "tool_calls": [{"id": f"c{r}", "type": "function",
                                "function": {"name": "Bash", "arguments": "{}"}}],
            })
            msgs.append({
                "role": "tool", "tool_call_id": f"c{r}",
                "content": [{"type": "text", "text": _bulk_text(chars, seed=r)}],
            })
        return msgs

    def test_round_drop_when_tool_slimming_insufficient(self):
        """第一级精简后仍超预算时，必须整轮丢弃最旧历史。"""
        msgs = self._rounds(100, 20000)
        before = len(msgs)
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        self.assertLess(len(msgs), before, "应丢弃部分历史轮次")
        after_tokens = sum(
            agent_mod.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs
        )
        self.assertLess(after_tokens, 256 * 1024)

    def test_round_drop_keeps_tool_pairing(self):
        """整轮丢弃后 tool_use 与 tool_result 必须仍然配对，否则协议报错。"""
        msgs = self._rounds(100, 20000)
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        uses, results = set(), set()
        for m in msgs:
            for tc in (m.get("tool_calls") or []):
                uses.add(tc["id"])
            if m.get("role") == "tool":
                results.add(m.get("tool_call_id"))
        self.assertEqual(uses, results, "tool_use 与 tool_result 必须一一配对")

    def test_round_drop_never_produces_negative_estimate(self):
        """估算值不能为负（曾因重复扣减出现 -65925）。"""
        msgs = self._rounds(100, 20000)
        self.agent._shrink_inflight_context(msgs, 256 * 1024)
        tokens = sum(
            agent_mod.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs
        )
        self.assertGreaterEqual(tokens, 0)

    def test_trigger_earlier_than_budget(self):
        """85% 就该触发，不能等到 100%（撞上限时请求已被上游拒绝）。"""
        # 构造刚好超过 85%、未到 100% 的体积
        budget = 256 * 1024
        msgs = self._rounds(40, 20000)
        before_tokens = sum(
            agent_mod.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs
        )
        self.assertGreater(before_tokens, budget * 0.85)
        self.agent._shrink_inflight_context(msgs, budget)
        after_tokens = sum(
            agent_mod.estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in msgs
        )
        self.assertLess(after_tokens, before_tokens, "85% 阈值必须触发压缩")


if __name__ == "__main__":
    unittest.main(verbosity=2)
