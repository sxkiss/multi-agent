"""MCP 工具 call action 测试：直调已配置 MCP server 的工具。"""
import json
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_client.tools.mcp_tool import MCP


def test_call_requires_server():
    r = MCP(action="call", server="", tool="search_news")
    assert "error" in r
    assert "server" in r


def test_call_requires_tool():
    r = MCP(action="call", server="chub__x", tool="")
    assert "error" in r
    assert "tool" in r


def test_call_invalid_arguments():
    r = MCP(action="call", server="chub__x", tool="t", arguments="not json")
    assert "error" in r
    assert "解析失败" in r


def test_call_success():
    mc = MagicMock()
    mc.is_loaded.return_value = True
    mc.call_tool.return_value = '{"news": [{"title": "hello"}]}'
    with patch("chat_client.mcp_client.get_mcp_client", return_value=mc):
        r = MCP(action="call", server="chub__tencent-news__x", tool="search_news",
                arguments=json.dumps({"query": "AI"}))
    assert "done" in r
    assert "hello" in r
    # 参数被正确传递
    _, kwargs = mc.call_tool.call_args
    assert kwargs.get("server_name") == "chub__tencent-news__x"


def test_call_client_not_loaded():
    mc = MagicMock()
    mc.is_loaded.return_value = False
    with patch("chat_client.mcp_client.get_mcp_client", return_value=mc):
        r = MCP(action="call", server="chub__x", tool="t", arguments="{}")
    assert "error" in r
    assert "未加载" in r


def test_call_exception():
    mc = MagicMock()
    mc.is_loaded.return_value = True
    mc.call_tool.side_effect = RuntimeError("boom")
    with patch("chat_client.mcp_client.get_mcp_client", return_value=mc):
        r = MCP(action="call", server="chub__x", tool="t", arguments="{}")
    assert "error" in r
    assert "boom" in r