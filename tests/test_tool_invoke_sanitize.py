#!/usr/bin/env python
"""
防 <invoke> 伪工具调用污染测试（unittest）：Agent._parse_invoke_tool_calls / _strip_invoke_blocks

存在意义：模型（尤其 GLM/自研系）在长会话/特定任务下偶尔会把工具调用写成
Claude Code 风格的 <invoke name="..."> 文本，而不是标准 function_call。
BT 不解析 <invoke> 文本，导致工具"只回文本不调用"；且历史里的 <invoke>
会诱导模型持续模仿，形成污染循环。

覆盖两条防线：
  1. _parse_invoke_tool_calls：把正文里混入的 <invoke> 解析为 (工具名, 参数) 列表
  2. _strip_invoke_blocks：剥离文本/消息 content 里的 <invoke> 块，防止历史污染
  3. _sanitize_messages_for_api：清洗历史时对 assistant 正文做剥离

运行：
    .venv/bin/python -m unittest tests.test_tool_invoke_sanitize -v
或：
    .venv/bin/python tests/test_tool_invoke_sanitize.py
"""

import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client.agent import Agent  # noqa: E402


class ParseInvokeToolCallsTest(unittest.TestCase):
    """_parse_invoke_tool_calls：解析 <invoke> 文本工具调用"""

    def test_single_bash(self):
        content = (
            '我用 Bash 写文件。\n'
            '<invoke name="Bash">\n'
            '<parameter name="command">echo hi</parameter>\n'
            '<parameter name="description">打招呼</parameter>\n'
            '</invoke>\n然后结束。'
        )
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        name, args = tcs[0]
        self.assertEqual(name, "Bash")
        self.assertEqual(args.get("command"), "echo hi")
        self.assertEqual(args.get("description"), "打招呼")

    def test_multiple(self):
        content = (
            '<invoke name="Read"><parameter name="file_path">/tmp/a</parameter></invoke>'
            '<invoke name="Bash"><parameter name="command">pwd</parameter></invoke>'
        )
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual([n for n, _ in tcs], ["Read", "Bash"])
        self.assertEqual(tcs[0][1]["file_path"], "/tmp/a")
        self.assertEqual(tcs[1][1]["command"], "pwd")

    def test_no_invoke(self):
        self.assertEqual(Agent._parse_invoke_tool_calls("普通文本，无工具调用"), [])

    def test_arbitrary_prefix_invoke(self):
        """不硬编码任何前缀：模型学来的任意前缀（｜U2｜ / 裸 / 标准）都能解析。
        验证框架对上游 token 前缀变化鲁棒（换模型/网关不失效）。"""
        # agnes 风格前缀
        u2 = (
            '｜U2｜invoke name="Bash">\n'
            '｜U2｜parameter name="command" string="true">echo hi</｜U2｜parameter>\n'
            '</｜U2｜invoke>'
        )
        self.assertEqual(Agent._parse_invoke_tool_calls(u2), [("Bash", {"command": "echo hi"})])
        # 裸前缀（无尖括号、无特殊 token）
        bare = 'invoke name="Read">\nparameter name="file_path">/tmp/x</parameter>'
        tcs = Agent._parse_invoke_tool_calls(bare)
        self.assertEqual(tcs[0][0], "Read")
        self.assertEqual(tcs[0][1].get("file_path"), "/tmp/x")

    def test_strip_arbitrary_prefix(self):
        """任意前缀的 <invoke> 文本都能被剥离（防污染）。"""
        u2 = (
            '让我执行命令。\n'
            '｜U2｜invoke name="Bash">\n'
            '｜U2｜parameter name="command" string="true">echo hi</｜U2｜parameter>\n'
            '</｜U2｜invoke>\n收尾'
        )
        cleaned = Agent._strip_invoke_blocks(u2)
        self.assertNotIn("invoke", cleaned)
        self.assertIn("让我执行命令。", cleaned)
        self.assertIn("收尾", cleaned)

    def test_unclosed_invoke(self):
        """不闭合的 <invoke> 块（模型模仿退化，漏掉 </invoke>）也能解析。"""
        content = (
            '让我用 Read 工具直接确认文件状态。\n'
            '<invoke name="Read">\n'
            '<parameter name="file_path" string="true">/home/sxkiss/bt/static/platform_panel.html</parameter>\n'
            '<parameter name="limit" string="false">5</parameter>\n'
            # 没有 </invoke>，块延伸到文本末尾
        )
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        name, args = tcs[0]
        self.assertEqual(name, "Read")
        self.assertEqual(args.get("file_path"), "/home/sxkiss/bt/static/platform_panel.html")
        self.assertEqual(args.get("limit"), "5")

    def test_unclosed_invoke_no_parameter_close(self):
        """不闭合的 <invoke> 且 <parameter> 也漏掉闭合标签的退化形式。"""
        content = (
            '<invoke name="Bash">\n'
            '<parameter name="command">echo hi</parameter>\n'
            '<parameter name="description">打招呼\n'
            # 没有 </parameter> 和 </invoke>
        )
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        name, args = tcs[0]
        self.assertEqual(name, "Bash")
        # 第二个不闭合的 parameter 无法可靠取值，但至少第一个闭合参数要解析出来
        self.assertEqual(args.get("command"), "echo hi")

    def test_entity_unescape(self):
        content = (
            '<invoke name="Write">'
            '<parameter name="file_path">/tmp/a&amp;b.txt</parameter>'
            '</invoke>'
        )
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(tcs[0][1]["file_path"], "/tmp/a&b.txt")

    def test_degenerate_chinese_bracket(self):
        """中文括号退化格式「（执行 Bash）现在直接调用工具，不再输出文字...」：
        识别为退化伪工具调用（带 __degenerate__ 标记，不强行执行）。"""
        content = (
            '（执行 Bash）现在直接调用工具，不再输出文字。给现有 gateway 实例补 '
            'internal_token 并重同步。'
        )
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(len(tcs), 1)
        name, args = tcs[0]
        self.assertEqual(name, "Bash")
        self.assertTrue(args.get("__degenerate__"))
        self.assertIn("internal_token", args.get("instruction", ""))

    def test_degenerate_multiple_blocks(self):
        """同一段退化输出连续重复十几遍时，全部识别并全部剥离（根治重复输出）。"""
        block = (
            '（执行 Bash）现在直接调用工具，不再输出文字。给现有 gateway 实例补 '
            'internal_token 并重同步。'
        )
        content = block * 10
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(len(tcs), 10)
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("执行", cleaned)
        self.assertNotIn("internal_token", cleaned)
        self.assertNotIn("Bash", cleaned)

    def test_degenerate_halfwidth_bracket(self):
        """半角方括号变体 [执行 Read] 也能识别并剥离。"""
        content = '[执行 Read] 查看 /tmp/a 文件内容。'
        tcs = Agent._parse_invoke_tool_calls(content)
        self.assertEqual(tcs[0][0], "Read")
        self.assertTrue(tcs[0][1].get("__degenerate__"))
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("执行", cleaned)


