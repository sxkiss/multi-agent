"""平台扩展层测试（/home/sxkiss/bt/platform_ext）。

覆盖：teams / channels / storage / cli。
不触网、不依赖生产 config（用临时目录重定向）。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def isolated_cfg(tmp_path, monkeypatch):
    """把 config.json 重定向到临时目录。"""
    from platform_ext import storage as st
    from platform_ext import teams as tm
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(st, "CONFIG_FILE", str(cfg))
    monkeypatch.setattr(tm, "CONFIG_FILE", str(cfg))
    return cfg


# ── teams ──────────────────────────────────────────────────
class TestTeams:
    def test_create_and_list(self, isolated_cfg):
        from platform_ext import teams as tm
        t = tm.create_team("调研组", ["search", "coder"], description="调研+实现")
        assert t["name"] == "调研组"
        assert t["kind"] == "team"
        assert len(t["member_ids"]) == 2
        assert len(tm.list_teams()) == 1

    def test_min_members_enforced(self, isolated_cfg):
        from platform_ext import teams as tm
        with pytest.raises(ValueError):
            tm.create_team("单人", ["search"])

    def test_unknown_member_rejected(self, isolated_cfg):
        from platform_ext import teams as tm
        with pytest.raises(ValueError):
            tm.create_team("组", ["search", "不存在的成员"])

    def test_update_and_delete(self, isolated_cfg):
        from platform_ext import teams as tm
        t = tm.create_team("组", ["search", "coder"])
        tm.update_team(t["team_id"], name="新名", member_ids=["search", "planner"])
        assert tm.get_team(t["team_id"])["name"] == "新名"
        assert tm.delete_team(t["team_id"]) is True
        assert tm.get_team(t["team_id"]) is None

    def test_template(self, isolated_cfg):
        from platform_ext import teams as tm
        tpl = tm.team_template()
        assert "SOUL.md" in tpl["files"]
        assert tpl["min_members"] == 2

    def test_roster_text(self, isolated_cfg):
        from platform_ext import teams as tm
        t = tm.create_team("组", ["search", "coder"])
        txt = tm.team_roster_text(t["team_id"])
        assert "search" in txt and "coder" in txt


# ── channels ───────────────────────────────────────────────
class TestChannels:
    def test_register_and_list(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        c = ch.HttpChannel("test-http")
        reg.register(c)
        assert reg.get("test-http") is c
        assert any(x["channel_id"] == "test-http" for x in reg.list())

    def test_types_include_builtin(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        reg.register_builtin("miniprogram", lambda *a: None)
        assert "miniprogram" in reg.list_types()

    def test_dispatch_unknown_raises(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        import asyncio
        with pytest.raises(KeyError):
            asyncio.run(reg.dispatch_text("nope", "u1", "hi"))

    def test_dispatch_echo(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        reg.register(ch.HttpChannel("c1"))
        import asyncio
        # 默认处理器回 echo，不抛异常即通过
        asyncio.run(reg.dispatch_text("c1", "u1", "hello"))

    def test_by_type(self):
        from platform_ext import channels as ch
        reg = ch.ChannelRegistry()
        reg.register(ch.HttpChannel("h1"))
        reg.register(ch.WebSocketChannel("w1"))
        assert len(reg.get_by_type(ch.CHANNEL_HTTP)) == 1
        assert len(reg.get_by_type(ch.CHANNEL_WEBSOCKET)) == 1


# ── storage ────────────────────────────────────────────────
class TestStorage:
    def test_create_local(self, isolated_cfg):
        from platform_ext import storage as st
        b = st.create_backend("local", "本机")
        assert b["kind"] == "local"
        assert len(st.list_backends()) == 1

    def test_unsupported_kind(self, isolated_cfg):
        from platform_ext import storage as st
        with pytest.raises(ValueError):
            st.create_backend("s3", "x")

    def test_duplicate_name(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        with pytest.raises(ValueError):
            st.create_backend("local", "a")

    def test_path_traversal_blocked(self, isolated_cfg):
        from platform_ext import storage as st
        assert st.safe_resolve("../../etc/passwd") is None
        assert st.safe_resolve("a\x00b") is None

    def test_write_read_roundtrip(self, isolated_cfg, tmp_path, monkeypatch):
        from platform_ext import storage as st
        st.create_backend("filesystem", "fs", root=str(tmp_path / "ws"))
        assert st.write_text("sub/hello.txt", "内容") is True
        assert st.read_text("sub/hello.txt") == "内容"
        entries = st.list_dir("sub")
        assert any(e["name"] == "hello.txt" for e in entries)

    def test_update_toggle(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        st.update_backend("a", enabled=False)
        assert st.get_backend("a")["enabled"] is False

    def test_delete(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        assert st.delete_backend("a") is True
        assert st.get_backend("a") is None

    def test_probe_local(self, isolated_cfg):
        from platform_ext import storage as st
        st.create_backend("local", "a")
        res = st.probe_backend("a")
        assert res["ok"] is True

    def test_supported_kinds(self):
        from platform_ext import storage as st
        assert "local" in st.SUPPORTED_KINDS
        assert "docker" in st.SUPPORTED_KINDS


# ── cli ────────────────────────────────────────────────────
class TestCli:
    def test_parser_builds(self):
        from platform_ext import cli
        p = cli.build_parser()
        args = p.parse_args(["status"])
        assert args.cmd == "status"

    def test_client_url_join(self):
        from platform_ext import cli
        c = cli.Client("http://127.0.0.1:9876/")
        assert c.base_url == "http://127.0.0.1:9876"

    def test_cli_subcommands(self):
        from platform_ext import cli
        p = cli.build_parser()
        for cmd in (["models"], ["skills", "list"], ["experts", "list"], ["connectors", "catalog"], ["ask", "hi"]):
            assert p.parse_args(cmd).cmd == cmd[0]
