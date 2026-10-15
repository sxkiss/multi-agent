"""bt 优化项（2026-10-15）单元测试：

- P0 InstallExpert：专家市场落地为 crew 成员（mock 网络，不真实请求）
- P1 工具下发裁剪：CRITICAL_TOOLS / get_gated_tools / disclosure_mode
- P1 per-thread HITL：set/get_session_hitl / is_high_allowed 优先级

运行：python3 -m pytest tests/test_optimizations.py -q
"""
import importlib
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# P1: 工具下发裁剪
# ============================================================
class TestToolDisclosure:
    def test_critical_tools_never_gated(self):
        from chat_client.tools import CRITICAL_TOOLS, get_gated_tools
        # 门控全部组，核心工具仍不应出现在 gated 里
        gated = get_gated_tools(enabled_groups=set(), extra=set(CRITICAL_TOOLS))
        assert not (gated & CRITICAL_TOOLS), f"核心工具被误门控: {gated & CRITICAL_TOOLS}"

    def test_default_gates_all_groups(self):
        from chat_client.tools import GATED_TOOL_GROUPS, get_gated_tools
        gated = get_gated_tools()
        # 未启用任何组时，所有组内非核心工具都应被门控
        for grp, tools in GATED_TOOL_GROUPS.items():
            assert tools <= gated, f"组 {grp} 未完全门控"

    def test_enabled_group_not_gated(self):
        from chat_client.tools import GATED_TOOL_GROUPS, get_gated_tools
        gated = get_gated_tools(enabled_groups={"market"})
        assert not (GATED_TOOL_GROUPS["market"] & gated), "已启用组仍被门控"
        # 其他组仍门控
        assert GATED_TOOL_GROUPS["database"] <= gated

    def test_disclosure_mode_default_full(self, monkeypatch):
        from chat_client.tools import disclosure_mode
        monkeypatch.delenv("AI_AGENT_TOOL_DISCLOSURE", raising=False)
        assert disclosure_mode() == "full"
        monkeypatch.setenv("AI_AGENT_TOOL_DISCLOSURE", "trim")
        assert disclosure_mode() == "trim"

    def test_extra_gate(self):
        from chat_client.tools import get_gated_tools
        gated = get_gated_tools(enabled_groups=set(), extra={"SomeTool"})
        assert "SomeTool" in gated


# ============================================================
# P1: per-thread HITL
# ============================================================
class TestHitl:
    def test_default_is_ask(self):
        from chat_client.tools import get_session_hitl
        assert get_session_hitl("no_such_session")["mode"] == "ask"

    def test_set_allow_all(self):
        from chat_client.tools import set_session_hitl, get_session_hitl
        set_session_hitl("s_hitl_1", "allow_all")
        assert get_session_hitl("s_hitl_1")["mode"] == "allow_all"
        # 清理
        set_session_hitl("s_hitl_1", "ask")

    def test_set_allow_tools(self):
        from chat_client.tools import set_session_hitl, get_session_hitl
        set_session_hitl("s_hitl_2", "allow_tools", ["Bash", "Write"])
        cfg = get_session_hitl("s_hitl_2")
        assert cfg["mode"] == "allow_tools"
        assert cfg["tools"] == {"Bash", "Write"}
        set_session_hitl("s_hitl_2", "ask")

    def test_invalid_mode_falls_back_ask(self):
        from chat_client.tools import set_session_hitl, get_session_hitl
        set_session_hitl("s_hitl_3", "bogus_mode")
        assert get_session_hitl("s_hitl_3")["mode"] == "ask"

    def test_ask_clears_entry(self):
        from chat_client.tools import set_session_hitl, get_session_hitl
        set_session_hitl("s_hitl_4", "allow_all")
        set_session_hitl("s_hitl_4", "ask")
        assert get_session_hitl("s_hitl_4")["mode"] == "ask"

    def test_is_high_allowed_priority(self, monkeypatch):
        """HITL allow_all 应放行（即使基线 is_allow_high=False）。"""
        from chat_client.tools import set_session_hitl
        import chat_client.tools as T

        # mock 当前 session 上下文
        class _Job:
            session_id = "s_hitl_prio"
        monkeypatch.setattr(T, "_current_session_id", lambda: "s_hitl_prio")
        monkeypatch.setattr(T, "is_allow_high", lambda: False)

        set_session_hitl("s_hitl_prio", "allow_all")
        assert T.is_high_allowed("Bash") is True

        set_session_hitl("s_hitl_prio", "allow_tools", ["Bash"])
        assert T.is_high_allowed("Bash") is True
        assert T.is_high_allowed("Write") is False

        set_session_hitl("s_hitl_prio", "ask")
        assert T.is_high_allowed("Bash") is False  # 回落到基线 False


