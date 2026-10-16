"""octop-gateway 多平台通道桥 + octop-memory 增强记忆测试。

覆盖：
- gateway：平台枚举、配置保存、SDK 缺失时的清晰错误
- memory：store / recall / list / get / delete / export / import / status
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def clear_memory(tmp_path, monkeypatch):
    """每个测试用独立 sqlite 文件。"""
    from platform_ext import memory_octop
    db = tmp_path / "test_memory.sqlite"
    monkeypatch.setattr(memory_octop, "SQLITE_PATH", str(db))
    memory_octop._memory = None  # 强制重建
    yield


class TestGateway:
    def test_platform_kinds(self):
        from platform_ext import gateway
        assert "weixin" in gateway.PLATFORM_KINDS
        assert "feishu" in gateway.PLATFORM_KINDS
        assert "telegram" in gateway.PLATFORM_KINDS
        assert gateway.PLATFORM_KINDS["weixin"]["label"]

    def test_list_channels(self):
        from platform_ext import gateway
        res = asyncio.run(gateway.gateway_list_channels())
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is True
        assert body["data"]["channels"]
        kinds = {c["kind"] for c in body["data"]["channels"]}
        assert "weixin" in kinds

    def test_status(self):
        from platform_ext import gateway
        res = asyncio.run(gateway.gateway_status())
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is True
        assert "running_channels" in body["data"]

    def test_start_missing_sdk(self):
        from platform_ext import gateway
        res = asyncio.run(gateway.gateway_start("telegram"))
        body = json.loads(res.body.decode("utf-8"))
        # SDK 未装时应返回清晰错误（非崩溃）
        assert body["status"] is False

    def test_unknown_kind(self):
        from platform_ext import gateway
        res = asyncio.run(gateway.gateway_start("nonexistent"))
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is False


class TestMemory:
    def test_store_and_recall(self):
        from platform_ext import memory_octop
        m = memory_octop._get_memory()
        node = m.store(content="BT 是单用户无 DB 的 agent 平台", topic="project")
        assert node.content
        nodes = m.recall("单用户", limit=3)
        assert any("BT" in n.content for n in nodes)

    def test_store_requires_content(self):
        from platform_ext import memory_octop
        import asyncio
        from fastapi import Request

        class FakeRequest:
            async def json(self):
                return {}

        res = asyncio.run(memory_octop.memory_store(FakeRequest()))  # type: ignore[arg-type]
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is False

    def test_status_api(self):
        from platform_ext import memory_octop
        m = memory_octop._get_memory()
        m.store(content="测试记忆", topic="t")
        res = asyncio.run(memory_octop.memory_status())
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is True
        assert body["data"]["total_nodes"] >= 1

    def test_list_and_get(self):
        from platform_ext import memory_octop
        m = memory_octop._get_memory()
        node = m.store(content="记住这个地址", topic="address")
        res = asyncio.run(memory_octop.memory_list(topic="address"))
        body = json.loads(res.body.decode("utf-8"))
        assert body["data"]["nodes"]
        res2 = asyncio.run(memory_octop.memory_get(node.id))
        body2 = json.loads(res2.body.decode("utf-8"))
        assert body2["data"]["node"]["id"] == node.id

    def test_delete(self):
        from platform_ext import memory_octop
        m = memory_octop._get_memory()
        node = m.store(content="将被删除", topic="tmp")
        res = asyncio.run(memory_octop.memory_delete(node.id))
        body = json.loads(res.body.decode("utf-8"))
        assert body["status"] is True

    def test_export_import(self):
        from platform_ext import memory_octop
        m = memory_octop._get_memory()
        m.store(content="可迁移的记忆", topic="migration")
        res = asyncio.run(memory_octop.memory_export())
        body = json.loads(res.body.decode("utf-8"))
        assert body["data"]["nodes"]

        # 清空后导入
        nodes = body["data"]["nodes"]
        memory_octop._memory = None
        m2 = memory_octop._get_memory()

        class FakeReq:
            def __init__(self):
                self._data = {"nodes": nodes}
            async def json(self):
                return self._data

        res2 = asyncio.run(memory_octop.memory_import(FakeReq()))  # type: ignore[arg-type]
        body2 = json.loads(res2.body.decode("utf-8"))
        assert body2["data"]["imported"] >= 1