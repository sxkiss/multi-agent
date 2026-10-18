#!/usr/bin/env python
"""工具结果面向模型的格式归一化测试。

存在意义：工具结果内部用 <tool>...</tool> XML 封装，但模型走原生
function calling。原样把 XML 回灌会让部分模型误以为工具协议是 XML，
在 reasoning 里输出 <invoke>/<parameter> 等伪调用标签并退化循环。
tool_result_for_model 只在发送给模型时去掉外壳，归档/展示保持原样。

运行：
    .venv/bin/python -m pytest tests/test_tool_result_for_model.py -q
"""
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client.agent import Agent  # noqa: E402
from chat_client.tools.base import tool_result_for_model  # noqa: E402


class ToolResultForModelTest(unittest.TestCase):
    def test_plain_text_unchanged(self):
        self.assertEqual(tool_result_for_model("hello"), "hello")

    def test_done_xml_is_flattened(self):
        xml = (
            "\n<tool>\n<tool_name>Read</tool_name>\n"
            "<toolcall_status>done</toolcall_status>\n"
            "<toolcall_result>\nline1\nline2\n</toolcall_result>\n</tool>\n"
        )
        out = tool_result_for_model(xml)
        self.assertIn("tool result: Read (done)", out)
        self.assertIn("line1\nline2", out)
        for tag in ("<tool>", "<tool_name>", "<toolcall_status>", "<toolcall_result>"):
            self.assertNotIn(tag, out)

    def test_error_status_kept(self):
        xml = (
            "<tool>\n<tool_name>Bash</tool_name>\n"
            "<toolcall_status>error</toolcall_status>\n"
            "<toolcall_result>\nboom\n</toolcall_result>\n</tool>"
        )
        out = tool_result_for_model(xml)
        self.assertIn("tool result: Bash (error)", out)
        self.assertIn("boom", out)

    def test_missing_tool_name(self):
        xml = (
            "<tool>\n<toolcall_status>done</toolcall_status>\n"
            "<toolcall_result>\nok\n</toolcall_result>\n</tool>"
        )
        out = tool_result_for_model(xml)
        self.assertIn("tool result: unknown (done)", out)

    def test_archive_marker_preserved(self):
        xml = (
            "<tool>\n<tool_name>Bash</tool_name>\n"
            "<toolcall_status>done</toolcall_status>\n"
            "<toolcall_result>\nhead\n<tool_archive id=\"abc\" />\ntail\n</toolcall_result>\n</tool>"
        )
        out = tool_result_for_model(xml)
        self.assertIn('<tool_archive id="abc" />', out)
        self.assertNotIn("<toolcall_status>", out)

    def test_agent_normalizes_text_block_list(self):
        content = [{
            "type": "text",
            "text": (
                "<tool>\n<tool_name>Read</tool_name>\n"
                "<toolcall_status>done</toolcall_status>\n"
                "<toolcall_result>\nX\n</toolcall_result>\n</tool>"
            ),
        }]
        out = Agent._tool_content_for_model(content)
        self.assertEqual(out[0]["type"], "text")
        self.assertIn("tool result: Read (done)", out[0]["text"])
        self.assertNotIn("<toolcall_status>", out[0]["text"])

    def test_agent_normalization_is_idempotent(self):
        once = tool_result_for_model(
            "<tool>\n<tool_name>Read</tool_name>\n"
            "<toolcall_status>done</toolcall_status>\n"
            "<toolcall_result>\nX\n</toolcall_result>\n</tool>"
        )
        self.assertEqual(tool_result_for_model(once), once)

    def test_sanitize_normalizes_legacy_tool_message(self):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": "{}"},
                }],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "content": [{
                    "type": "text",
                    "text": (
                        "<tool>\n<tool_name>Read</tool_name>\n"
                        "<toolcall_status>done</toolcall_status>\n"
                        "<toolcall_result>\nX\n</toolcall_result>\n</tool>"
                    ),
                }],
            },
        ]
        out = Agent._sanitize_messages_for_api(messages)
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        text = tool_msgs[0]["content"][0]["text"]
        self.assertIn("tool result: Read (done)", text)
        self.assertNotIn("<toolcall_status>", text)


if __name__ == "__main__":
    unittest.main()
