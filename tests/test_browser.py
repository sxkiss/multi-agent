"""浏览器基础设施测试（platform_ext/browser + chat_client/tools/browser）。

覆盖：env 探测、harness 辅助、record_replay 路由、工具注册与截图 Markdown。
不真正启动 Chrome（用 monkeypatch 替换底层），只验证逻辑与 API 契约。
"""

from __future__ import annotations

import asyncio
import os
import sys

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


# ── record_replay ──────────────────────────────────────────
class TestRecordReplay:
    def test_status_route(self):
        from platform_ext.browser import record_replay as rr
        res = _body(asyncio.run(rr.record_status()))
        assert "active" in res
        assert "recordings" in res

    def test_stop_without_active(self):
        from platform_ext.browser import record_replay as rr
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

    def test_screenshots_dir_path(self):
        from chat_client.tools.browser import _screenshots_dir
        d = _screenshots_dir()
        assert d.endswith(os.path.join("workspace", "outbound", "screenshots"))

    def test_screenshot_action_returns_markdown_image(self, monkeypatch):
        """mock 底层 browser_tool 返回截图路径，验证工具返回 Markdown 图片。"""
        import platform_ext.browser as _peb
        _peb.ensure_vendor_on_path()
        from octop_browser.models import ActionMetrics, ToolResult
        from octop_browser import tool_interface
        from chat_client.tools import browser

        def fake_tool(action, profile, **kwargs):
            return ToolResult(
                success=True,
                content="/home/sxkiss/bt/workspace/outbound/screenshots/fake.png",
                error=None,
                metrics=ActionMetrics(action="screenshot", duration_ms=1,
                                      dom_nodes_scanned=0, estimated_tokens=0),
            )

        monkeypatch.setattr(tool_interface, "browser_tool", fake_tool)
        # 让 _run 直接返回传入值（ToolResult），不进入事件循环
        monkeypatch.setattr(browser, "_run", lambda coro, timeout=120: coro)
        res = browser.browser_use(action="screenshot", profile="p1")
        assert "![screenshot]" in res
        assert "fake.png" in res


# ── proxy 配置 ───────────────────────────────────────────
class TestProxyConfig:
    def test_load_proxy_config_default(self, monkeypatch, tmp_path):
        """无配置文件时返回禁用状态。"""
        import platform_ext.browser.proxy_config as pc
        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        cfg = pc.load_proxy_config()
        assert cfg == {"enabled": False, "proxy": ""}

    def test_save_and_load_proxy_config(self, monkeypatch, tmp_path):
        import platform_ext.browser.proxy_config as pc
        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        pc.save_proxy_config(True, "http://127.0.0.1:7890")
        cfg = pc.load_proxy_config()
        assert cfg == {"enabled": True, "proxy": "http://127.0.0.1:7890"}

    def test_effective_proxy_prefers_persisted(self, monkeypatch, tmp_path):
        """持久化配置优先于环境变量。"""
        import platform_ext.browser.proxy_config as pc
        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        monkeypatch.setenv("BROWSER_USE_PROXY", "socks5://127.0.0.1:1080")
        # 无持久化配置时回退环境变量
        assert pc.effective_proxy() == "socks5://127.0.0.1:1080"
        # 有持久化配置时优先
        pc.save_proxy_config(True, "http://127.0.0.1:7890")
        assert pc.effective_proxy() == "http://127.0.0.1:7890"

    def test_effective_proxy_disabled_falls_back_to_env(self, monkeypatch, tmp_path):
        import platform_ext.browser.proxy_config as pc
        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        monkeypatch.setenv("BROWSER_USE_PROXY", "http://127.0.0.1:7890")
        # 持久化配置存在但 disabled → 回退环境变量
        pc.save_proxy_config(False, "http://127.0.0.1:9999")
        assert pc.effective_proxy() == "http://127.0.0.1:7890"

    def test_proxy_routes_registered(self):
        from platform_ext.browser import api
        paths = []
        for r in api.subrouters:
            prefix = getattr(r, "prefix", "")
            for route in getattr(r, "routes", []):
                paths.append(prefix + route.path)
        assert "/api/v2/browser/proxy" in paths

    def test_get_proxy_endpoint(self, monkeypatch, tmp_path):
        from platform_ext.browser import proxy_config as pc
        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        res = asyncio.run(pc.get_proxy())
        assert res["status"] is True
        assert res["data"]["effective_proxy"] is None
        assert res["data"]["configured"] == {"enabled": False, "proxy": ""}

    def test_set_proxy_endpoint(self, monkeypatch, tmp_path):
        from platform_ext.browser import proxy_config as pc
        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        res = asyncio.run(pc.set_proxy(pc.ProxyBody(enabled=True, proxy="http://127.0.0.1:7890")))
        assert res["status"] is True
        assert res["data"]["configured"] == {"enabled": True, "proxy": "http://127.0.0.1:7890"}
        assert res["data"]["effective_proxy"] == "http://127.0.0.1:7890"

    def test_vendor_launcher_effective_proxy(self, monkeypatch, tmp_path):
        """验证 vendored launcher 的 _effective_proxy 读取持久化配置。"""
        import platform_ext.browser as _peb
        _peb.ensure_vendor_on_path()
        from octop_browser.cdp import launcher

        # 写一份代理配置到临时 home
        (tmp_path / ".octop-browser").mkdir(parents=True, exist_ok=True)
        import json
        (tmp_path / ".octop-browser" / "proxy.json").write_text(
            json.dumps({"enabled": True, "proxy": "http://127.0.0.1:7890"}), encoding="utf-8")

        monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
        monkeypatch.delenv("BROWSER_USE_PROXY", raising=False)
        assert launcher._effective_proxy() == "http://127.0.0.1:7890"

    def test_browser_use_set_proxy(self, monkeypatch, tmp_path):
        """browser_use 工具可直接调用 action=set_proxy 设置代理。"""
        from platform_ext.browser import proxy_config as pc
        import platform_ext.browser as _peb
        _peb.ensure_vendor_on_path()
        from chat_client.tools import browser

        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        res = browser.browser_use(action="set_proxy", proxy="http://127.0.0.1:7890")
        assert "成功" in res or "代理配置已保存" in res
        assert pc.load_proxy_config() == {"enabled": True, "proxy": "http://127.0.0.1:7890"}

    def test_browser_use_set_proxy_disabled(self, monkeypatch, tmp_path):
        """proxy_enabled=False 时清除代理配置。"""
        from platform_ext.browser import proxy_config as pc
        import platform_ext.browser as _peb
        _peb.ensure_vendor_on_path()
        from chat_client.tools import browser

        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        pc.save_proxy_config(True, "http://127.0.0.1:7890")
        res = browser.browser_use(action="set_proxy", proxy_enabled=False, proxy="")
        assert "代理配置已保存" in res
        assert pc.load_proxy_config() == {"enabled": False, "proxy": ""}

    def test_browser_use_get_proxy(self, monkeypatch, tmp_path):
        """browser_use 工具可直接调用 action=get_proxy 读取代理。"""
        from platform_ext.browser import proxy_config as pc
        import platform_ext.browser as _peb
        _peb.ensure_vendor_on_path()
        from chat_client.tools import browser

        monkeypatch.setattr(pc, "_proxy_config_path", lambda: tmp_path / "proxy.json")
        pc.save_proxy_config(True, "socks5://127.0.0.1:1080")
        res = browser.browser_use(action="get_proxy")
        assert '"effective_proxy": "socks5://127.0.0.1:1080"' in res
        assert '"enabled": true' in res or '"enabled": True' in res