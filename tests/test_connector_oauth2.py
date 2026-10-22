"""OAuth2 连接器接入测试。

覆盖：provider 配置完整性、PKCE、token 存储与过期判定、
授权 URL 构造、回调 URL 动态推导（反向代理/远程访问）。

不触网：所有网络相关行为通过 monkeypatch 隔离。
"""

from __future__ import annotations

import pytest


# ── provider 配置 ────────────────────────────────────────────
class TestOAuthProviders:
    def test_five_providers_registered(self):
        from connector_hub.oauth_providers import list_providers
        kinds = {p.kind for p in list_providers()}
        assert kinds == {"notion", "tencent-ardot", "dida365", "openalex", "qcc"}

    def test_all_have_authorize_and_token_endpoint(self):
        """5 个厂商实测均发布完整 OAuth 发现，故均应可一键授权。"""
        from connector_hub.oauth_providers import get_provider
        for k in ("notion", "tencent-ardot", "dida365", "openalex", "qcc"):
            p = get_provider(k)
            assert p is not None, k
            assert p.authorization_endpoint, f"{k} 缺授权端点"
            assert p.token_endpoint, f"{k} 缺 token 端点"

    def test_all_support_dcr_and_s256(self):
        from connector_hub.oauth_providers import get_provider
        for k in ("notion", "tencent-ardot", "dida365", "openalex", "qcc"):
            p = get_provider(k)
            assert p.supports_dcr is True, k
            assert "S256" in (p.code_challenge_methods_supported or []), k

    def test_openalex_and_qcc_allow_manual_token(self):
        """自动授权之外的兜底路径应始终可用。"""
        from connector_hub.oauth_providers import get_provider
        assert get_provider("openalex").supports_manual_token is True
        assert get_provider("qcc").supports_manual_token is True

    def test_oauth2_entries_now_supported(self):
        from connector_hub import connectors_catalog as cat
        for k in ("notion", "tencent-ardot", "dida365", "openalex", "qcc"):
            assert cat.get_catalog_entry(k).supported is True, k


