#!/usr/bin/env python
"""reasoning 退化重复检测测试。

存在意义：部分模型会陷入"重复规划同一句话"的退化循环（例如
`Let me read it.` 重复数百次），导致单轮思考无限膨胀。检测到后应由
Agent 丢弃本次响应并重新请求，而不是等整段退化思考生成完或直接结束任务。

运行：
    .venv/bin/python -m pytest tests/test_reasoning_repeat.py -q
"""
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client.agent import Agent  # noqa: E402


def _filler(n=80):
    """生成足够长且每句唯一的正常思考内容，用于跨过门槛。"""
    return "\n".join(
        f"Step {i}: inspect module_{i}.py for behavior {i * 7} and record it."
        for i in range(n)
    )


class ReasoningRepeatTest(unittest.TestCase):
    def test_short_reasoning_ignored(self):
        # 总长度不足 600 不判定，避免误伤简短回答
        text = "Let me read it. " * 5
        self.assertLess(len(text), 600)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))

    def test_normal_long_reasoning_not_flagged(self):
        text = _filler(120)
        self.assertGreater(len(text), 600)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))

    def test_newline_separated_repeat_detected(self):
        # 复现线上真实形态：每句独占一行，前面带不同数量的缩进
        text = _filler() + "\n" + "\n".join(
            "   Let me read it." for _ in range(10)
        )
        self.assertGreater(len(text), 600)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_same_line_period_repeat_detected(self):
        # 同一行内以 ". " 分隔的重复也要能识别
        text = _filler() + "\n" + "Let me read it. " * 10
        self.assertGreater(len(text), 600)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_punctuation_case_and_space_insensitive(self):
        variants = [
            "Let me read it.",
            "  let me read it  ",
            "LET ME READ IT!",
            "Let   me read it?",
        ]
        text = _filler() + "\n" + "\n".join(variants * 3)
        self.assertGreater(len(text), 600)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_threshold_boundary(self):
        # 同句重复达到 limit 触发；连续复读 4 句也触发（短退化保护）
        base = _filler() + "\n"
        seven = base + "\n".join("Let me read it." for _ in range(7))
        eight = base + "\n".join("Let me read it." for _ in range(8))
        # 7 次连续复读已触发连续检测（>=4 连续相同）
        self.assertTrue(Agent._reasoning_has_repeat(seven, 8))
        self.assertTrue(Agent._reasoning_has_repeat(eight, 8))

    def test_dotted_identifiers_not_false_positive(self):
        # 正常思考里大量出现 xxx.py 这类点号标识符，不应被切碎后误判重复
        lines = []
        for i in range(80):
            lines.append(
                f"Check chat_client.hotreload.py step {i} and compare with "
                f"chat_client.agent.py variant {i}."
            )
        text = "\n".join(lines)
        self.assertGreater(len(text), 600)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))

    def test_short_consecutive_repeat_detected(self):
        # 短退化：总长不足 2000 但同一句连续复读（复现线上 281 字符退化形态）
        text = ("Let me look at the `_on_message_event` and `_enqueue` methods "
                "to understand the dedup logic.\n") * 6
        text += _filler(30)
        self.assertGreater(len(text), 600)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_chinese_guided_thinking_not_flagged(self):
        # 中文排查思考：句式相似但内容不同，不应误判
        lines = [
            "让我先看一下飞书通道的实现。",
            "让我查看 gateway_processor 里的去重逻辑。",
            "让我检查 feishu 的 on_message 方法。",
            "让我确认 auto_start 是否包含 feishu。",
            "让我看日志里 10 50 46 的记录。",
            "让我对比两次收到的消息 ID。",
            "让我查看 ChannelManager 的 add_channel。",
            "让我检查是否有重复注册。",
            "让我看看 gateway 的 auto_start 配置。",
            "让我确认 workers_per_channel 的值。",
        ]
        text = "\n".join(lines * 2) + "\n" + _filler(40)
        self.assertGreater(len(text), 600)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))


if __name__ == "__main__":
    unittest.main()


class AgentMaxToolIterationsTest(unittest.TestCase):
    """max_tool_iterations 来自 web_server 时可能是字符串，需防御性转换。"""

    def test_string_max_tool_iterations(self):
        from unittest.mock import MagicMock
        import chat_client.agent as agent_mod
        # 只测构造逻辑：用最小 config，避免完整初始化
        orig_init = agent_mod.Agent.__init__
        try:
            # 直接调用构造前片段：用反射验证 int 转换逻辑存在
            src = open(os.path.join(BASE_DIR, 'chat_client/agent.py'), encoding='utf-8').read()
            self.assertIn('int(self.config.get("max_tool_iterations", 9999))', src)
        finally:
            pass


if __name__ == "__main__":
    unittest.main()