class StripInvokeBlocksTest(unittest.TestCase):
    """_strip_invoke_blocks：剥离 <invoke> 块"""

    def test_strip_str(self):
        content = (
            '好的。\n'
            '<invoke name="Bash"><parameter name="command">echo hi</parameter></invoke>\n'
            '继续。'
        )
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("<invoke", cleaned)
        self.assertIn("好的。", cleaned)
        self.assertIn("继续。", cleaned)

    def test_strip_multiline_heredoc(self):
        content = (
            '写文件。\n'
            '<invoke name="Bash">\n'
            '<parameter name="command">cat > /tmp/a.txt <<EOF\nhello\nEOF</parameter>\n'
            '</invoke>\n完成。'
        )
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("<invoke", cleaned)
        self.assertIn("写文件。", cleaned)
        self.assertIn("完成。", cleaned)

    def test_strip_list_content(self):
        content = [
            {"type": "text", "text": '见下。\n<invoke name="Bash"><parameter name="command">ls</parameter></invoke>'},
            {"type": "image_url", "url": "http://x"},
        ]
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("<invoke", cleaned[0]["text"])
        self.assertEqual(cleaned[1], {"type": "image_url", "url": "http://x"})

    def test_non_text_passthrough(self):
        self.assertEqual(Agent._strip_invoke_blocks(123), 123)
        self.assertEqual(Agent._strip_invoke_blocks(None), None)

    def test_strip_unclosed_invoke(self):
        """不闭合的 <invoke> 块也能被剥离，防止历史污染。"""
        content = (
            '确认文件状态。\n'
            '<invoke name="Read">\n'
            '<parameter name="file_path">/home/sxkiss/bt/x</parameter>\n'
            # 没有 </invoke>
        )
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("<invoke", cleaned)
        self.assertIn("确认文件状态。", cleaned)

    def test_strip_unclosed_invoke_with_trailing_text(self):
        """不闭合的 <invoke> 块（参数 0/0 的畸形格式）后跟另一个块 + 尾部文本，
        剥离时只剥块、保留块外正文（贴近真实历史数据）。"""
        content = (
            '让我用 Read 工具直接确认文件状态。\n'
            '<invoke name="Read">\n'
            '<｜U2｜parameter name="file_path" string="true">/home/sxkiss/bt/static/platform_panel.html</｜U2｜parameter>\n'
            '<｜U2｜parameter name="limit" string="false">5</｜U2｜parameter>\n'
            # 未闭合（真实数据中漏掉 </invoke>）
            '收尾文本'
        )
        cleaned = Agent._strip_invoke_blocks(content)
        self.assertNotIn("<invoke", cleaned)
        self.assertIn("让我用 Read 工具直接确认文件状态。", cleaned)


