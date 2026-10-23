"""Edit 工具缩进自动对齐的回归测试。"""
import pytest
from chat_client.tools.edit import _perform_replace, _align_new_str_indent


class TestAlignIndent:
    def test_new_str_no_indent_in_block(self):
        """new_str 无缩进但插入缩进块时自动对齐到 old_str 基准。"""
        result = _perform_replace(
            "def foo():\n    if True:\n        x = 1\n        y = 2\n    return x\n",
            "        x = 1\n        y = 2",
            "x = 1\ny = 3",
        )
        assert "        x = 1\n        y = 3" in result
        compile(result, "<t>", "exec")

    def test_nested_indent_preserved(self):
        """嵌套结构：new_str 比 old_str 少一级，相对层级保留。"""
        result = _perform_replace(
            "class A:\n    def bar(self):\n        if True:\n            return 1\n        return 2\n",
            "        if True:\n            return 1\n        return 2",
            "    if True:\n        return 1\n    return 3",
        )
        assert "    if True:" in result
        assert "        return 1" in result
        compile(result, "<t>", "exec")

    def test_correct_indent_unchanged(self):
        """new_str 缩进已经正确时不被改动。"""
        before = "def foo():\n    x = 1\n    y = 2\n"
        result = _perform_replace(before, "    x = 1\n    y = 2", "    x = 1\n    y = 3")
        assert result == "def foo():\n    x = 1\n    y = 3\n"

    def test_tab_indent_preserved(self):
        """tab 缩进文件：对齐后保持 tab。"""
        result = _perform_replace(
            "def foo():\n\tif True:\n\t\tx = 1\n\t\ty = 2\n",
            "\t\tx = 1\n\t\ty = 2",
            "x = 1\ny = 3",
        )
        assert "\t\t" in result
        compile(result, "<t>", "exec")
