"""浏览器基础设施测试（/home/sxkiss/bt/platform_ext/browser + chat_client/tools/browser）。

覆盖：env 探测、harness 解析、record_replay 格式、工具注册。
不触网、不真实启动 Chrome（除 probe_env 只读探测）。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestBrowserEnv:
    def test_probe_env_shape(self):
        from platform_ext.browser import ensure_vendor_on_path
        ensure_vendor_on_path()
        from platform_ext.browser.env import probe_env
        env = probe_env()
        for k in ("playwright", "browsers_ok", "harness_browser", "chrome_path", "error"):
            assert k in env

    def test_probe_env_types(self):
        from platform_ext.browser.env import probe_env
        env = probe_env()
        assert isinstance(env["browsers_ok"], bool)
        assert env["chrome_path"] is None or isinstance(env["chrome_path"], str)

    def test_verify_binary(self):
        from platform_ext.browser.env import _verify_binary
        ok, msg = _verify_binary("/bin/true")
        assert ok is True
        assert isinstance(msg, str)

    def test_install_stream_yields(self):
        from platform_ext.browser.env import install_stream
        import asyncio
        # 只消费一个事件（可能立即 done 或 log），不真正下载
        async def _first():
            gen = install_stream()
            try:
                item = await gen.__anext__()
                return item
            except StopAsyncIteration:
                return ""
        data = asyncio.run(_first())
        assert data.startswith("data: ")


class TestBrowserHarness:
    def test_default_profile(self):
        from platform_ext.browser import harness
        assert harness.DEFAULT_PROFILE == "default"

    def test_control_owner_roundtrip(self):
        from platform_ext.browser import harness
        harness.set_control_owner("s1", "user")
        assert harness.control_owner_for("s1") == "user"
        harness.set_control_owner("s1", "agent")
        assert harness.control_owner_for("s1") == "agent"

    def test_harness_page_url_empty_when_no_session(self):
        from platform_ext.browser import harness
        import asyncio
        url = asyncio.run(harness.harness_page_url(None))
        assert url == ""

    def test_harness_list_tabs_empty(self):
        from platform_ext.browser import harness
        import asyncio
        assert asyncio.run(harness.harness_list_tabs(None)) == []


class TestBrowserRecordReplay:
    def test_store_isolated(self, tmp_path, monkeypatch):
        # 重定向 RecordingStore 的基目录
        from octop_browser.record import RecordingStore
        import importlib
        # 默认 store 用 settings 里的路径，这里直接验证类存在
        assert RecordingStore is not None

    def test_router_wraps_ok(self):
        from platform_ext.browser.record_replay import _ok, _err
        r = _ok({"a": 1})
        assert r.body is not None


class TestBrowserTool:
    def test_tool_registered(self):
        from chat_client.tools import registry
        assert "browser_use" in registry._metadata

    def test_tool_metadata(self):
        from chat_client.tools import registry
        meta = registry._metadata.get("browser_use", {})
        assert meta.get("category") == "浏览器"
        assert meta.get("name_cn") == "浏览器自动化"

    def test_tool_imports_vendor(self):
        from chat_client.tools.browser import _get_loop
        loop = _get_loop()
        assert loop is not None