# ============================================================
# P0: InstallExpert（mock 网络）
# ============================================================
class TestInstallExpert:
    def _patch_market(self, monkeypatch, prompt_text="<expert>你是营销专家</expert>",
                      meta=None):
        from chat_client.tools import market as M
        monkeypatch.setattr(M, "fetch_expert_prompt",
                            lambda name: (name, prompt_text))
        monkeypatch.setattr(M, "find_expert_meta",
                            lambda name: (meta or {}))

    def test_install_creates_member_and_dept(self, monkeypatch, tmp_path):
        """InstallExpert 应写入 crew_agents.json 并自动建 experts 部门。"""
        from chat_client.tools import task as TK
        from chat_client.tools import market as M

        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)
        self._patch_market(monkeypatch, meta={"displayName": "营销专家", "description": "营销"})

        out = M.InstallExpert(name="marketing-expert")
        assert "已安装为团队成员" in out, out
        assert "marketing-expert" in out

        data = json.load(open(crew_file, encoding="utf-8"))
        names = [a["name"] for a in data["agents"]]
        assert "marketing-expert" in names
        assert any(d["name"] == "experts" for d in data["departments"])
        member = next(a for a in data["agents"] if a["name"] == "marketing-expert")
        assert member["expert_source"] == "marketing-expert"
        assert "营销专家" in member["backstory"]

    def test_install_with_identity_and_custom_name(self, monkeypatch, tmp_path):
        from chat_client.tools import task as TK
        from chat_client.tools import market as M

        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)
        self._patch_market(monkeypatch)

        out = M.InstallExpert(name="content-writer", member_name="my-writer",
                              identity="名字：小笔；风格：简洁")
        assert "my-writer" in out
        data = json.load(open(crew_file, encoding="utf-8"))
        member = next(a for a in data["agents"] if a["name"] == "my-writer")
        assert "小笔" in member["backstory"]
        assert member["identity"].startswith("名字")

    def test_install_duplicate_rejected(self, monkeypatch, tmp_path):
        from chat_client.tools import task as TK
        from chat_client.tools import market as M

        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)
        self._patch_market(monkeypatch)

        M.InstallExpert(name="dup-expert")
        out2 = M.InstallExpert(name="dup-expert")
        assert "已存在" in out2

    def test_install_bad_member_name(self, monkeypatch, tmp_path):
        from chat_client.tools import task as TK
        from chat_client.tools import market as M

        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)
        self._patch_market(monkeypatch)

        # member_name 全是非法字符 → 规范化后为空 → 报错
        out = M.InstallExpert(name="some-expert", member_name="中文名")
        assert "不合法" in out

    def test_install_network_failure(self, monkeypatch, tmp_path):
        from chat_client.tools import task as TK
        from chat_client.tools import market as M

        crew_file = str(tmp_path / "crew_agents.json")
        monkeypatch.setattr(TK.AgentRegistry, "CUSTOM_AGENTS_FILE", crew_file)

        def _boom(name):
            raise RuntimeError("HTTP 404")
        monkeypatch.setattr(M, "fetch_expert_prompt", _boom)
        out = M.InstallExpert(name="ghost-expert")
        assert "获取专家提示词失败" in out


# ============================================================
# 回归：工具注册表完整性
# ============================================================
class TestRegistryIntegrity:
    def test_install_expert_registered(self):
        from chat_client.tools import registry
        assert "InstallExpert" in registry._metadata
        assert registry._metadata["InstallExpert"]["risk_level"] == "medium"

    def test_all_gated_tools_exist_or_ignored(self):
        """门控清单里的工具名若不存在也不应报错（只是不生效）。"""
        from chat_client.tools import GATED_TOOL_GROUPS, registry
        known = set(registry._metadata.keys())
        for grp, tools in GATED_TOOL_GROUPS.items():
            # 仅断言不抛异常；存在性由实际注册决定
            assert isinstance(tools, frozenset)
