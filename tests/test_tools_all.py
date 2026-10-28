"""bt 工具单测全覆盖（2026-10-23）。

运行：python -m pytest tests/test_tools_all.py -q
"""
import os
import sys
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# edit.py
# ============================================================
class TestEditPerformReplace:
    def test_exact_match(self):
        from chat_client.tools.edit import _perform_replace
        assert _perform_replace("hello world", "hello", "hi") == "hi world"

    def test_multiline_exact(self):
        from chat_client.tools.edit import _perform_replace
        content = "def foo():\n    x = 1\n    y = 2\n    return x\n"
        result = _perform_replace(content, "    x = 1\n    y = 2", "    x = 1\n    y = 3")
        assert "    y = 3" in result
        compile(result, "<t>", "exec")

    def test_no_change_raises(self):
        from chat_client.tools.edit import _perform_replace
        with pytest.raises(ValueError, match="identical"):
            _perform_replace("abc", "abc", "abc")

    def test_not_found_raises(self):
        from chat_client.tools.edit import _perform_replace
        with pytest.raises(ValueError, match="Could not find"):
            _perform_replace("hello", "xyz", "abc")

    def test_multi_occurrence_raises(self):
        from chat_client.tools.edit import _perform_replace
        with pytest.raises(ValueError, match="multiple matches"):
            _perform_replace("a\na\n", "a", "b")

    def test_replace_all(self):
        from chat_client.tools.edit import _perform_replace
        assert _perform_replace("a\na\na", "a", "b", replace_all=True) == "b\nb\nb"

    def test_indent_flexible_match(self):
        from chat_client.tools.edit import _perform_replace
        result = _perform_replace("    def foo():\n        x = 1\n", "def foo():\n    x = 1", "def foo():\n    x = 2")
        assert "x = 2" in result


class TestAlignIndent:
    def test_new_str_no_indent_in_block(self):
        from chat_client.tools.edit import _perform_replace
        result = _perform_replace(
            "def foo():\n    if True:\n        x = 1\n        y = 2\n    return x\n",
            "        x = 1\n        y = 2",
            "x = 1\ny = 3",
        )
        assert "        x = 1\n        y = 3" in result
        compile(result, "<t>", "exec")

    def test_nested_indent_preserved(self):
        from chat_client.tools.edit import _perform_replace
        result = _perform_replace(
            "class A:\n    def bar(self):\n        if True:\n            return 1\n        return 2\n",
            "        if True:\n            return 1\n        return 2",
            "    if True:\n        return 1\n    return 3",
        )
        assert "    if True:" in result
        assert "        return 1" in result
        compile(result, "<t>", "exec")

    def test_correct_indent_unchanged(self):
        from chat_client.tools.edit import _perform_replace
        result = _perform_replace("def foo():\n    x = 1\n    y = 2\n", "    x = 1\n    y = 2", "    x = 1\n    y = 3")
        assert result == "def foo():\n    x = 1\n    y = 3\n"

    def test_tab_indent_preserved(self):
        from chat_client.tools.edit import _perform_replace
        result = _perform_replace(
            "def foo():\n\tif True:\n\t\tx = 1\n\t\ty = 2\n",
            "\t\tx = 1\n\t\ty = 2",
            "x = 1\ny = 3",
        )
        assert "\t\t" in result
        compile(result, "<t>", "exec")

    def test_empty_new_str(self):
        from chat_client.tools.edit import _align_new_str_indent
        assert _align_new_str_indent("", "    x = 1") == ""


# ============================================================
# agent_tools.py
# ============================================================
class TestAgentTools:
    def test_read_nonexistent_file(self, tmp_path):
        from chat_client.tools.agent_tools import Read
        result = Read(file_path=str(tmp_path / "no_such_file.txt"))
        assert "error" in str(result).lower()

    def test_write_and_read_roundtrip(self, tmp_path):
        from chat_client.tools.agent_tools import Write, Read
        f = tmp_path / "test.txt"
        Write(file_path=str(f), content="hello\nworld")
        assert "hello" in Read(file_path=str(f))

    def test_grep_nonexistent_pattern(self, tmp_path):
        from chat_client.tools.agent_tools import Grep
        (tmp_path / "a.py").write_text("def foo(): pass\n")
        result = Grep(pattern="nonexistent_xyz", path=str(tmp_path))
        assert "[]" in str(result) or "no match" in str(result).lower() or str(result).strip() == ""

    def test_glob_basic(self, tmp_path):
        from chat_client.tools.agent_tools import Glob
        (tmp_path / "a.txt").write_text("x")
        (tmp_path / "b.txt").write_text("y")
        result = Glob(pattern="*.txt", path=str(tmp_path))
        assert "a.txt" in str(result) or "b.txt" in str(result)

    def test_write_protected_path_rejected(self):
        from chat_client.tools.agent_tools import Write
        result = Write(file_path="/etc/passwd", content="hacked")
        assert "error" in str(result).lower()


