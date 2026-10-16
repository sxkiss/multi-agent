"""Octop 连接器适配层测试（/home/sxkiss/bt/connector_hub）。

覆盖：
- catalog 加载（27 个连接器、本地化、supported 判定）
- Fernet 凭据加解密
- 实例 CRUD + mcp/config.json 同步（增删只碰 chub__ 前缀，不动其它）
- gateway registry（list_tools / MCP JSON-RPC 协议）
- http_mcp 工具文本抽取

不触网、不依赖生产 config（用临时目录重定向）。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── catalog ────────────────────────────────────────────────
class TestCatalog:
    def test_loads_27_connectors(self):
        from connector_hub import connectors_catalog as cat
        entries = cat.list_catalog_entries()
        assert len(entries) == 27

    def test_known_kind_present(self):
        from connector_hub import connectors_catalog as cat
        for kind in ("tencent-docs", "tencent-ima", "qq-mail", "dify", "notion"):
            assert cat.get_catalog_entry(kind) is not None

    def test_localized_text(self):
        from connector_hub import connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-docs")
        assert e.name.resolve("zh") == "腾讯文档"
        assert e.name.resolve("en") == "Tencent Docs"

    def test_supported_flag(self):
        from connector_hub import connectors_catalog as cat
        # personal_token / api_key 支持
        assert cat.get_catalog_entry("tencent-docs").supported is True
        assert cat.get_catalog_entry("qq-mail").supported is True
        # oauth2 暂不支持（未实现 OAuth 全流）
        assert cat.get_catalog_entry("notion").supported is False

    def test_to_api_shape(self):
        from connector_hub import connectors_catalog as cat
        d = cat.get_catalog_entry("tencent-docs").to_api("zh")
        for k in ("kind", "name", "auth_kind", "mcp_mode", "category", "supported", "credential_fields"):
            assert k in d


# ── Fernet 凭据 ────────────────────────────────────────────
class TestCredentials:
    def test_encrypt_decrypt_roundtrip(self):
        from connector_hub import connectors_service as svc
        payload = {"api_key": "secret-123", "note": "中文"}
        token = svc.encrypt_credentials(payload)
        assert isinstance(token, str) and token
        assert "secret-123" not in token  # 密文不含明文
        assert svc.decrypt_credentials(token) == payload

    def test_tamper_rejected(self):
        from connector_hub import connectors_service as svc
        token = svc.encrypt_credentials({"a": 1})
        with pytest.raises(Exception):
            svc.decrypt_credentials(token[:-4] + "AAAA")


# ── 实例 CRUD（隔离到临时 config）──────────────────────────
@pytest.fixture
def isolated_service(tmp_path, monkeypatch):
    """把 config.json 与 mcp/config.json 重定向到临时目录。"""
    from connector_hub import connectors_service as svc
    cfg_file = tmp_path / "config.json"
    mcp_dir = tmp_path / "mcp"
    mcp_dir.mkdir()
    mcp_file = mcp_dir / "config.json"
    mcp_file.write_text(json.dumps({"mcpServers": {"existing-server": {"type": "stdio"}}}), encoding="utf-8")
    monkeypatch.setattr(svc, "CONFIG_FILE", str(cfg_file))
    monkeypatch.setattr(svc, "MCP_CONFIG_FILE", str(mcp_file))
    monkeypatch.setattr(svc, "_FERNET_KEY_FILE", str(tmp_path / ".fernet_key"))
    return svc, cfg_file, mcp_file


class TestInstances:
    def test_create_and_list(self, isolated_service):
        svc, cfg, mcp = isolated_service
        inst = svc.create_instance("qq-music", "我的音乐", {"api_key": "k"})
        assert inst["kind"] == "qq-music"
        assert "creds_enc" not in inst  # 不外泄密文
        lst = svc.list_instances()
        assert len(lst) == 1

    def test_unknown_kind_rejected(self, isolated_service):
        svc, _, _ = isolated_service
        with pytest.raises(ValueError):
            svc.create_instance("no-such-kind", "x", {})

    def test_oauth2_unsupported_rejected(self, isolated_service):
        svc, _, _ = isolated_service
        with pytest.raises(ValueError):
            svc.create_instance("notion", "x", {})

    def test_sync_adds_octop_prefix_only(self, isolated_service):
        svc, cfg, mcp = isolated_service
        svc.create_instance("qq-music", "m", {"api_key": "k"})
        data = json.loads(mcp.read_text(encoding="utf-8"))
        servers = data["mcpServers"]
        assert "existing-server" in servers  # 原有条目保留
        octop_keys = [k for k in servers if k.startswith("chub__")]
        assert len(octop_keys) == 1

    def test_delete_removes_octop_entry(self, isolated_service):
        svc, cfg, mcp = isolated_service
        inst = svc.create_instance("qq-music", "m", {"api_key": "k"})
        assert svc.delete_instance(inst["instance_id"]) is True
        data = json.loads(mcp.read_text(encoding="utf-8"))
        servers = data["mcpServers"]
        assert "existing-server" in servers
        assert not [k for k in servers if k.startswith("chub__")]

    def test_update_enabled_toggles_sync(self, isolated_service):
        svc, cfg, mcp = isolated_service
        inst = svc.create_instance("qq-music", "m", {"api_key": "k"})
        svc.update_instance(inst["instance_id"], enabled=False)
        data = json.loads(mcp.read_text(encoding="utf-8"))
        assert not [k for k in data["mcpServers"] if k.startswith("chub__")]


# ── gateway registry ───────────────────────────────────────
class TestGateway:
    def test_registry_has_kinds(self):
        from connector_hub import connectors_gateway as gw
        kinds = gw.list_gateway_kinds()
        assert "qq-music" in kinds
        assert "tencent-ima" in kinds
        assert len(kinds) >= 15

    def test_list_tools_real(self):
        from connector_hub import connectors_gateway as gw
        tools = gw.mcp_tools_for_kind("qq-music")
        assert len(tools) == 5
        assert any(t["name"] == "search_music" for t in tools)

    def test_mcp_initialize(self):
        from connector_hub import connectors_gateway as gw
        res = gw.handle_mcp_request(kind="qq-music", creds={}, body={"method": "initialize", "id": 1, "params": {}})
        assert res["result"]["serverInfo"]["name"] == "bt-qq-music"

    def test_mcp_tools_list(self):
        from connector_hub import connectors_gateway as gw
        res = gw.handle_mcp_request(kind="baidu-map", creds={}, body={"method": "tools/list", "id": 2, "params": {}})
        assert "tools" in res["result"]

    def test_mcp_unknown_method(self):
        from connector_hub import connectors_gateway as gw
        res = gw.handle_mcp_request(kind="qq-music", creds={}, body={"method": "bogus", "id": 3, "params": {}})
        assert res["error"]["code"] == -32601

    def test_unknown_kind_raises(self):
        from connector_hub import connectors_gateway as gw
        with pytest.raises(ValueError):
            gw.call_gateway_tool("nope", {}, "x", {})


# ── http_mcp 工具文本抽取 ──────────────────────────────────
class TestHttpMcpHelpers:
    def test_extract_text_content(self):
        from connector_hub.http_mcp import _extract_tool_text
        res = {"content": [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]}
        assert _extract_tool_text(res) == "hello\nworld"

    def test_extract_string_content(self):
        from connector_hub.http_mcp import _extract_tool_text
        assert _extract_tool_text({"content": "plain"}) == "plain"

    def test_last_sse_data(self):
        from connector_hub.http_mcp import _last_sse_data
        sse = "event: message\ndata: {\"a\":1}\n\nevent: message\ndata: {\"b\":2}\n"
        assert _last_sse_data(sse) == '{"b":2}'


# ── CLI 安装器 ─────────────────────────────────────────────
class TestCliInstall:
    def test_specs_defined(self):
        from connector_hub.gateway_adapters import cli_install as ci
        for kind in ("feishu-cli", "wecom-cli", "agently-cli", "obsidian-cli"):
            spec = ci.get_cli_install_spec(kind)
            assert spec is not None
            assert spec.binary

    def test_feishu_npm_package(self):
        from connector_hub.gateway_adapters import cli_install as ci
        spec = ci.get_cli_install_spec("feishu-cli")
        assert spec.npm_package == "@larksuite/cli"
        assert spec.binary == "lark-cli"
        assert spec.install_command == "npm install -g @larksuite/cli"

    def test_wecom_npm_package(self):
        from connector_hub.gateway_adapters import cli_install as ci
        assert ci.get_cli_install_spec("wecom-cli").npm_package == "@wecom/cli"

    def test_agently_pinned_version(self):
        from connector_hub.gateway_adapters import cli_install as ci
        assert ci.get_cli_install_spec("agently-cli").npm_package == "@tencent-qqmail/agently-cli@1.0.18"

    def test_obsidian_no_npm(self):
        from connector_hub.gateway_adapters import cli_install as ci
        assert ci.get_cli_install_spec("obsidian-cli").npm_package is None

    def test_status_shape(self):
        from connector_hub.gateway_adapters import cli_install as ci
        s = ci.cli_install_status("feishu-cli")
        for k in ("kind", "binary", "npm_package", "install_command", "installed", "doc_url"):
            assert k in s
        assert s["kind"] == "feishu-cli"
        assert isinstance(s["installed"], bool)

    def test_status_unknown_kind_raises(self):
        from connector_hub.gateway_adapters import cli_install as ci
        with pytest.raises(ValueError):
            ci.cli_install_status("no-such-cli")

    def test_ensure_cli_path_returns_str(self):
        from connector_hub.gateway_adapters import cli_install as ci
        assert isinstance(ci.ensure_cli_path(), str)

    def test_install_unknown_kind_returns_fail(self):
        from connector_hub.gateway_adapters import cli_install as ci
        # 未知 kind 不抛异常（cli_install_status 会抛，但 install_connector_cli 先取 spec）
        with pytest.raises(ValueError):
            ci.install_connector_cli("no-such-cli")

    def test_external_cli_kinds_registered(self):
        from connector_hub import connectors_gateway as gw
        for k in ("feishu-cli", "wecom-cli", "agently-cli", "obsidian-cli"):
            assert k in gw.EXTERNAL_CLI_KINDS
            assert gw.get_gateway_adapter(k) is not None
