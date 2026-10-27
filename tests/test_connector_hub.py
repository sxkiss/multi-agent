"""连接器适配层测试（/home/sxkiss/bt/connector_hub）。

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
    def test_loads_connectors(self):
        from connector_hub import connectors_catalog as cat
        entries = cat.list_catalog_entries()
        # 含 WorkBuddy 443 个连接器（原 27/28 个）
        assert len(entries) >= 400
        assert len(entries) == 466

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
        # oauth2 已实现 OAuth 全流（DCR + PKCE + 刷新），应标记为支持
        assert cat.get_catalog_entry("notion").supported is True
        # 非 catalog 内的 OAuth2 kind 不应被误判为支持
        assert cat.get_catalog_entry("tencent-docs").supported is True

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


# ── 回归：remote spec / 凭据归一化 ──────
class TestRemoteSpecAlignment:
    """本次修复的回归用例（此前 4 个真实缺陷）。"""

    def test_remote_spec_tencent_docs_uses_openapi_path_and_auth_header(self):
        """腾讯文档必须用 /openapi/mcp，token 放 Authorization 头（非 query）。"""
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-docs")
        url, headers, transport = svc._remote_spec_for("tencent-docs", {"token": "T"}, e)
        assert url == "https://docs.qq.com/openapi/mcp"
        assert headers["Authorization"] == "T"
        assert "token=" not in url
        assert transport == "streamable_http"

    def test_remote_spec_tencent_meeting_headers(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-meeting")
        url, headers, transport = svc._remote_spec_for("tencent-meeting", {"token": "T"}, e)
        assert url == "https://mcp.meeting.tencent.com/mcp/wemeet-open/v1"
        assert headers["X-Tencent-Meeting-Token"] == "T"
        assert headers["X-Skill-Version"] == "v1.0.1"

    def test_remote_spec_weiyun_normalizes_token(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-weiyun")
        _, headers, _ = svc._remote_spec_for(
            "tencent-weiyun", {"token": "WyHeader: mcp_token=abc123"}, e
        )
        assert headers["WyHeader"] == "mcp_token=abc123"

    def test_remote_spec_youdao_uses_sse_and_x_api_key(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("youdao-note")
        url, headers, transport = svc._remote_spec_for("youdao-note", {"token": "T"}, e)
        assert transport == "sse"
        assert headers["x-api-key"] == "T"

    def test_custom_mcp_dify_uses_user_url(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("dify")
        spec = svc._remote_spec_for(
            "dify", {"mcp_url": "https://dify.example.com/mcp/a/b"}, e
        )
        assert spec is not None
        assert spec[0] == "https://dify.example.com/mcp/a/b"
        assert spec[2] == "streamable_http"

    def test_oauth2_remote_requires_access_token(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("notion")
        assert svc._remote_spec_for("notion", {}, e) is None
        url, headers, transport = svc._remote_spec_for(
            "notion", {"access_token": "AT"}, e
        )
        assert url == "https://mcp.notion.com/mcp"
        assert headers["Authorization"] == "Bearer AT"
        assert transport == "streamable_http"

    def test_transport_normalization(self):
        from connector_hub import connectors_service as svc
        assert svc._normalize_transport("raw_http") == "streamable_http"
        assert svc._normalize_transport(None) == "streamable_http"
        assert svc._normalize_transport("sse") == "sse"
        assert svc._normalize_transport("streamable_http") == "streamable_http"

    def test_generate_spec_includes_headers(self):
        """同步到 mcp/config.json 的条目必须带认证头，否则远端 401。"""
        from connector_hub import connectors_service as svc
        inst = {
            "instance_id": "abc123",
            "kind": "tencent-docs",
            "name": "腾讯文档",
            "enabled": True,
            "creds_enc": svc.encrypt_credentials({"token": "T"}),
        }
        spec = svc.generate_mcp_spec(inst)
        assert spec["url"] == "https://docs.qq.com/openapi/mcp"
        assert spec["headers"]["Authorization"] == "T"

    def test_gateway_internal_mcp_url_has_v2_prefix(self):
        """gateway 的 internal MCP 必须指向真实挂载路径 /api/v2/connectors/internal/mcp。"""
        from connector_hub import connectors_service as svc
        inst = {
            "instance_id": "g1",
            "kind": "tencent-news",
            "name": "腾讯新闻",
            "enabled": True,
            "creds_enc": svc.encrypt_credentials(
                {"api_key": "k", "internal_token": "tok"}
            ),
        }
        spec = svc.generate_mcp_spec(inst)
        assert "/api/v2/connectors/internal/mcp/tencent-news/g1" in spec["url"]
        assert "token=tok" in spec["url"]


class TestCredentialNormalization:
    def test_api_key_accepts_legacy_token_field(self):
        """前端无 credential_fields 时兜底提交 token，需映射为 api_key。"""
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-news")
        out = svc.normalize_credentials("tencent-news", e, {"token": "abc"})
        assert out == {"api_key": "abc"}

    def test_personal_token_keeps_token(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-docs")
        out = svc.normalize_credentials("tencent-docs", e, {"token": "abc"})
        assert out == {"token": "abc"}

    def test_imap_maps_email_password(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("qq-mail")
        out = svc.normalize_credentials(
            "qq-mail", e, {"email": "a@qq.com", "password": "p"}
        )
        assert out["email"] == "a@qq.com" and out["password"] == "p"

    def test_empty_api_key_raises(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("tencent-news")
        with pytest.raises(ValueError):
            svc.normalize_credentials("tencent-news", e, {})

    def test_feishu_cli_keeps_app_fields(self):
        """飞书 CLI 归一化必须保留 app_id/app_secret，供适配器使用。"""
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("feishu-cli")
        out = svc.normalize_credentials(
            "feishu-cli", e,
            {"app_id": "cli_abc", "app_secret": "secret"}
        )
        assert out["app_id"] == "cli_abc"
        assert out["app_secret"] == "secret"
        assert "api_key" not in out

    def test_feishu_cli_missing_secret_raises(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("feishu-cli")
        with pytest.raises(ValueError):
            svc.normalize_credentials("feishu-cli", e, {"app_id": "cli_abc"})

    def test_wecom_cli_keeps_bot_fields(self):
        """企业微信 CLI 归一化必须保留 bot_id/bot_secret。"""
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("wecom-cli")
        out = svc.normalize_credentials(
            "wecom-cli", e,
            {"bot_id": "bot1", "bot_secret": "s3cret"}
        )
        assert out["bot_id"] == "bot1"
        assert out["bot_secret"] == "s3cret"
        assert "api_key" not in out

    def test_wecom_cli_missing_bot_raises(self):
        from connector_hub import connectors_service as svc, connectors_catalog as cat
        e = cat.get_catalog_entry("wecom-cli")
        with pytest.raises(ValueError):
            svc.normalize_credentials("wecom-cli", e, {"bot_id": "bot1"})


class TestInternalMcpRoute:
    def test_route_mounted_under_v2_prefix(self):
        from connector_hub.connectors_api import router
        paths = [r.path for r in router.routes]
        assert "/api/v2/connectors/internal/mcp/{kind}/{instance_id}" in paths

    def test_route_is_public_for_middleware(self):
        """internal MCP 由路由内部校验 token，必须在鉴权白名单内。"""
        import auth
        assert auth.is_public("/api/v2/connectors/internal/mcp/tencent-news/x")


class TestHttpMcpClientIntegration:
    """回归：HTTP 连接器工具 schema 与调用分发（此前两处致命缺陷）。"""

    def test_tool_schema_accepts_dict_form(self):
        """amap_list_tools 返回 dict，不能按 SDK 对象取 .name。"""
        from chat_client.mcp_client import _tool_schema_from_mcp
        schema = _tool_schema_from_mcp({
            "name": "search_news",
            "description": "Search news",
            "inputSchema": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "kw"}},
                "required": ["query"],
            },
        })
        assert schema["function"]["name"] == "search_news"
        assert schema["function"]["parameters"]["required"] == ["query"]
        assert schema["function"]["parameters"]["properties"]["query"]["description"] == "kw"

    def test_tool_schema_accepts_object_form(self):
        """SDK Tool 对象仍需兼容。"""
        from chat_client.mcp_client import _tool_schema_from_mcp

        class Tool:
            name = "t1"
            description = "d"
            input_schema = {"type": "object", "properties": {"a": {"type": "string"}}}

        schema = _tool_schema_from_mcp(Tool())
        assert schema["function"]["name"] == "t1"
        assert "a" in schema["function"]["parameters"]["properties"]

    def test_http_headers_are_forwarded(self):
        """HttpMcpClient 必须保存并发送自定义认证头。"""
        from connector_hub.http_mcp import HttpMcpClient
        c = HttpMcpClient("https://example.com/mcp", headers={"Authorization": "T"})
        assert c._headers == {"Authorization": "T"}

    def test_call_tool_routes_http_servers_without_session(self):
        """HTTP 连接器无 SDK session，call_tool 应走 amap_call 而非报未连接。"""
        from chat_client.mcp_client import MCPClient
        c = MCPClient()
        c._tool_servers["fake_tool"] = "fake_server"
        c._tool_servers_by_name["fake_server"] = {
            "url": "http://127.0.0.1:1/mcp",
            "token": None,
            "transport": "streamable_http",
            "headers": {},
        }
        out = c.call_tool("fake_tool", {})
        # 不应是 "未连接"，而是真实发起请求后的连接错误
        assert "未连接" not in out
        assert "调用异常" in out or "失败" in out


class TestEditKeepSecretAndConfiguredFields:
    """编辑场景：secret 留空表示不修改；返回 configured_fields 标记已配置字段。"""

    def _make(self, kind, creds):
        from connector_hub.connectors_service import create_instance
        return create_instance(kind, "test-" + kind, creds, True)

    def _cleanup(self, iid):
        from connector_hub.connectors_service import _instances, _save_instances
        _save_instances([i for i in _instances() if i["instance_id"] != iid])

    def test_configured_fields_marks_secret(self):
        from connector_hub.connectors_service import create_instance
        inst = create_instance("weknora", "cfg", {"base_url": "http://127.0.0.1:1", "api_key": "sk"}, True)
        try:
            assert "api_key" in inst.get("configured_fields", [])
            assert "base_url" not in inst.get("configured_fields", [])  # 非 secret 不标记
        finally:
            self._cleanup(inst["instance_id"])

    def test_update_keeps_secret_when_blank(self):
        from connector_hub.connectors_service import create_instance, update_instance, get_instance
        inst = create_instance("weknora", "k", {"base_url": "http://a:1", "api_key": "sk-old"}, True)
        iid = inst["instance_id"]
        try:
            upd = update_instance(iid, name="k2", creds={"base_url": "http://b:2"})
            assert "api_key" in upd.get("configured_fields", [])
            detail = get_instance(iid, include_secrets=True)
            assert detail["creds"].get("api_key") == "sk-old"
            assert detail["creds"].get("base_url") == "http://b:2"
        finally:
            self._cleanup(iid)

    def test_update_empty_creds_keeps_all(self):
        from connector_hub.connectors_service import create_instance, update_instance, get_instance
        inst = create_instance("qq-music", "m", {"api_key": "qmk-old"}, True)
        iid = inst["instance_id"]
        try:
            update_instance(iid, name="m2", creds={})
            detail = get_instance(iid, include_secrets=True)
            assert detail["creds"].get("api_key") == "qmk-old"
        finally:
            self._cleanup(iid)
