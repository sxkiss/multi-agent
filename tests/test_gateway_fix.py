"""gateway 通道层修复测试。

覆盖：
- config_fields 与底层 SDK 真实凭据字段对齐（手动配置、扫码绑定、SDK 启动字段一致）
- _norm_cfg 归一化（weixin 扁平→accounts、mqtt port→int、其它透传）
- gateway_processor 的 session_id 生成与文本提取
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

# 触发 vendor 加入 sys.path（octop_gateway / octop_memory 随仓库携带在 vendor/ 下）
import platform_ext  # noqa: E402


class TestPlatformMetaAligned:
    """config_fields 必须能映射到底层 SDK 的 required_credentials。"""

    def _load_sdk_requirements(self) -> dict[str, tuple]:
        """读取底层各平台 ChannelConfig 的 required_credentials。"""
        req = {}
        try:
            import importlib

            for kind in ("feishu", "wecom", "dingtalk", "discord", "qq"):
                mod = importlib.import_module(f"octop_gateway.channels.{kind}")
                cls = None
                for name in dir(mod):
                    if name.endswith("Config"):
                        obj = getattr(mod, name)
                        if getattr(obj, "required_credentials", None):
                            cls = obj
                            break
                if cls is not None:
                    req[kind] = tuple(cls.required_credentials)
        except Exception:
            pass
        return req

    def test_fields_exist_in_config_meta(self):
        from platform_ext import gateway

        for kind, meta in gateway._PLATFORM_META.items():
            names = [f["name"] for f in meta.get("config_fields", [])]
            # 每通道至少有一个字段
            assert names, f"{kind} config_fields 为空"

    def test_qq_fields_aligned(self):
        """QQ 需要 app_id+secret，而非旧的 account。"""
        from platform_ext import gateway

        names = [f["name"] for f in gateway._PLATFORM_META["qq"]["config_fields"]]
        assert "app_id" in names
        assert "secret" in names
        assert "account" not in names

    def test_wecom_fields_aligned(self):
        """企微需要 bot_id+secret，而非旧的 corp_id。"""
        from platform_ext import gateway

        names = [f["name"] for f in gateway._PLATFORM_META["wecom"]["config_fields"]]
        assert "bot_id" in names
        assert "secret" in names
        assert "corp_id" not in names

    def test_mqtt_fields_aligned(self):
        """MQTT 需要 host/subscribe_topic 等。"""
        from platform_ext import gateway

        names = [f["name"] for f in gateway._PLATFORM_META["mqtt"]["config_fields"]]
        assert "host" in names
        assert "subscribe_topic" in names
        assert "broker" not in names

    def test_sdk_required_credentials_covered(self):
        """对底层库能加载的平台，config_fields 必须覆盖其 required_credentials。"""
        from platform_ext import gateway

        req = self._load_sdk_requirements()
        for kind, creds in req.items():
            if kind not in gateway._PLATFORM_META:
                continue
            names = set(f["name"] for f in gateway._PLATFORM_META[kind]["config_fields"])
            for c in creds:
                assert c in names, f"{kind} 缺少 SDK 必需字段 {c}"


class TestNormCfg:
    def test_weixin_flat_to_accounts(self):
        from platform_ext import gateway

        out = gateway._norm_cfg("weixin", {
            "token": "tok123",
            "account_id": "a1",
            "base_url": "https://x",
        })
        assert out["accounts"][0]["account_id"] == "a1"
        assert out["accounts"][0]["token"] == "tok123"
        assert out["accounts"][0]["base_url"] == "https://x"

    def test_weixin_missing_account_returns_empty(self):
        from platform_ext import gateway

        out = gateway._norm_cfg("weixin", {"token": "tok123"})
        assert out["accounts"] == []

    def test_mqtt_port_to_int(self):
        from platform_ext import gateway

        out = gateway._norm_cfg("mqtt", {"host": "h", "port": "1883", "subscribe_topic": "t"})
        assert out["port"] == 1883

    def test_mqtt_bad_port_default(self):
        from platform_ext import gateway

        out = gateway._norm_cfg("mqtt", {"host": "h", "port": "abc", "subscribe_topic": "t"})
        assert out["port"] == 8883

    def test_other_kind_passthrough(self):
        from platform_ext import gateway

        cfg = {"app_id": "a", "app_secret": "s"}
        out = gateway._norm_cfg("feishu", cfg)
        assert out == cfg


class TestGatewayProcessor:
    def test_session_id_safe(self):
        from platform_ext.gateway_processor import session_id_for

        sid = session_id_for("weixin", "openid_abc-123")
        assert sid.startswith("gw_weixin_")
        assert "/" not in sid
        assert ".." not in sid

    def test_session_id_anon(self):
        from platform_ext.gateway_processor import session_id_for

        sid = session_id_for("weixin", None)
        assert "anon" in sid

    def test_extract_text(self):
        from octop_gateway.models import InboundMessage, TextContent
        from platform_ext.gateway_processor import _extract_text

        msg = InboundMessage(
            channel_id="c1",
            content=[TextContent(text="hello")],
        )
        assert _extract_text(msg) == "hello"

    def test_channel_template_read(self):
        from platform_ext.gateway_processor import _channel_template

        class FakeGateway:
            @staticmethod
            def _gateway_cfg():
                return {"weixin": {"template": "hermes"}}

        assert _channel_template(FakeGateway(), "weixin") == "hermes"
        assert _channel_template(FakeGateway(), "qq") == ""

    def test_processor_passes_template(self):
        """processor 发起对话时把通道 template 传给 chat_start。"""
        import asyncio

        from octop_gateway.models import InboundMessage, TextContent
        from platform_ext.gateway_processor import make_processor

        captured = {}

        class FakeAgentMain:
            def chat_start(self, params):
                captured.update(params)
                return {"status": True, "data": {}}

            async def chat_events(self, params):
                yield 'event: message\ndata: {"event":"message","data":{"content":"ok"}}\n\n'
                yield 'event: message_end\ndata: {"event":"message_end"}\n\n'

        class FakeGateway:
            @staticmethod
            def _gateway_cfg():
                return {"weixin": {"template": "gpt5.5"}}

        msg = InboundMessage(
            channel_id="c1",
            channel_type="weixin",
            content=[TextContent(text="hi")],
        )

        async def run():
            proc = make_processor(agent_main=FakeAgentMain(), gateway=FakeGateway())
            async for _ in proc(msg):
                pass

        asyncio.run(run())
        assert captured.get("template") == "gpt5.5"
        assert captured.get("session_id", "").startswith("gw_weixin_")

    def test_processor_cancels_running_before_start(self):
        """连续对话：会话有运行中任务时先取消再发新消息。"""
        import asyncio

        from octop_gateway.models import InboundMessage, TextContent
        from platform_ext.gateway_processor import make_processor

        calls = []

        class FakeJob:
            is_running = True

        class FakeChatJobs:
            def __init__(self):
                self._jobs = {"k": FakeJob()}

            def active_for_session(self, sid):
                return [("k", self._jobs["k"])]

        class FakeAgentMain:
            def chat_stop(self, params):
                calls.append(("stop", params.get("session_id")))
                self._stopped = True

            def chat_start(self, params):
                calls.append(("start", params.get("session_id")))
                return {"status": True, "data": {}}

            async def chat_events(self, params):
                yield 'event: message\ndata: {"event":"message","data":{"content":"ok"}}\n\n'
                yield 'event: message_end\ndata: {"event":"message_end"}\n\n'

        # 覆盖 _wait_tasks_stopped：让 FakeJob 在 stop 后不再 running
        import platform_ext.gateway_processor as gp

        orig_wait = gp._wait_tasks_stopped

        def fake_wait(ws, sid, timeout=5.0):
            ws._jobs["k"].is_running = False

        gp._wait_tasks_stopped = fake_wait

        msg = InboundMessage(
            channel_id="c1",
            channel_type="weixin",
            content=[TextContent(text="hi")],
        )

        async def run():
            proc = make_processor(
                agent_main=FakeAgentMain(),
                chat_jobs=FakeChatJobs(),
                gateway=None,
            )
            async for _ in proc(msg):
                pass

        try:
            asyncio.run(run())
        finally:
            gp._wait_tasks_stopped = orig_wait

        # 先 stop 后 start
        assert calls[0][0] == "stop"
        assert calls[1][0] == "start"