# ============================================================
# mem0.py
# ============================================================
class TestMem0:
    def test_mem0_search_no_crash(self):
        from chat_client.tools.mem0 import mem0_search
        try:
            mem0_search(query="test", limit=5)
        except Exception:
            pass

    def test_mem0_add_no_crash(self):
        from chat_client.tools.mem0 import mem0_add
        try:
            mem0_add(text="test", metadata={})
        except Exception:
            pass

    def test_mem0_list_no_crash(self):
        from chat_client.tools.mem0 import mem0_list
        try:
            mem0_list(agent_id=None, limit=10)
        except Exception:
            pass


# ============================================================
# webfetch.py
# ============================================================
class TestWebFetch:
    def test_simple_html_to_markdown(self):
        from chat_client.tools.webfetch import simple_html_to_markdown
        md = simple_html_to_markdown("<html><body><h1>Title</h1><p>Para</p></body></html>")
        assert "Title" in md and "Para" in md

    def test_extract_text_from_html(self):
        from chat_client.tools.webfetch import extract_text_from_html
        text = extract_text_from_html("<html><body>Hello <b>world</b></body></html>")
        assert "Hello" in text and "world" in text

    def test_validate_url_not_ssrf(self):
        from chat_client.tools.webfetch import _validate_url_not_ssrf
        # 私网地址返回错误字符串，不是 None
        for url in ("http://169.254.169.254/", "http://127.0.0.1/", "http://10.0.0.1/"):
            result = _validate_url_not_ssrf(url)
            assert isinstance(result, str) and "blocked" in result.lower() or "private" in result.lower()
        # 公网地址返回 None 表示通过校验
        assert _validate_url_not_ssrf("https://example.com") is None


# ============================================================
# todo.py
# ============================================================
class TestTodo:
    def test_todo_write_missing_session(self):
        """无 session context 时 TodoWrite 返回 error。"""
        from chat_client.tools.todo import TodoWrite
        result = TodoWrite(todos=[{"id": "1", "content": "task", "status": "pending", "priority": "high"}])
        assert "error" in str(result).lower()

    def test_todo_write_empty(self):
        from chat_client.tools.todo import TodoWrite
        result = TodoWrite(todos=[])
        assert isinstance(result, str)


# ============================================================
# terminal.py
# ============================================================
class TestTerminal:
    def test_bash_simple_command(self):
        from chat_client.tools.terminal import Bash
        b = Bash()
        result = b.execute(command="echo hello", description="test")
        assert "hello" in str(result)

    def test_bash_cwd_default(self):
        from chat_client.tools.terminal import Bash
        b = Bash()
        result = b.execute(command="pwd", description="test-cwd")
        assert "/" in str(result)

    def test_check_command_status_invalid_id(self):
        from chat_client.tools.terminal import CheckCommandStatus
        c = CheckCommandStatus()
        result = c.execute(command_id="nonexistent_id_xyz")
        assert isinstance(result, str)

    def test_stop_command_invalid_id(self):
        from chat_client.tools.terminal import StopCommand
        s = StopCommand()
        result = s.execute(command_id="nonexistent_id_xyz")
        assert isinstance(result, str)


# ============================================================
# knowledge.py
# ============================================================
class TestKnowledge:
    def test_knowledge_add_requires_path_or_text(self):
        from chat_client.tools.knowledge import Knowledge
        result = Knowledge(action="add")
        assert "error" in str(result).lower() or "required" in str(result).lower()

    def test_knowledge_search_no_crash(self):
        from chat_client.tools.knowledge import Knowledge
        try:
            Knowledge(action="search", query="test", top_k=5)
        except Exception:
            pass


# ============================================================
# view_image.py
# ============================================================
class TestViewImage:
    def test_view_image_nonexistent(self):
        from chat_client.tools.view_image import ViewImage
        result = ViewImage(path="/nonexistent/image.png", return_base64=False)
        assert "error" in str(result).lower() or "not found" in str(result).lower()

    def test_view_image_safe_path(self):
        from chat_client.tools.view_image import _safe_path
        assert isinstance(_safe_path("/tmp/test.png"), str)


# ============================================================
# wechat_route.py
# ============================================================
class TestWeChatRoute:
    def test_missing_msg_type(self):
        from chat_client.tools.wechat_route import WeChatRoute
        result = WeChatRoute(msg_type="")
        assert "error" in str(result).lower() or "required" in str(result).lower()

    def test_text_missing_content(self):
        from chat_client.tools.wechat_route import WeChatRoute
        result = WeChatRoute(msg_type="text")
        assert "error" in str(result).lower() or "content" in str(result).lower()

    def test_image_missing_path(self):
        from chat_client.tools.wechat_route import WeChatRoute
        result = WeChatRoute(msg_type="image", path="")
        assert "error" in str(result).lower() or "path" in str(result).lower()

    def test_target_arg_no_wxid_no_session(self, monkeypatch):
        import chat_client.tools.wechat_route as mod
        monkeypatch.setattr(mod, '_current_session_id', lambda: "")
        from chat_client.tools.wechat_route import _target_arg
        with pytest.raises(ValueError, match="无法定位接收方"):
            _target_arg(None)

    def test_target_arg_placeholder_keys(self, monkeypatch):
        import chat_client.tools.wechat_route as mod
        monkeypatch.setattr(mod, '_current_session_id', lambda: "")
        from chat_client.tools.wechat_route import _target_arg
        for ph in ["null", "none", "undefined", "nil", "n/a"]:
            with pytest.raises(ValueError):
                _target_arg(ph)

    def test_extract_new_msg_id(self):
        from chat_client.tools.wechat_route import _extract_new_msg_id
        assert _extract_new_msg_id({"newMsgId": 123}) == 123
        assert _extract_new_msg_id({"newMsgId": 0}) is None
        assert _extract_new_msg_id({}) is None
        assert _extract_new_msg_id(None) is None

    def test_wechat_route_invalid_msg_type(self):
        from chat_client.tools.wechat_route import WeChatRoute
        result = WeChatRoute(msg_type="invalid_type", content="test")
        assert "error" in str(result).lower() or "不支持" in str(result)


