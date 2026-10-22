"""通道自动启动恢复测试：重启后恢复手动启动过、未手动停止的通道。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def test_mark_unmark_auto_start(monkeypatch):
    import platform_ext.gateway as gw

    cfg = {}
    monkeypatch.setattr(gw, "_gateway_cfg", lambda: cfg)
    monkeypatch.setattr(gw, "_set_gateway_cfg", lambda c: cfg.update(c))

    gw.mark_auto_start("weixin")
    assert gw._auto_start_list() == ["weixin"]

    gw.mark_auto_start("wecom")
    assert gw._auto_start_list() == ["weixin", "wecom"]

    # 重复 mark 去重
    gw.mark_auto_start("weixin")
    assert gw._auto_start_list() == ["weixin", "wecom"]

    gw.unmark_auto_start("weixin")
    assert gw._auto_start_list() == ["wecom"]


def test_auto_start_list_filters_unknown_kind(monkeypatch):
    import platform_ext.gateway as gw

    cfg = {"_auto_start": ["weixin", "not-a-real-channel"]}
    monkeypatch.setattr(gw, "_gateway_cfg", lambda: cfg)
    monkeypatch.setattr(gw, "_set_gateway_cfg", lambda c: cfg.update(c))
    assert gw._auto_start_list() == ["weixin"]


@pytest.mark.asyncio
async def test_restore_auto_start_only_configured(monkeypatch):
    import platform_ext.gateway as gw

    started = []

    async def fake_start(kind, cfg):
        started.append(kind)
        return f"cid-{kind}"

    cfg = {
        "weixin": {"configured": True, "token": "t", "account_id": "a"},
        "wecom": {"configured": False},  # 未配置，应跳过
        "_auto_start": ["weixin", "wecom"],
    }
    monkeypatch.setattr(gw, "_gateway_cfg", lambda: cfg)
    monkeypatch.setattr(gw, "_set_gateway_cfg", lambda c: cfg.update(c))
    monkeypatch.setattr(gw, "_sdk_available", lambda k: True)
    monkeypatch.setattr(gw, "_ensure_deps", lambda k: (True, ""))
    monkeypatch.setattr(gw, "_start_channel", fake_start)

    await gw.restore_auto_start_channels()
    assert started == ["weixin"]


@pytest.mark.asyncio
async def test_restore_auto_start_skips_missing_sdk(monkeypatch):
    import platform_ext.gateway as gw

    started = []

    async def fake_start(kind, cfg):
        started.append(kind)
        return f"cid-{kind}"

    cfg = {"weixin": {"configured": True}, "_auto_start": ["weixin"]}
    monkeypatch.setattr(gw, "_gateway_cfg", lambda: cfg)
    monkeypatch.setattr(gw, "_set_gateway_cfg", lambda c: cfg.update(c))
    monkeypatch.setattr(gw, "_sdk_available", lambda k: False)  # SDK 不可用
    monkeypatch.setattr(gw, "_ensure_deps", lambda k: (True, ""))
    monkeypatch.setattr(gw, "_start_channel", fake_start)

    await gw.restore_auto_start_channels()
    assert started == []