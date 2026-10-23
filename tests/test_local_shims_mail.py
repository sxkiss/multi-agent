"""测试 connector_hub/gateway_adapters/local_shims.py 的邮箱相关函数。

覆盖：
- preferred_netease_imap_host / preferred_netease_smtp_host
- correct_netease_imap_host / correct_netease_smtp_host
- resolve_mail_servers 边界情况
"""

import pytest

from connector_hub.gateway_adapters.local_shims import (
    DEFAULT_IMAP_HOST,
    DEFAULT_SMTP_HOST,
    NETEASE_IMAP_HOSTS,
    NETEASE_SMTP_HOSTS,
    correct_netease_imap_host,
    correct_netease_smtp_host,
    preferred_netease_imap_host,
    preferred_netease_smtp_host,
    resolve_mail_servers,
)


class TestPreferredNeteaseHosts:
    """preferred_* 辅助函数测试。"""

    @pytest.mark.parametrize("email,expected", [
        ("user@163.com", "imap.163.com"),
        ("USER@126.COM", "imap.126.com"),
        ("a@yeah.net", "imap.yeah.net"),
        ("user@qq.com", None),
        ("user@gmail.com", None),
        ("", None),
        ("invalid", None),
    ])
    def test_preferred_netease_imap_host(self, email, expected):
        assert preferred_netease_imap_host(email) == expected

    @pytest.mark.parametrize("email,expected", [
        ("user@163.com", "smtp.163.com"),
        ("USER@126.COM", "smtp.126.com"),
        ("a@yeah.net", "smtp.yeah.net"),
        ("user@qq.com", None),
        ("user@gmail.com", None),
        ("", None),
        ("invalid", None),
    ])
    def test_preferred_netease_smtp_host(self, email, expected):
        assert preferred_netease_smtp_host(email) == expected


class TestCorrectNeteaseHosts:
    """correct_netease_* 函数测试。"""

    def test_correct_imap_uses_preferred_for_empty(self):
        assert correct_netease_imap_host("user@163.com", None) == "imap.163.com"
        assert correct_netease_imap_host("user@163.com", "") == "imap.163.com"

    def test_correct_smtp_uses_preferred_for_empty(self):
        assert correct_netease_smtp_host("user@163.com", None) == "smtp.163.com"
        assert correct_netease_smtp_host("user@163.com", "") == "smtp.163.com"

    def test_correct_imap_preserves_custom_host(self):
        # 自定义 host 不在 NETEASE_IMAP_HOSTS 中时保留
        assert correct_netease_imap_host("user@163.com", "custom.example.com") == "custom.example.com"

    def test_correct_smtp_preserves_custom_host(self):
        assert correct_netease_smtp_host("user@163.com", "custom.example.com") == "custom.example.com"

    def test_correct_imap_normalizes_known_netase(self):
        # 已知网易 host 被规范化为 preferred
        assert correct_netease_imap_host("user@163.com", "imap.126.com") == "imap.163.com"
        assert correct_netease_imap_host("user@126.com", "imap.163.com") == "imap.126.com"

    def test_correct_smtp_normalizes_known_netase(self):
        assert correct_netease_smtp_host("user@163.com", "smtp.126.com") == "smtp.163.com"
        assert correct_netease_smtp_host("user@126.com", "smtp.163.com") == "smtp.126.com"

    def test_correct_non_netase_returns_default(self):
        assert correct_netease_imap_host("user@qq.com", None) == DEFAULT_IMAP_HOST
        assert correct_netease_smtp_host("user@qq.com", None) == DEFAULT_SMTP_HOST


class TestResolveMailServers:
    """resolve_mail_servers 主路径测试。"""

    @pytest.mark.parametrize("email,expected_imap,expected_smtp", [
        ("user@163.com", "imap.163.com", "smtp.163.com"),
        ("user@126.com", "imap.126.com", "smtp.126.com"),
        ("user@yeah.net", "imap.yeah.net", "smtp.yeah.net"),
        ("user@qq.com", "imap.qq.com", "smtp.qq.com"),
        ("user@gmail.com", "imap.gmail.com", "smtp.gmail.com"),
    ])
    def test_resolve_by_domain(self, email, expected_imap, expected_smtp):
        creds = {"email": email}
        result = resolve_mail_servers(creds)
        assert result[0] == expected_imap
        assert result[2] == expected_smtp

    def test_resolve_by_provider_netease(self):
        creds = {"mail_provider": "netease", "email": "user@example.com"}
        result = resolve_mail_servers(creds)
        # netease 默认走 163
        assert result[0] == "imap.163.com"
        assert result[2] == "smtp.163.com"

    def test_resolve_by_provider_qq(self):
        creds = {"mail_provider": "qq", "email": "user@example.com"}
        result = resolve_mail_servers(creds)
        assert result[0] == "imap.qq.com"
        assert result[2] == "smtp.qq.com"

    def test_resolve_fallback_to_manual_hosts(self):
        creds = {
            "email": "user@other.com",
            "imap_host": "imap.custom.com",
            "imap_port": 993,
            "smtp_host": "smtp.custom.com",
            "smtp_port": 587,
        }
        result = resolve_mail_servers(creds)
        assert result == ("imap.custom.com", 993, "smtp.custom.com", 587)

    def test_resolve_empty_credentials(self):
        creds = {}
        result = resolve_mail_servers(creds)
        assert result == (DEFAULT_IMAP_HOST, 993, DEFAULT_SMTP_HOST, 587)

    def test_netease_imap_hosts_set_is_consistent(self):
        # 验证 frozenset 包含所有网易 IMAP host
        assert NETEASE_IMAP_HOSTS == {"imap.163.com", "imap.126.com", "imap.yeah.net"}
        assert NETEASE_SMTP_HOSTS == {"smtp.163.com", "smtp.126.com", "smtp.yeah.net"}
