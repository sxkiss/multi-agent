#!/usr/bin/env python
"""
工具消息序列自检测试（unittest）：Agent._sanitize_messages_for_api + _build_messages

存在意义：DeepSeek 等严格校验的上游网关要求 assistant 声明的 tool_calls 与
后续 tool 结果**严格一一对应**，否则以 400（tool calls and tool results do not
match / 记录不完整）拒绝，且悬空记录会永久残留在会话历史里，导致后续每次请求
都失败、用户只能"新建任务"。

覆盖两条防线：
  1. 悬空声明（进程被杀/崩溃时"声明先落库、结果未回填"）→ 发送前补齐合成结果
  2. 无参数工具（arguments 为空）→ 不得被丢弃，应保留并替换为合法空 JSON

运行：
    .venv/bin/python -m unittest tests.test_tool_message_sanitize -v
或（无需 __init__.py）：
    .venv/bin/python tests/test_tool_message_sanitize.py
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client.agent import Agent  # noqa: E402


def _tc(tid, name="Bash", args='{"command":"echo hi"}'):
    return {
        "id": tid,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class SanitizeToolMessagesTest(unittest.TestCase):
    """_sanitize_messages_for_api：清理 + 配对自检"""

    def _declarations_and_results(self, msgs):
        declared = {
            tc["id"]
            for m in msgs
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        }
        replied = {
            m.get("tool_call_id")
            for m in msgs
            if m.get("role") == "tool" and m.get("tool_call_id")
        }
        return declared, replied

    def test_dangling_tool_call_gets_synthetic_result(self):
        """回归核心 bug：声明了工具但无结果 → 必须补齐，否则 DS 网关 400"""
        msgs = [
            {"role": "user", "content": "重启服务"},
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1")]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        declared, replied = self._declarations_and_results(out)
        self.assertEqual(declared, {"t1"})
        self.assertEqual(declared - replied, set(), "悬空 tool_call 未被补齐")
        # 补齐的结果必须紧跟在其 assistant 声明之后
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "tool"])

    def test_partial_results_only_completes_the_missing_ones(self):
        """同一 assistant 声明多个工具、只回填部分 → 只补缺失的那几个"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1"), _tc("t2"), _tc("t3")]},
            {"role": "tool", "tool_call_id": "t2", "content": [{"type": "text", "text": "ok"}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        declared, replied = self._declarations_and_results(out)
        self.assertEqual(declared, {"t1", "t2", "t3"})
        self.assertEqual(declared - replied, set())
        # 原有结果不能被删
        self.assertIn("t2", replied)

    def test_results_follow_their_own_assistant_message(self):
        """多轮场景：补齐的结果必须挂在自己的 assistant 后面，不能串到别一轮"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("a1")]},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "", "tool_calls": [_tc("b1")]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        self.assertEqual([m["role"] for m in out],
                         ["assistant", "tool", "user", "assistant", "tool"])
        self.assertEqual(out[1]["tool_call_id"], "a1")
        self.assertEqual(out[4]["tool_call_id"], "b1")

    def test_empty_arguments_preserved_as_valid_json(self):
        """无参数工具：arguments 为空必须保留并替换为 {}，不得丢弃"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1", "get_system_resources", "")]},
            {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "CPU 20%"}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        declared, replied = self._declarations_and_results(out)
        self.assertEqual(declared, {"t1"}, "空参数 tool_call 被误丢弃")
        self.assertEqual(replied, {"t1"}, "对应 tool 结果被连带丢弃")
        self.assertEqual(out[0]["tool_calls"][0]["function"]["arguments"], "{}")

    def test_invalid_json_still_dropped_together_with_result(self):
        """既有能力不能回退：截断 JSON 的声明连同其结果一起跳过"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1", "Write", '{"content": "abc')]},
            {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "done"}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        declared, replied = self._declarations_and_results(out)
        self.assertEqual(declared, set())
        self.assertEqual(replied, set())

    def test_healthy_history_untouched(self):
        """正常配对的历史不应被改动"""
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1")]},
            {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "ok"}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "tool"])
        declared, replied = self._declarations_and_results(out)
        self.assertEqual(declared - replied, set())
        self.assertEqual(replied - declared, set())

    def test_empty_input(self):
        self.assertEqual(Agent._sanitize_messages_for_api([]), [])


class BuildMessagesEmptyArgsTest(unittest.TestCase):
    """端到端：真实对话历史经 _build_messages 重建后不得丢工具记录

    直接构造临时 session 并调用真实的 memory + _build_messages，
    而不是复刻其内部循环——否则代码回退时测试仍会通过。
    """

    def setUp(self):
        import tempfile
        from chat_client.memory import MemoryManager
        self.tmp = tempfile.mkdtemp()
        self.mem = MemoryManager(session_id="t_build", sessions_dir=self.tmp)
        self.mem.add_message("user", "看看服务器资源占用")
        self.mem.add_message(
            "assistant", "",
            tool_calls=[_tc("t1", "get_system_resources", "")],
        )
        self.mem.add_message(
            "tool", [{"type": "text", "text": "CPU 20%"}], tool_call_id="t1",
        )

    def _build(self):
        """构造最小 Agent 并调用真实的 _build_messages（跳过 __init__ 的重依赖）"""
        agent = Agent.__new__(Agent)
        agent.system_prompt = "你是助手"
        agent.memory = self.mem
        agent.context_window_kb = 0  # 关闭自动压缩，避免触发 LLM 摘要
        return agent._build_messages("")

    def test_empty_args_tool_call_survives_reconstruction(self):
        """回归：if name and args 会把无参数工具整段（声明+结果）抹掉"""
        out = self._build()
        declared = {tc["id"] for m in out if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or [])}
        replied = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
        self.assertEqual(declared, {"t1"}, "重建时空参数工具被丢弃")
        self.assertEqual(replied, {"t1"}, "重建时工具结果被丢弃")

    def test_empty_args_becomes_valid_json_object(self):
        """空参数必须被规范成 {} （由 sanitize 负责）"""
        out = self._build()
        for m in out:
            for tc in m.get("tool_calls") or []:
                if tc["id"] == "t1":
                    self.assertEqual(tc["function"]["arguments"], "{}")

    def test_dangling_call_repaired_end_to_end(self):
        """端到端：历史里留有悬空声明时，最终请求序列必须自洽"""
        self.mem.add_message(
            "assistant", "", tool_calls=[_tc("t2", "Bash", '{"command":"kill 123"}')],
        )
        out = self._build()
        declared = {tc["id"] for m in out if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or [])}
        replied = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
        self.assertEqual(declared - replied, set(), "悬空 tool_call 未被补齐")
        self.assertEqual(declared, {"t1", "t2"})
        # 每条 tool 结果都必须紧跟在其 assistant 声明之后
        for i, m in enumerate(out):
            if m.get("role") == "tool":
                self.assertEqual(out[i - 1].get("role"), "assistant",
                                 "tool 结果未紧跟 assistant 声明")


class InterleavedToolSequenceTest(unittest.TestCase):
    """乱序工具序列必须自愈（网关 11148: tool calls and tool results do not match）

    真实事故：线上会话历史出现两种旧逻辑修不了的形态——
      A. 连续两条 assistant 都带 tool_calls，中间没有任何 tool 结果
      B. tool 结果散落在声明之后很远处（中间隔着别的 assistant/tool）
    旧实现只收集"紧随其后的 tool 块"，遇到「后面是 assistant」就断链，
    实测 788 条窗口里 30 处违规只修掉 1 处，网关仍然 400。
    """

    def _assert_well_formed(self, msgs):
        """每个 assistant 声明后必须紧跟其全部结果；不得有多余/孤儿结果"""
        declared = {tc["id"] for m in msgs if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or [])}
        replied = {m["tool_call_id"] for m in msgs
                   if m.get("role") == "tool" and m.get("tool_call_id")}
        self.assertEqual(declared - replied, set(), "仍有悬空声明")
        self.assertEqual(replied - declared, set(), "仍有无归属结果")
        i, n = 0, len(msgs)
        while i < n:
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                want = [tc["id"] for tc in m["tool_calls"]]
                j = i + 1
                got = []
                while j < n and msgs[j].get("role") == "tool":
                    got.append(msgs[j].get("tool_call_id"))
                    j += 1
                self.assertEqual(got, want, "结果未紧跟声明（顺序/数量不符）")
                i = j
                continue
            self.assertNotEqual(m.get("role"), "tool", "出现孤儿 tool 结果")
            i += 1

    def test_consecutive_assistants_both_with_tool_calls(self):
        """形态 A：连续两条 assistant 各声明工具，结果在两条之后"""
        msgs = [
            {"role": "user", "content": "查一下"},
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1")]},
            {"role": "assistant", "content": "", "tool_calls": [_tc("t2")]},
            {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "r1"}]},
            {"role": "tool", "tool_call_id": "t2", "content": [{"type": "text", "text": "r2"}]},
        ]
        self._assert_well_formed(Agent._sanitize_messages_for_api(msgs))

    def test_result_far_from_its_declaration(self):
        """形态 B：结果与声明相隔多条消息，必须被搬回声明之后"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [_tc("t1")]},
            {"role": "assistant", "content": "思考中"},
            {"role": "assistant", "content": "", "tool_calls": [_tc("t2")]},
            {"role": "tool", "tool_call_id": "t2", "content": [{"type": "text", "text": "r2"}]},
            {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": "r1"}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        self._assert_well_formed(out)
        # t1 的结果必须紧跟 t1 的声明之后，不能留在末尾
        for i, m in enumerate(out):
            if m.get("role") == "assistant" and any(
                tc["id"] == "t1" for tc in (m.get("tool_calls") or [])
            ):
                self.assertEqual(out[i + 1].get("tool_call_id"), "t1",
                                 "t1 结果未被搬回其声明之后")

    def test_mixed_interleaved_sequence_from_real_history(self):
        """真实历史片段（529-539 形态）：乱序 + 悬空混合"""
        msgs = [
            {"role": "user", "content": "排查 503"},
            {"role": "assistant", "content": "", "tool_calls": [_tc("a1")]},
            {"role": "assistant", "content": "", "tool_calls": [_tc("a2")]},
            {"role": "tool", "tool_call_id": "a1", "content": [{"type": "text", "text": "r1"}]},
            {"role": "tool", "tool_call_id": "a2", "content": [{"type": "text", "text": "r2"}]},
            {"role": "assistant", "content": "", "tool_calls": [_tc("b1")]},
            {"role": "assistant", "content": "", "tool_calls": [_tc("b2")]},
            {"role": "tool", "tool_call_id": "b2", "content": [{"type": "text", "text": "r"}]},
            {"role": "assistant", "content": "继续"},
            {"role": "tool", "tool_call_id": "b1", "content": [{"type": "text", "text": "rb"}]},
            {"role": "assistant", "content": "", "tool_calls": [_tc("c1")]},  # 悬空
        ]
        self._assert_well_formed(Agent._sanitize_messages_for_api(msgs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