class SanitizeHistoryTest(unittest.TestCase):
    """_sanitize_messages_for_api：清洗 assistant 历史里的 <invoke>"""

    def test_assistant_content_stripped(self):
        msgs = [
            {"role": "user", "content": "写文件"},
            {"role": "assistant", "content": '好的。\n<invoke name="Bash"><parameter name="command">echo hi</parameter></invoke>\n继续。'},
            {"role": "tool", "tool_call_id": "x", "content": [{"type": "text", "text": "ok"}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        assistant = [m for m in out if m.get("role") == "assistant"][0]
        self.assertNotIn("<invoke", assistant["content"])
        self.assertIn("好的。", assistant["content"])
        self.assertIn("继续。", assistant["content"])

    def test_tool_result_xml_untouched(self):
        tool_text = "\n<tool>\n<tool_name>Bash</tool_name>\n<toolcall_status>done</toolcall_status>\n</tool>\n"
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "Bash", "arguments": '{"command":"echo hi"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": [{"type": "text", "text": tool_text}]},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        tool = [m for m in out if m.get("role") == "tool"][0]
        self.assertIn("<tool_name>", tool["content"][0]["text"])

    def test_unclosed_invoke_stripped_from_history(self):
        """_sanitize_messages_for_api 对不闭合 <invoke> 的 assistant 历史也做剥离。"""
        msgs = [
            {"role": "user", "content": "确认文件"},
            {"role": "assistant", "content": '查文件。\n<invoke name="Read">\n<parameter name="file_path">/tmp/x</parameter>'},
            {"role": "user", "content": "继续"},
        ]
        out = Agent._sanitize_messages_for_api(msgs)
        assistant = [m for m in out if m.get("role") == "assistant"][0]
        self.assertNotIn("<invoke", assistant["content"])
        self.assertIn("查文件。", assistant["content"])


if __name__ == "__main__":
    unittest.main()