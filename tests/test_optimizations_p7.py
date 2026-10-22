"""bt P7 优化项（2026-10-15）单元测试：安全/能力增强。

覆盖：
- guard_rules：YAML 护栏规则库（加载/校验/扫描/热加载/穿越拒绝）
- hitl_store：审批闭环（pending/TTL/批准拒绝）
- hitl 硬保护：AskUserQuestion 永不跳过 + 白名单校验
- conversation_mode：Ask/Plan/Craft 工具裁剪
- slash：斜杠命令解析/分发
- skill_package：技能打包/导入（含穿越防护）
- kb_jobs：索引任务队列
- inbound_media：入站媒体落盘
- response_mode：响应模式 + 思考清洗
- session_tools：7 段上下文分解

运行：python3 -m pytest tests/test_optimizations_p7.py -q
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestGuardRules:
    def test_seed_and_scan(self, tmp_path):
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(str(tmp_path))
        assert os.path.isfile(gr.path)
        assert len(gr.rules) >= 5
        blocked, rule = gr.scan("rm -rf /")
        assert blocked and rule["id"] == "rm-rf-root"

    def test_normal_cmd_not_blocked(self, tmp_path):
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(str(tmp_path))
        blocked, _ = gr.scan("ls -la /home/sxkiss")
        assert not blocked

    def test_custom_rule_hotload(self, tmp_path):
        import yaml
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(str(tmp_path))
        doc = yaml.safe_load(open(gr.path, encoding="utf-8"))
        doc["rules"].append({"id": "custom", "pattern": r"curl\s+evil\.com",
                             "severity": "high", "action": "block", "description": "自定义"})
        yaml.safe_dump(doc, open(gr.path, "w", encoding="utf-8"), allow_unicode=True)
        gr2 = get_guard_rules(str(tmp_path))
        blocked, rule = gr2.scan("curl evil.com/x")
        assert blocked and rule["id"] == "custom"

    def test_invalid_yaml_fallback(self, tmp_path):
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(str(tmp_path))
        open(gr.path, "w", encoding="utf-8").write("::: not yaml [[[")
        gr._load(force=True)
        assert len(gr.rules) >= 5  # 回退默认

    def test_warn_rule_not_blocked(self, tmp_path):
        from chat_client.guard_rules import get_guard_rules
        gr = get_guard_rules(str(tmp_path))
        blocked, _ = gr.scan("history -c")  # 内置 warn 级
        assert not blocked


class TestHitlHardening:
    def test_ask_user_question_never_allowed(self):
        from chat_client.tools import set_session_hitl, is_high_allowed
        import chat_client.tools as T
        set_session_hitl("sx", "allow_all")
        T._current_job_local.job = type("J", (), {"session_id": "sx"})()
        try:
            assert not is_high_allowed("AskUserQuestion")
            assert is_high_allowed("Bash")
        finally:
            T._current_job_local.job = None

    def test_whitelist_filtering(self):
        from chat_client.tools import set_session_hitl, get_session_hitl
        set_session_hitl("sy", "allow_tools", ["Read", "AskUserQuestion", "x" * 200] + [f"t{i}" for i in range(100)])
        cfg = get_session_hitl("sy")
        assert "AskUserQuestion" not in cfg["tools"]
        assert len(cfg["tools"]) <= 64


class TestHitlStore:
    def test_request_decide(self, tmp_path):
        from chat_client.hitl_store import get_hitl_store
        st = get_hitl_store(str(tmp_path))
        r = st.request(session_id="s1", tool="Bash")
        assert r["status"] == "pending"
        assert len(st.list_pending("s1")) == 1
        assert st.decide(r["id"], True)["status"] == "approved"
        assert st.is_approved(r["id"])

    def test_ttl_expire(self, tmp_path):
        from chat_client.hitl_store import get_hitl_store
        st = get_hitl_store(str(tmp_path))
        r = st.request(session_id="s1", tool="Bash", ttl=-1)
        assert st.get(r["id"])["status"] == "expired"


class TestConversationMode:
    def test_filter(self):
        from chat_client.conversation_mode import filter_tools_for_mode
        tools = ["Read", "Write", "Edit", "Bash", "Task", "Grep", "TodoWrite"]
        assert filter_tools_for_mode(tools, "craft") == tools
        assert filter_tools_for_mode(tools, "ask") == ["Read", "Grep"]
        assert filter_tools_for_mode(tools, "plan") == ["Read", "Grep", "TodoWrite"]

    def test_resolve(self):
        from chat_client.conversation_mode import resolve_mode
        assert resolve_mode({}) == "craft"
        assert resolve_mode({"conversation_mode": "ask"}) == "ask"
        assert resolve_mode({"conversation_mode": "ask"}, "plan") == "plan"


class TestSlash:
    def test_parse_and_dispatch(self):
        from chat_client.slash import get_slash_catalog
        cat = get_slash_catalog()
        assert cat.parse("/help") == ("help", [])
        assert cat.parse("/h") == ("help", [])  # 别名
        assert cat.parse("你好") is None
        res = cat.dispatch("/tools")
        assert res["status"] is True

    def test_unknown_command(self):
        from chat_client.slash import get_slash_catalog
        res = get_slash_catalog().dispatch("/nope")
        assert res["status"] is False


class TestSkillPackage:
    def test_export_import(self, tmp_path):
        from chat_client.skill_package import export_skill_package, import_skill_package
        base = str(tmp_path / "a"); os.makedirs(os.path.join(base, "skills", "demo"))
        open(os.path.join(base, "skills", "demo", "SKILL.md"), "w").write("# Demo")
        r = export_skill_package(base, ["demo"])
        assert r["ok"]
        base2 = str(tmp_path / "b")
        r2 = import_skill_package(base2, r["path"])
        assert r2["ok"] and "demo" in r2["installed"]
        assert os.path.isfile(os.path.join(base2, "skills", "demo", "SKILL.md"))

    def test_zip_traversal_rejected(self, tmp_path):
        import zipfile
        from chat_client.skill_package import import_skill_package
        z = str(tmp_path / "evil.zip")
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("../../evil/SKILL.md", "x")
        r = import_skill_package(str(tmp_path), z)
        assert not r["ok"]


class TestKbJobs:
    def test_submit_and_status(self, tmp_path):
        from chat_client.kb_jobs import IndexJobQueue
        done = []
        q = IndexJobQueue(worker=lambda job: done.append(job["id"]))
        jid = q.submit(kb=None, text="hi")
        time.sleep(0.5)
        assert q.status(jid)["status"] == "done"
        assert q.stats()["total"] == 1


class TestInboundMedia:
    def test_save(self, tmp_path):
        from chat_client.inbound_media import save_inbound_media
        r = save_inbound_media(b"\x89PNG" + b"x" * 50, base_dir=str(tmp_path),
                               session_id="single:assistant:x", ext=".png")
        assert r["ok"] and os.path.isfile(r["path"])

    def test_bad_ext(self, tmp_path):
        from chat_client.inbound_media import save_inbound_media
        r = save_inbound_media(b"data", base_dir=str(tmp_path), session_id="s", ext=".exe")
        assert not r["ok"]


class TestResponseMode:
    def test_resolve(self):
        from chat_client.response_mode import resolve_response_mode
        assert resolve_response_mode({}) == "stream"
        assert resolve_response_mode({"channel_response_modes": {"wechat": "invoke"}}, "wechat") == "invoke"

    def test_strip_thinking(self):
        from chat_client.response_mode import strip_thinking
        assert strip_thinking("<thinking>abc</thinking>你好") == "你好"
        assert strip_thinking("正文<thinking>半截") == "正文"
        assert strip_thinking("正常回答") == "正常回答"


class TestContextBreakdown:
    def test_seven_segments(self, tmp_path):
        from chat_client.session_tools import analyze_context
        # 造一个会话
        d = tmp_path / "sessions" / "s1"
        d.mkdir(parents=True)
        (d / "sessions.json").write_text(json.dumps([
            {"role": "user", "content": "hi"},
            {"role": "tool", "content": "<tool_result>x</tool_result>"},
        ]), encoding="utf-8")
        r = analyze_context(str(tmp_path), "s1", system_prompt="s" * 100,
                            tool_definitions="t" * 200, skills="k" * 50)
        keys = set(r["segments_dict"].keys())
        for k in ("system_prompt", "tool_definitions", "rules", "skills", "mcp",
                  "subagent_definitions", "conversation", "tool_results"):
            assert k in keys
        assert r["segments_dict"]["system_prompt"] > 0
