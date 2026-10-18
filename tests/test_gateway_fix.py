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