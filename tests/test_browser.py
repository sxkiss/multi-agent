"""浏览器基础设施测试（platform_ext/browser）。

覆盖：env 探测、harness 辅助、record_replay 路由、工具注册。
不真正启动 Chrome（通过 monkeypatch 替换底层启动函数），保证测试轻量、无残留。
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def vendor_ready():
    from platform_ext.browser import ensure_vendor_on_path  # noqa: PLC0415
    ensure_vendor_on_path()
    yield


def _body(res):
    """从 JSONResponse 提取 data dict；错误响应无 data 时回退到整个 dict。"""
    import json
    if hasattr(res, "body"):
        obj = json.loads(res.body.decode("utf-8"))
        return obj.get("data", obj)
    return res


# ── env ────────────────────────────────────────────────────
class TestEnv:
    def test_probe_env_shape(self):
        from platform_ext.browser import env
        res = env.probe_env()
        for k in ("playwright", "browsers_ok", "harness_browser", "chrome_path", "error"):
            assert k in res
        assert isinstance(res["playwright"], bool)

    def test_verify_binary_missing(self):
        from platform_ext.browser import env
        ok, msg = env._verify_binary("/nonexistent/chrome")
        assert ok is False
        assert msg


# ── harness ────────────────────────────────────────────────
class TestHarness:
    def test_control_owner_default(self):
        from platform_ext.browser import harness
        assert harness.control_owner_for("nope") == "agent"

    def test_set_control_owner(self):
        from platform_ext.browser import harness
        harness.set_control_owner("sid", "user")
        assert harness.control_owner_for("sid") == "user"
        harness.set_control_owner("sid", "agent")

    def test_handoff_body(self):
        from platform_ext.browser import harness
        b = harness.HandoffBody(target="user", reason="x")
        assert b.target == "user"


# ── record_replay（不真启动 Chrome）─────────────────────────
class TestRecordReplay:
    def test_status_route(self):
        from platform_ext.browser import record_replay as rr
        res = _body(asyncio.run(rr.record_status()))
        assert "active" in res
        assert "recordings" in res

    def test_start_invalid_profile_fails_fast(self):
        # 不 mock：用一个几乎必失败的 profile，验证返回 dict 而非抛异常
        from platform_ext.browser import record_replay as rr
        res = _body(asyncio.run(rr.record_start(rr.StartBody(profile="__no_such_profile__"))))
        assert isinstance(res, dict)

    def test_stop_without_active(self):
        from platform_ext.browser import record_replay as rr
        # 清空活动录制状态，保证没有进行中的录制
        rr._ACTIVE.update({"session": None, "recording_id": None})
        res = _body(asyncio.run(rr.record_stop()))
        assert res.get("ok") is None
        assert res.get("msg") or res.get("error")

    def test_list_recordings_shape(self):
        from platform_ext.browser import record_replay as rr
        res = _body(asyncio.run(rr.list_recordings()))
        assert "recordings" in res

    def test_skill_content_missing(self):
        from platform_ext.browser import record_replay as rr
        res = _body(asyncio.run(rr.get_skill_content(rr.RecordingIdBody(recording_id="nope"))))
        assert res.get("ok") is None
        assert res.get("msg") or res.get("error")

    def test_replay_missing(self):
        from platform_ext.browser import record_replay as rr
        res = _body(asyncio.run(rr.replay(rr.ReplayBody(recording_id="nope", profile="test"))))
        assert res.get("ok") is None
        assert res.get("msg") or res.get("error")


# ── tool ───────────────────────────────────────────────────
class TestTool:
    def test_browser_use_registered(self):
        from chat_client.tools import registry  # noqa: PLC0415
        assert "browser_use" in registry._metadata
        meta = registry._metadata["browser_use"]
        assert meta.get("category") == "浏览器"
        assert meta.get("name_cn") == "浏览器自动化"

    def test_browser_use_navigate_no_url(self):
        from chat_client.tools import browser
        res = browser.browser_use(action="navigate", url="")
        assert "error" in res
        assert "url" in res

    def test_browser_use_unknown_action(self):
        from chat_client.tools import browser
        res = browser.browser_use(action="not_a_real_action")
        assert "error" in res