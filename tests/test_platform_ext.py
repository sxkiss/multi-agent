"""平台扩展层测试（/home/sxkiss/bt/platform_ext）。

覆盖：teams / channels / storage / cli。
不触网、不依赖生产 config（用临时目录重定向）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def isolated_cfg(tmp_path, monkeypatch):
    """把 config.json 重定向到临时目录。"""
    from platform_ext import storage as st
    from platform_ext import teams as tm
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(st, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(tm, "CONFIG_FILE", str(cfg))
    return cfg


# ── teams ──────────────────────────────────────────────────
class TestTeams:
    def test_create_and_list(self, isolated_cfg):
        from platform_ext import teams as tm
        t = tm.create_team("调研组", ["search", "coder"], description="调研+实现")
        assert t["name"] == "调研组"
        assert t["kind"] == "team"
        assert len(t["member_ids"]) == 2
        assert len(tm.list_teams()) == 1

    def test_min_members_enforced(self, isolated_cfg):
        from platform_ext import teams as tm
        with pytest.raises(ValueError):
            tm.create_team("单人", ["search"])

    def test_unknown_member_rejected(self, isolated_cfg):
        from platform_ext import teams as tm
        with pytest.raises(ValueError):
            tm.create_team("组", ["search", "不存在的成员"])

    def test_update_and_delete(self, isolated_cfg):
        from platform_ext import teams as tm
        t = tm.create_team("组", ["search", "coder"])
        tm.update_team(t["team_id"], name="新名", member_ids=["search", "planner"])
        assert tm.get_team(t["team_id"])["name"] == "新名"
        assert tm.delete_team(t["team_id"]) is True
        assert tm.get_team(t["team_id"]) is None

    def test_template(self, isolated_cfg):
        from platform_ext import teams as tm
        tpl = tm.team_template()
        assert "SOUL.md" in tpl["files"]
        assert tpl["min_members"] == 2

    def test_roster_text(self, isolated_cfg):
        from platform_ext import teams as tm
        t = tm.create_team("组", ["search", "coder"])
        txt = tm.team_roster_text(t["team_id"])
        assert "search" in txt and "coder" in txt


# ── channels ───────────────────────────────────────────────
class TestChannels:
    def test_register_and_list(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        c = ch.HttpChannel("test-http")
        reg.register(c)
        assert reg.get("test-http") is c
        assert any(x["channel_id"] == "test-http" for x in reg.list())

    def test_types_include_builtin(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        reg.register_builtin("miniprogram", lambda *a: None)
        assert "miniprogram" in reg.list_types()

    def test_dispatch_unknown_raises(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        import asyncio
        with pytest.raises(KeyError):
            asyncio.run(reg.dispatch_text("nope", "u1", "hi"))

    def test_dispatch_echo(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        reg.register(ch.HttpChannel("c1"))
        import asyncio
        # 默认处理器回 echo，不抛异常即通过
        asyncio.run(reg.dispatch_text("c1", "u1", "hello"))

    def test_by_type(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        reg.register(ch.HttpChannel("h1"))
        reg.register(ch.WebSocketChannel("w1"))
        assert len(reg.get_by_type(ch.CHANNEL_HTTP)) == 1
        assert len(reg.get_by_type(ch.CHANNEL_WEBSOCKET)) == 1


# ── storage ────────────────────────────────────────────────
class TestStorage:
    def test_create_local(self, isolated_cfg):
        from platform_ext import storage as st
        b = st.create_backend("local", "本机")
        assert b["kind"] == "local"
        assert len(st.list_backends()) == 1

    def test_unsupported_kind(self, isolated_cfg):
        from platform_ext import storage as st
        with pytest.raises(ValueError):
            st.create_backend("s3", "x")

    def test_duplicate_name(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        with pytest.raises(ValueError):
            st.create_backend("local", "a")

    def test_path_traversal_blocked(self, isolated_cfg):
        from platform_ext import storage as st
        assert st.safe_resolve("../../etc/passwd") is None
        assert st.safe_resolve("a\x00b") is None

    def test_write_read_roundtrip(self, isolated_cfg, tmp_path, monkeypatch):
        from platform_ext import storage as st
        st.create_backend("filesystem", "fs", root=str(tmp_path / "ws"))
        assert st.write_text("sub/hello.txt", "内容") is True
        assert st.read_text("sub/hello.txt") == "内容"
        entries = st.list_dir("sub")
        assert any(e["name"] == "hello.txt" for e in entries)

    def test_update_toggle(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        st.update_backend("a", enabled=False)
        assert st.get_backend("a")["enabled"] is False

    def test_delete(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        assert st.delete_backend("a") is True
        assert st.get_backend("a") is None

    def test_probe_local(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        res = st.probe_backend("a")
        assert res["ok"] is True

    def test_supported_kinds(self):
        from platform_ext import storage as st
        assert "local" in st.SUPPORTED_KINDS
        assert "docker" in st.SUPPORTED_KINDS


# ── cli ────────────────────────────────────────────────────
class TestCli:
    def test_parser_builds(self):
        from platform_ext import cli
        p = cli.build_parser()
        args = p.parse_args(["status"])
        assert args.cmd == "status"

    def test_client_url_join(self):
        from platform_ext import cli
        c = cli.Client("http://127.0.0.1:9876/")
        assert c.base_url == "http://127.0.0.1:9876"

    def test_cli_subcommands(self):
        from platform_ext import cli
        p = cli.build_parser()
        for cmd in (["models"], ["skills", "list"], ["experts", "list"], ["connectors", "catalog"], ["ask", "hi"]):
            assert p.parse_args(cmd).cmd == cmd[0]


# ── Webhook 通讯器（gateway_webhook，前端可配的 webhook IM 平台）────────
class TestGatewayWebhook:
    def test_registered_in_platform_kinds(self):
        from platform_ext import gateway as gw
        assert "webhook" in gw.PLATFORM_KINDS
        meta = gw.PLATFORM_KINDS["webhook"]
        names = [f["name"] for f in meta["config_fields"]]
        assert "channel_name" in names and "base_url" in names and "api_key" in names
        # api_key 是敏感字段
        assert "api_key" in gw._SENSITIVE_FIELDS

    def test_sdk_available_and_no_deps(self):
        from platform_ext import gateway as gw
        assert gw._sdk_available("webhook") is True
        assert gw._missing_deps("webhook") == []

    def test_config_parse_and_numeric_coercion(self):
        from platform_ext.gateway_webhook import WebhookConfig
        c = WebhookConfig.from_dict({
            "channel_name": "muse1号", "base_url": "http://x:8100",
            "api_key": "k", "poll_interval": "1", "timeout": "5",
        })
        assert c.channel_name == "muse1号" and c.name == "muse1号"
        assert isinstance(c.poll_interval, float) and c.poll_interval == 1.0
        assert isinstance(c.timeout, float) and c.timeout == 5.0

    def test_parse_inbound_text_and_media(self):
        from platform_ext.gateway_webhook import build_webhook_channel
        ch = build_webhook_channel(object(), config={"channel_name": "muse1号"}, channel_id="i1")
        text = ch.parse_inbound({
            "id": "m-1", "type": "text", "content": "hello", "name": "muse1号",
            "direction": "inbound", "timestamp": 1000.0, "metadata": {"user_key": "wx_a"},
        })
        assert text.text == "hello" and text.channel_subject.subject_id == "wx_a"
        assert text.metadata["webhook_id"] == "m-1"
        img = ch.parse_inbound({
            "id": "m-2", "type": "image", "media_url": "http://cdn/x.png",
            "name": "muse1号", "metadata": {},
        })
        assert img.has_media

    def test_parse_inbound_idempotent_for_inbound_message(self):
        """ChannelManager worker 传入已解析的 InboundMessage 时必须原样返回，
        否则二次当 dict 解析会得到空消息，processor 直接 return，消息进不了 agent。"""
        from octop_gateway.models import InboundMessage, TextContent, ChannelSubject
        from platform_ext.gateway_webhook import build_webhook_channel
        ch = build_webhook_channel(object(), config={"channel_name": "sxkiss"}, channel_id="i1")
        original = InboundMessage(
            channel_id="i1", channel_type="webhook",
            channel_subject=ChannelSubject(subject_id="sxkiss"),
            content=[TextContent(text="你是谁")],
        )
        again = ch.parse_inbound(original)
        assert again is original
        assert again.text == "你是谁"

    def test_start_initializes_cursor_to_now(self, monkeypatch):
        """启动时应把 cursor 初始化为当前时间，避免首轮轮询不带 since 导致历史消息全量回放。"""
        import time as _time
        from platform_ext.gateway_webhook import build_webhook_channel
        ch = build_webhook_channel(object(), config={"channel_name": "sxkiss"}, channel_id="i1")
        assert ch._cursor is None
        async def _fake_ensure_client():
            return None
        async def _fake_poll_loop():
            return None
        monkeypatch.setattr(ch, "_ensure_client", _fake_ensure_client)
        monkeypatch.setattr(ch, "_poll_loop", _fake_poll_loop)
        before = _time.time()
        asyncio.run(ch.start())
        after = _time.time()
        assert ch._cursor is not None
        assert before <= ch._cursor <= after

    def test_start_instance_uses_custom_channel(self, monkeypatch):
        from platform_ext import gateway as gw
        captured = {}
        fake_mgr = MagicMock()
        fake_mgr.start = AsyncMock()
        async def fake_add(*a, **kw):
            if a and hasattr(a[0], "channel_type"):
                captured["type"] = a[0].channel_type
                captured["name"] = a[0]._config.channel_name
            else:
                captured["by_type"] = kw.get("channel_type")
            return "ch-x"
        fake_mgr.add_channel = fake_add
        monkeypatch.setattr(gw, "_get_manager", lambda: fake_mgr)
        monkeypatch.setattr(gw, "_manager_running", False, raising=False)
        monkeypatch.setattr(gw, "_manager_instances", {}, raising=False)
        monkeypatch.setattr("platform_ext.gateway_processor.make_processor", lambda: object())
        asyncio.run(gw._start_instance("wh-i", "webhook", {"channel_name": "测试通道"}))
        assert captured.get("type") == "webhook"
        assert captured.get("name") == "测试通道"
        assert "by_type" not in captured


# ── Webhook 通讯器：附件（媒体）收发 ─────────────────────────
class TestGatewayWebhookMedia:
    def test_rel_media_url_made_absolute(self):
        from octop_gateway.models import ImageContent, FileContent
        from platform_ext.gateway_webhook import build_webhook_channel
        ch = build_webhook_channel(object(), config={
            "channel_name": "m", "base_url": "http://1.2.3.4:8100"}, channel_id="i")
        im = ch.parse_inbound({"id": "m1", "type": "image", "media_url": "/uploads/a.png", "metadata": {}})
        img = [p for p in im.content if isinstance(p, ImageContent)][0]
        assert img.url == "http://1.2.3.4:8100/uploads/a.png"
        assert im.metadata["webhook_media_url"] == "http://1.2.3.4:8100/uploads/a.png"
        # 绝对 url 原样保留
        im2 = ch.parse_inbound({"id": "m2", "type": "file",
                                "media_url": "https://cdn/x/pdf", "metadata": {}})
        f = [p for p in im2.content if isinstance(p, FileContent)][0]
        assert f.url == "https://cdn/x/pdf"

    def test_send_media_base64_type_detection(self, monkeypatch):
        """FileContent 装 base64 图片时，应按 mime 判成 image 而非 file。"""
        import base64
        from octop_gateway.models import FileContent, ChannelSubject
        from platform_ext.gateway_webhook import build_webhook_channel

        posted = {}
        class FakeClient:
            def __init__(self, **kw): pass
            async def post(self, url, data=None, files=None, json=None):
                posted["url"] = url; posted["data"] = data
                posted["json"] = json; posted["files"] = files
                class R: pass
                return R()
        ch = build_webhook_channel(object(), config={"channel_name": "m"}, channel_id="i")
        ch._client = FakeClient()
        subj = ChannelSubject(subject_id="wx_a")
        png = base64.b64encode(b"\x89PNG-fake").decode()
        asyncio.run(ch._send_media(subj, FileContent(data=png, filename="x.png", mime_type="image/png")))
        assert posted["data"]["type"] == "image"
        assert "media" in posted["files"]

    def test_send_media_relative_url_absolute(self, monkeypatch):
        from octop_gateway.models import FileContent, ChannelSubject
        from platform_ext.gateway_webhook import build_webhook_channel
        posted = {}
        class FakeClient:
            def __init__(self, **kw): pass
            async def post(self, url, json=None, **kw):
                posted["json"] = json
                class R: pass
                return R()
        ch = build_webhook_channel(object(), config={
            "channel_name": "m", "base_url": "http://host:8100"}, channel_id="i")
        ch._client = FakeClient()
        asyncio.run(ch._send_media(ChannelSubject(subject_id="u"),
                                   FileContent(url="/uploads/f.pdf", filename="f.pdf")))
        assert posted["json"]["media_url"] == "http://host:8100/uploads/f.pdf"

    def test_fetch_remote_media_uses_auth(self, monkeypatch):
        from platform_ext.gateway_webhook import build_webhook_channel
        captured = {}
        class FakeResp:
            content = b"bytes"
            headers = {"content-type": "image/png"}
            def raise_for_status(self): pass
        class FakeClient:
            def __init__(self, **kw): pass
            async def get(self, url):
                captured["url"] = url
                return FakeResp()
        ch = build_webhook_channel(object(), config={
            "channel_name": "m", "base_url": "http://host:8100", "api_key": "k"}, channel_id="i")
        ch._client = FakeClient()
        data, ctype = asyncio.run(ch.fetch_remote_media("/uploads/x.png"))
        assert captured["url"] == "http://host:8100/uploads/x.png"
        assert data == b"bytes" and ctype == "image/png"
