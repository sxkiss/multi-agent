"""扫码登录（qr_login + gateway QR 路由）测试。

只测本地逻辑，避免真实调用外部 API（微信/QQ/钉钉/企微）。
覆盖：supported_kinds、save_channel_config 写入、generate/poll 对不支持平台报错、
gateway 路由对不支持平台的正确错误响应。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """把 qr_login 的 config 路径重定向到临时文件。"""
    from platform_ext import qr_login
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(qr_login, "_config_path", lambda: str(cfg))
    return cfg


class TestQrLogin:
    def test_supported_kinds(self):
        from platform_ext import qr_login
        kinds = qr_login.supported_qr_kinds()
        assert "weixin" in kinds
        assert "qq" in kinds
        assert "wecom" in kinds
        assert "dingtalk" in kinds

    def test_generate_unknown_kind(self):
        from platform_ext import qr_login
        with pytest.raises(KeyError):
            asyncio.run(qr_login.generate("nonexistent"))

    def test_poll_unknown_kind(self):
        from platform_ext import qr_login
        with pytest.raises(KeyError):
            asyncio.run(qr_login.poll("nonexistent", "token"))

    def test_save_channel_config(self, isolated_config):
        from platform_ext import qr_login
        qr_login.save_channel_config("weixin", {"token": "abc", "account_id": "a1"})
        g = qr_login._gateway_cfg()
        assert g["weixin"]["token"] == "abc"
        assert g["weixin"]["account_id"] == "a1"
        assert g["weixin"]["configured"] is True

    def test_save_channel_config_overwrite(self, isolated_config):
        from platform_ext import qr_login
        qr_login.save_channel_config("weixin", {"token": "abc"})
        qr_login.save_channel_config("weixin", {"token": "def", "base_url": "x"})
        g = qr_login._gateway_cfg()
        assert g["weixin"]["token"] == "def"
        assert g["weixin"]["base_url"] == "x"

    def test_gateway_generate_unsupported(self):
        from platform_ext import gateway
        res = asyncio.run(gateway.gateway_qrcode_generate("feishu", None))  # type: ignore[arg-type]
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is False
        assert "不支持扫码登录" in body["msg"]


class TestGatewayQrRoutes:
    def test_qr_routes_registered(self):
        from platform_ext import gateway
        paths = [r.path for r in gateway.router.routes]
        assert "/gateway/channels/{kind}/qrcode/generate" in paths
        assert "/gateway/channels/{kind}/qrcode/poll" in paths