# ── PKCE ─────────────────────────────────────────────────────
class TestPKCE:
    def test_verifier_length_within_rfc7636(self):
        from connector_hub.oauth_manager import generate_code_verifier
        v = generate_code_verifier()
        assert 43 <= len(v) <= 128

    def test_challenge_is_s256_of_verifier(self):
        import base64
        import hashlib
        from connector_hub.oauth_manager import (
            generate_code_challenge,
            generate_code_verifier,
        )
        v = generate_code_verifier()
        got = generate_code_challenge(v)
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(v.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert got == expected
        assert "=" not in got  # base64url 无 padding

    def test_challenge_deterministic(self):
        from connector_hub.oauth_manager import generate_code_challenge
        assert generate_code_challenge("fixed-verifier-value") == \
               generate_code_challenge("fixed-verifier-value")


# ── 凭据存储与过期 ────────────────────────────────────────────
class TestOAuth2Credentials:
    def test_expiry_uses_60s_leeway(self):
        import time
        from connector_hub.oauth_manager import OAuth2Credentials
        c = OAuth2Credentials(kind="notion", access_token="t", expires_at=int(time.time()) + 30)
        assert c.is_expired() is True
        c2 = OAuth2Credentials(kind="notion", access_token="t", expires_at=int(time.time()) + 3600)
        assert c2.is_expired() is False

    def test_no_expiry_never_expires(self):
        from connector_hub.oauth_manager import OAuth2Credentials
        assert OAuth2Credentials(kind="notion", access_token="t").is_expired() is False

    def test_dict_roundtrip(self):
        from connector_hub.oauth_manager import OAuth2Credentials
        c = OAuth2Credentials(
            kind="openalex", access_token="a", refresh_token="r",
            expires_at=123, scope="openalex:query", client_id="cid",
        )
        d = c.to_dict()
        assert d["access_token"] == "a" and d["refresh_token"] == "r"
        back = OAuth2Credentials.from_dict(d)
        assert back.kind == "openalex" and back.client_id == "cid"


# ── 授权 URL 构造 ────────────────────────────────────────────
class TestAuthorizeURL:
    def test_contains_pkce_and_state(self):
        from connector_hub.oauth_manager import build_authorize_url
        url = build_authorize_url(
            kind="notion", client_id="cid123", redirect_uri="http://x/cb",
            code_verifier="verifier-value", state="st4te",
        )
        assert url.startswith("https://mcp.notion.com/authorize?")
        assert "code_challenge_method=S256" in url
        assert "client_id=cid123" in url
        assert "state=st4te" in url
        assert "response_type=code" in url

    def test_scope_uses_provider_scopes(self):
        from connector_hub.oauth_manager import build_authorize_url
        url = build_authorize_url(
            kind="dida365", client_id="c", redirect_uri="http://x/cb",
            code_verifier="v", state="s",
        )
        assert "tasks%3Aread" in url or "tasks:read" in url

    def test_unknown_provider_raises(self):
        from connector_hub.oauth_manager import build_authorize_url
        with pytest.raises(ValueError):
            build_authorize_url(kind="nope", client_id="c", redirect_uri="r",
                                code_verifier="v", state="s")


# ── 回调 URL 动态推导 ────────────────────────────────────────
class TestCallbackURL:
    def test_uses_request_host(self):
        from connector_hub.oauth_manager import build_callback_url

        class FakeURL:
            scheme = "https"
            port = None

        class FakeReq:
            url = FakeURL()
            headers = {"host": "panel.example.com", "x-forwarded-proto": "https"}

        url = build_callback_url(FakeReq())
        assert url == "https://panel.example.com/api/v2/connectors/oauth2/callback"

    def test_prefers_forwarded_host(self):
        from connector_hub.oauth_manager import build_callback_url

        class FakeURL:
            scheme = "http"
            port = None

        class FakeReq:
            url = FakeURL()
            headers = {
                "host": "127.0.0.1:9876",
                "x-forwarded-host": "bt.example.cn",
                "x-forwarded-proto": "https",
            }

        url = build_callback_url(FakeReq())
        assert url == "https://bt.example.cn/api/v2/connectors/oauth2/callback"

    def test_fallback_without_request(self):
        from connector_hub.oauth_manager import build_callback_url
        url = build_callback_url()
        assert url.endswith("/api/v2/connectors/oauth2/callback")
        assert url.startswith("http://localhost:")


# ── auth state 生命周期 ──────────────────────────────────────
class TestAuthState:
    def test_create_get_remove(self, monkeypatch):
        from connector_hub import oauth_manager as om
        store = {}
        monkeypatch.setattr(om, "_get_oauth2_section",
                            lambda: {"auth_states": store, "clients": {}, "token_cache": {}})
        monkeypatch.setattr(om, "_save_oauth2_section", lambda s: None)

        state, ast = om.create_auth_state("notion", "http://x/cb")
        assert om.get_auth_state(state) is not None
        assert om.get_auth_state(state).kind == "notion"
        om.remove_auth_state(state)
        assert om.get_auth_state(state) is None

    def test_unknown_state_returns_none(self, monkeypatch):
        from connector_hub import oauth_manager as om
        monkeypatch.setattr(om, "_get_oauth2_section",
                            lambda: {"auth_states": {}, "clients": {}, "token_cache": {}})
        assert om.get_auth_state("missing") is None


# ── 手动 token 兜底 ──────────────────────────────────────────
class TestManualToken:
    def test_manual_token_saved_and_readable(self, monkeypatch):
        from connector_hub import oauth_manager as om
        store = {}
        monkeypatch.setattr(om, "_get_oauth2_section",
                            lambda: {"auth_states": {}, "clients": {}, "token_cache": store})
        monkeypatch.setattr(om, "_save_oauth2_section", lambda s: None)

        om.save_manual_token("qcc", "tok-abc", "inst1")
        creds = om.get_credentials("qcc", "inst1")
        assert creds is not None
        assert creds.access_token == "tok-abc"
        assert creds.manual_token == "tok-abc"
        # 手动 token 无过期信息
        assert creds.expires_at is None
        assert creds.is_expired() is False

    def test_delete_credentials(self, monkeypatch):
        from connector_hub import oauth_manager as om
        store = {}
        monkeypatch.setattr(om, "_get_oauth2_section",
                            lambda: {"auth_states": {}, "clients": {}, "token_cache": store})
        monkeypatch.setattr(om, "_save_oauth2_section", lambda s: None)
        om.save_manual_token("qcc", "t", "i1")
        om.delete_credentials("i1")
        assert om.get_credentials("qcc", "i1") is None


# ── service 层集成 ───────────────────────────────────────────
class TestServiceIntegration:
    def test_normalize_accepts_manual_token(self):
        from connector_hub import connectors_service as svc
        from connector_hub import connectors_catalog as cat
        entry = cat.get_catalog_entry("openalex")
        out = svc.normalize_credentials("openalex", entry, {"manual_token": "tk"})
        assert out["access_token"] == "tk"

    def test_normalize_accepts_oauth_access_token(self):
        from connector_hub import connectors_service as svc
        from connector_hub import connectors_catalog as cat
        entry = cat.get_catalog_entry("notion")
        out = svc.normalize_credentials("notion", entry, {"access_token": "at"})
        assert out["access_token"] == "at"

    def test_normalize_rejects_empty(self):
        from connector_hub import connectors_service as svc
        from connector_hub import connectors_catalog as cat
        entry = cat.get_catalog_entry("notion")
        with pytest.raises(ValueError):
            svc.normalize_credentials("notion", entry, {})

    def test_oauth2_remote_spec_requires_token(self):
        from connector_hub import connectors_service as svc
        from connector_hub import connectors_catalog as cat
        entry = cat.get_catalog_entry("notion")
        # 无 token 时无法生成 remote spec
        assert svc._remote_spec_for("notion", {}, entry) is None

    def test_oauth2_remote_spec_builds_bearer_header(self):
        from connector_hub import connectors_service as svc
        from connector_hub import connectors_catalog as cat
        entry = cat.get_catalog_entry("notion")
        spec = svc._remote_spec_for("notion", {"access_token": "tk"}, entry)
        assert spec is not None
        url, headers, transport = spec
        assert url == "https://mcp.notion.com/mcp"
        assert headers["Authorization"] == "Bearer tk"