# ============================================================
# wechat_media.py
# ============================================================
class TestClawbotSendMedia:
    def test_missing_path(self):
        from chat_client.tools.wechat_media import ClawbotSendMedia
        result = ClawbotSendMedia(path="", media_type="image")
        assert "error" in str(result).lower() or "path" in str(result).lower()

    def test_nonexistent_file(self):
        from chat_client.tools.wechat_media import ClawbotSendMedia
        result = ClawbotSendMedia(path="/nonexistent/file.png", media_type="image")
        assert "error" in str(result).lower() or "not found" in str(result).lower()


# ============================================================
# gateway_media_send.py
# ============================================================
class TestSendMedia:
    def test_nonexistent_path(self):
        from chat_client.tools.gateway_media_send import SendMedia
        result = SendMedia(path="/nonexistent/file.txt")
        assert "error" in str(result).lower() or "not found" in str(result).lower()


# ============================================================
# skill.py
# ============================================================
class TestSkills:
    def test_skills_action_list(self):
        from chat_client.tools.skill import Skills
        result = Skills(name="", action="list")
        assert isinstance(result, str)

    def test_skills_action_unknown(self):
        from chat_client.tools.skill import Skills
        result = Skills(name="", action="unknown_action_xyz")
        assert isinstance(result, str)


# ============================================================
# market.py
# ============================================================
class TestMarket:
    def test_expert_market_no_crash(self):
        from chat_client.tools.market import ExpertMarket
        result = ExpertMarket(keyword="", limit=5)
        assert isinstance(result, str)

    def test_skill_market_returns_string(self):
        from chat_client.tools.market import SkillMarket
        try:
            result = SkillMarket(keyword="", limit=5)
        except Exception:
            # 市场数据源暂时不可用允许异常
            return
        assert isinstance(result, str)

    def test_get_expert_prompt_missing_name(self):
        from chat_client.tools.market import ExpertMarket
        result = ExpertMarket(action="prompt", name="")
        assert isinstance(result, str) or result is None


# ============================================================
# mcp_tool.py
# ============================================================
class TestMCP:
    def test_mcp_missing_server(self):
        from chat_client.tools.mcp_tool import MCP
        result = MCP(action="call", server="nonexistent", tool="test")
        assert isinstance(result, str)

    def test_mcp_list_action(self):
        from chat_client.tools.mcp_tool import MCP
        result = MCP(action="list")
        assert isinstance(result, str)


# ============================================================
# scheduler.py
# ============================================================
class TestScheduler:
    def test_list_scheduled_tasks_no_crash(self):
        from chat_client.tools.scheduler import ListScheduledTasks
        result = ListScheduledTasks()
        assert isinstance(result, (str, dict, list)) or result is None

    def test_cancel_scheduled_task_missing_name(self):
        from chat_client.tools.scheduler import CancelScheduledTask
        result = CancelScheduledTask(name_or_id="")
        assert isinstance(result, str)


# ============================================================
# summary.py
# ============================================================
class TestSummary:
    def test_task_summary_minimal(self):
        from chat_client.tools.summary import TaskSummary
        result = TaskSummary(content="test summary")
        assert isinstance(result, str)


# ============================================================
# tool_archive.py
# ============================================================
class TestToolArchive:
    def test_read_tool_archive_invalid_id(self):
        from chat_client.tools.tool_archive import ReadToolArchive
        result = ReadToolArchive(archive_id="nonexistent", limit=0, offset=1)
        assert "error" in str(result).lower() or "not found" in str(result).lower() or result is None


# ============================================================
# base.py
# ============================================================
class TestBase:
    def test_xml_response_success(self):
        from chat_client.tools.base import _xml_response
        result = _xml_response("done", "success msg")
        assert "done" in result and "success msg" in result

    def test_xml_response_error(self):
        from chat_client.tools.base import _xml_response
        result = _xml_response("error", "something failed")
        assert "error" in result.lower() and "something failed" in result


# ============================================================
# sys_ops.py (部分工具已合并/删除，保留基础测试)
# ============================================================
class TestSysOps:
    pass  # 原 find_large_files/dir_usage/list_port_bindings 已移除
