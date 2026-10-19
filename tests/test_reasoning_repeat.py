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
    """生成足够长且每句唯一的正常思考内容，用于跨过 2000 字符门槛。"""
    return "\n".join(
        f"Step {i}: inspect module_{i}.py for behavior {i * 7} and record it."
        for i in range(n)
    )


class ReasoningRepeatTest(unittest.TestCase):
    def test_short_reasoning_ignored(self):
        # 即使重复很多次，总长度不足 2000 字符也不判定，避免误伤简短回答
        text = "Let me read it. " * 40
        self.assertLess(len(text), 2000)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))

    def test_normal_long_reasoning_not_flagged(self):
        text = _filler(120)
        self.assertGreater(len(text), 2000)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))

    def test_newline_separated_repeat_detected(self):
        # 复现线上真实形态：每句独占一行，前面带不同数量的缩进
        text = _filler() + "\n" + "\n".join(
            "   Let me read it." for _ in range(10)
        )
        self.assertGreater(len(text), 2000)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_same_line_period_repeat_detected(self):
        # 同一行内以 ". " 分隔的重复也要能识别
        text = _filler() + "\n" + "Let me read it. " * 10
        self.assertGreater(len(text), 2000)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_punctuation_case_and_space_insensitive(self):
        variants = [
            "Let me read it.",
            "  let me read it  ",
            "LET ME READ IT!",
            "Let   me read it?",
        ]
        text = _filler() + "\n" + "\n".join(variants * 3)
        self.assertGreater(len(text), 2000)
        self.assertTrue(Agent._reasoning_has_repeat(text, 8))

    def test_threshold_boundary(self):
        # limit 次才触发，limit-1 次不触发
        base = _filler() + "\n"
        seven = base + "\n".join("Let me read it." for _ in range(7))
        eight = base + "\n".join("Let me read it." for _ in range(8))
        self.assertFalse(Agent._reasoning_has_repeat(seven, 8))
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
        self.assertGreater(len(text), 2000)
        self.assertFalse(Agent._reasoning_has_repeat(text, 8))


if __name__ == "__main__":
    unittest.main()
