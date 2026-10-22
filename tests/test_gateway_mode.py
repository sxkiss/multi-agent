"""通道对话模式配置测试：gateway_processor 读取通道配置的 mode，默认单 Agent。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _FakeGateway:
    def __init__(self, cfg: dict):
        self._cfg = cfg

    def _gateway_cfg(self):
        return self._cfg


def test_channel_mode_default_single():
    from platform_ext.gateway_processor import _channel_mode

    gw = _FakeGateway({"weixin": {"configured": True}})
    assert _channel_mode(gw, "weixin") == "single"


def test_channel_mode_group():
    from platform_ext.gateway_processor import _channel_mode

    gw = _FakeGateway({"weixin": {"configured": True, "mode": "group"}})
    assert _channel_mode(gw, "weixin") == "group"


def test_channel_mode_claude():
    from platform_ext.gateway_processor import _channel_mode

    gw = _FakeGateway({"weixin": {"configured": True, "mode": "claude"}})
    assert _channel_mode(gw, "weixin") == "claude"


def test_norm_cfg_strips_mode_and_template():
    from platform_ext.gateway import _norm_cfg

    norm = _norm_cfg("weixin", {
        "token": "t", "account_id": "a", "template": "x", "mode": "group",
    })
    # 剔除 mode/template，只留 SDK 需要的 accounts
    assert "accounts" in norm
    assert norm["accounts"] == [{"account_id": "a", "token": "t", "base_url": "https://ilinkai.weixin.qq.com"}]


def test_platform_kinds_include_mode_field():
    from platform_ext.gateway import _build_platform_kinds

    kinds = _build_platform_kinds()
    for kind, meta in kinds.items():
        names = [f.get("name") for f in meta.get("config_fields", [])]
        assert "mode" in names, f"{kind} config_fields 缺少 mode 字段"
        assert "template" in names, f"{kind} config_fields 缺少 template 字段"