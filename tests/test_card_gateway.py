"""platform_ext.card_gateway 的单元测试。

覆盖：鉴权、CRUD、启动/停止（mock ChannelManager）、隔离性。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 把项目根目录加入 sys.path，确保 platform_ext 可导入
BASE_DIR = str(Path(__file__).resolve().parent.parent)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


@pytest.fixture
def tmp_dirs(tmp_path: Path):
    """为每次测试提供独立的卡密数据目录，并在结束后恢复模块全局状态。

    为什么必须恢复：card_gateway 的 PROJECT_ROOT 与 _card_channels_path 都是
    模块级全局，若只改不还原，后续测试（含其他模块）会读到上一个测试的临时
    目录，导致「单跑通过、全量失败」的假阴性。
    """
    import platform_ext.card_gateway as m

    data_dir = tmp_path / "card_data"
    data_dir.mkdir(parents=True)

    saved_root = m.PROJECT_ROOT
    saved_func = m._card_channels_path

    m.PROJECT_ROOT = str(data_dir)
    m._card_channels_path = lambda: str(data_dir / "card_channels.json")
    try:
        yield data_dir
    finally:
        m.PROJECT_ROOT = saved_root
        m._card_channels_path = saved_func


def _build_module(data_dir: Path):
    """返回已指向 data_dir 的模块对象（状态由 tmp_dirs fixture 负责恢复）。"""
    import platform_ext.card_gateway as m
    return m


def _make_card_token(prefix: str) -> str:
    """签发一个 fake JWT 用于测试，payload sub=card:<prefix>。"""
    from auth import create_token, load_auth
    return create_token(str(Path(BASE_DIR)), ttl_hours=1, sub=f"card:{prefix}")


class TestListChannels:
    def test_empty_returns_empty_list(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        prefix = "CARD001"
        channels = m.list_channels(prefix)
        assert channels == []

    def test_returns_existing_channels(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        prefix = "CARD002"
        ch = m.create_channel(prefix, "mqtt", "我的MQTT", {"host": "localhost", "subscribe_topic": "test"})
        assert ch is not None
        channels = m.list_channels(prefix)
        assert len(channels) == 1
        assert channels[0]["kind"] == "mqtt"
        assert channels[0]["name"] == "我的MQTT"
        assert channels[0]["channel_id"] == ch["channel_id"]


class TestCreateChannel:
    def test_creates_valid_mqtt(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ch = m.create_channel("C1", "mqtt", "test-mqtt", {
            "host": "broker.example.com",
            "subscribe_topic": "devices/C1/in",
        })
        assert ch is not None
        assert ch["kind"] == "mqtt"
        assert ch["instance_id"].startswith("card_C1_")
        assert ch["configured"] is True  # mqtt required: host (present)

    def test_invalid_kind_returns_none(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ch = m.create_channel("C2", "nonexistent", "", {})
        assert ch is None

    def test_config_required_fields(self, tmp_dirs):
        """mqtt 的 required 是 host + subscribe_topic，缺一不可。"""
        m = _build_module(tmp_dirs)
        ch = m.create_channel("C3", "mqtt", "", {})
        assert ch["configured"] is False
        # 只补 host 仍不齐
        updated = m.update_channel("C3", ch["channel_id"], {"config": {"host": "b"}})
        assert updated["configured"] is False
        # 再补 subscribe_topic 才齐
        updated = m.update_channel("C3", ch["channel_id"], {"config": {"subscribe_topic": "in"}})
        assert updated["configured"] is True


class TestUpdateChannel:
    def test_update_name_and_enabled(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ch = m.create_channel("U1", "mqtt", "old", {"host": "h"})
        updated = m.update_channel("U1", ch["channel_id"], {"name": "new", "enabled": False})
        assert updated["name"] == "new"
        assert updated["enabled"] is False
        # 再次读取确认持久化
        channels = m.list_channels("U1")
        assert channels[0]["name"] == "new"
        assert channels[0]["enabled"] is False

    def test_not_found_returns_none(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        assert m.update_channel("X", "ch_fake", {}) is None

    def test_update_config_field(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ch = m.create_channel("U2", "mqtt", "", {"host": "h1"})
        updated = m.update_channel("U2", ch["channel_id"], {"config": {"host": "h2"}})
        assert updated["config"]["host"] == "h2"


class TestDeleteChannel:
    def test_deletes_existing(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ch = m.create_channel("D1", "mqtt", "x", {"host": "h"})
        assert m.delete_channel("D1", ch["channel_id"]) is True
        assert len(m.list_channels("D1")) == 0

    def test_delete_nonexistent_returns_false(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        assert m.delete_channel("D1", "ch_fake") is False


class TestStartStopChannel:
    def test_start_without_real_manager(self, tmp_dirs):
        """不依赖真实 ChannelManager，mock 掉 _start_instance 验证流程。"""
        m = _build_module(tmp_dirs)
        ch = m.create_channel("S1", "mqtt", "x", {"host": "h"})
        with patch.object(m.gw, "_start_instance", new=AsyncMock()):
            ok, msg = asyncio.run(m.start_channel("S1", ch["channel_id"]))
        assert ok is True
        assert msg == "已启动"

    def test_stop_without_real_manager(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ch = m.create_channel("S2", "mqtt", "x", {"host": "h"})
        with patch.object(m.gw, "_stop_instance", new=AsyncMock()):
            result = asyncio.run(m.stop_channel("S2", ch["channel_id"]))
        assert result is True

    def test_start_nonexistent_returns_false(self, tmp_dirs):
        m = _build_module(tmp_dirs)
        ok, msg = asyncio.run(m.start_channel("S3", "ch_fake"))
        assert ok is False
        assert "不存在" in msg


class TestIsolation:
    def test_two_users_separate(self, tmp_dirs):
        """不同卡密前缀各自独立。"""
        m = _build_module(tmp_dirs)
        ch_a = m.create_channel("ISO_A", "mqtt", "a", {"host": "h"})
        ch_b = m.create_channel("ISO_B", "mqtt", "b", {"host": "h"})
        assert len(m.list_channels("ISO_A")) == 1
        assert len(m.list_channels("ISO_B")) == 1
        assert ch_a["channel_id"] != ch_b["channel_id"]
        # A 删掉自己的，B 不受影响
        m.delete_channel("ISO_A", ch_a["channel_id"])
        assert len(m.list_channels("ISO_A")) == 0
        assert len(m.list_channels("ISO_B")) == 1


