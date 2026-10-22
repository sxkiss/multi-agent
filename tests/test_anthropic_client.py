#!/usr/bin/env python
"""Anthropic Messages API 适配层测试。

覆盖：工具定义转换、消息历史转换（assistant tool_use + tool_result）、
请求体组装、流事件翻译回 chat.delta 形状、agent 协议分流。
"""
import os
import sys
import unittest
from types import SimpleNamespace

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client import anthropic_client as ac


def ns(**kw):
    return SimpleNamespace(**kw)


class ConvertToolsTest(unittest.TestCase):
    def test_openai_tool_to_anthropic(self):
        tools = [{
            "type": "function",
            "function": {
                "name": "Bash",
                "description": "Run shell",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            },
        }]
        out = ac.convert_tools(tools)
        self.assertEqual(out[0]["name"], "Bash")
        self.assertEqual(out[0]["input_schema"]["required"], ["command"])

    def test_empty_tools(self):
        self.assertEqual(ac.convert_tools(None), [])


class BuildRequestTest(unittest.TestCase):
    def test_message_history_conversion(self):
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "file1"},
        ]
        req = ac.build_request(messages, None, "auto", max_output_tokens=100)
        self.assertEqual(req["model"], "auto")
        # system 现在是块数组（Anthropic 官方前缀缓存写法），文本内容不变
        self.assertEqual(req["system"][0]["text"], "你是助手")
        self.assertEqual(req["system"][0]["cache_control"], {"type": "ephemeral"})
        # tool_result 是独立 user 消息，因此共 3 条：user + assistant + user(tool_result)
        self.assertEqual(len(req["messages"]), 3)
        # assistant 消息 content：tool_use
        asst = req["messages"][1]
        self.assertEqual(asst["role"], "assistant")
        self.assertEqual(asst["content"][0]["type"], "tool_use")
        self.assertEqual(asst["content"][0]["id"], "call_1")
        self.assertEqual(asst["content"][0]["input"], {"command": "ls"})
        # 最后的 user 消息：tool_result
        last = req["messages"][2]
        self.assertEqual(last["role"], "user")
        self.assertEqual(last["content"][0]["type"], "tool_result")
        self.assertEqual(last["content"][0]["tool_use_id"], "call_1")
        self.assertEqual(last["content"][0]["content"], "file1")

    def test_tool_result_attached_to_previous_assistant(self):
        messages = [
            {"role": "assistant", "content": "hi", "tool_calls": [
                {"id": "c2", "function": {"name": "Read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c2", "content": "data"},
        ]
        req = ac.build_request(messages, None, "auto", max_output_tokens=10)
        # assistant 消息含 text + tool_use；tool_result 是独立 user 消息
        self.assertEqual(len(req["messages"]), 2)
        asst_content = req["messages"][0]["content"]
        self.assertEqual(asst_content[0]["type"], "text")
        self.assertEqual(asst_content[1]["type"], "tool_use")
        user_content = req["messages"][1]["content"]
        self.assertEqual(user_content[0]["type"], "tool_result")
        self.assertEqual(user_content[0]["tool_use_id"], "c2")

    def test_thinking_enabled_with_reasoning_effort(self):
        req = ac.build_request([{"role": "user", "content": "hi"}], None, "auto",
                               reasoning_effort="high", max_output_tokens=500)
        self.assertEqual(req["thinking"]["type"], "enabled")


class StreamAdapterTest(unittest.TestCase):
    def test_iter_parses_text_and_tool_use(self):
        events = [
            {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 5, "output_tokens": 0, "total_tokens": 5}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "想想"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "继续想"}},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "你好"}},
            {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "tu_1", "name": "Bash"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"command":"ls"}'}},
            {"type": "message_delta", "usage": {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15}},
            {"type": "message_stop"},
        ]
        chunks = list(ac.iter_anthropic_chunks(events))
        # usage 起始
        self.assertTrue(any(c.usage is not None for c in chunks))
        # reasoning
        reasoning = "".join(c.choices[0].delta.reasoning_content or "" for c in chunks if c.choices and getattr(c.choices[0].delta, "reasoning_content", None))
        self.assertIn("想想", reasoning)
        self.assertIn("继续想", reasoning)
        # text
        text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices and c.choices[0].delta.content)
        self.assertEqual(text, "你好")
        # tool calls
        tcs = [c for c in chunks if c.choices and c.choices[0].delta.tool_calls]
        self.assertTrue(tcs)
        # 第一个 tool_call chunk：name + id（来自 content_block_start）
        first = tcs[0].choices[0].delta.tool_calls[0]
        self.assertEqual(first.function.name, "Bash")
        self.assertEqual(first.id, "tu_1")
        # 最后一个 chunk（input_json_delta）：只带 args，不带 name/id
        last = tcs[-1].choices[0].delta.tool_calls[0]
        self.assertEqual(last.function.name, "")
        self.assertEqual(last.function.arguments, '{"command":"ls"}')

    def test_empty_stream(self):
        self.assertEqual(list(ac.iter_anthropic_chunks([])), [])


class TestAdjacentMessageMerge(unittest.TestCase):
    """Anthropic 协议要求 user/assistant 严格交替；相邻同 role 必须合并。

    回归背景：一轮并行调用多个工具时，每个 tool 消息各自转成一条 user，
    产生连续 user 消息，上游拒绝整个请求（多轮对话 500「不符合 API 协议」）。
    """

    def test_parallel_tool_results_merged_into_one_user_message(self):
        messages = [
            {"role": "user", "content": "run two tools"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "Bash", "arguments": '{"command":"ls"}'}},
                {"id": "c2", "function": {"name": "Read", "arguments": '{"file_path":"a"}'}},
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "out1"},
            {"role": "tool", "tool_call_id": "c2", "content": "out2"},
            {"role": "user", "content": "thanks"},
        ]
        req = ac.build_request(messages, None, "auto")
        roles = [m["role"] for m in req["messages"]]
        # 严格交替
        for a, b in zip(roles, roles[1:]):
            self.assertNotEqual(a, b, f"相邻同 role: {roles}")
        # 两个 tool_result 落在同一条 user 消息里
        result_msgs = [
            m for m in req["messages"]
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        ]
        self.assertEqual(len(result_msgs), 1)
        tool_results = [b for b in result_msgs[0]["content"] if b.get("type") == "tool_result"]
        self.assertEqual(len(tool_results), 2)
        self.assertEqual(
            {b["tool_use_id"] for b in tool_results}, {"c1", "c2"}
        )

    def test_adjacent_user_text_merged(self):
        messages = [
            {"role": "user", "content": "第一条"},
            {"role": "user", "content": "第二条"},
            {"role": "assistant", "content": "好"},
        ]
        req = ac.build_request(messages, None, "auto")
        roles = [m["role"] for m in req["messages"]]
        self.assertEqual(roles, ["user", "assistant"])
        texts = [b["text"] for b in req["messages"][0]["content"]]
        self.assertEqual(texts, ["第一条", "第二条"])


if __name__ == "__main__":
    unittest.main()
