"""bt P4 优化项（2026-10-15）单元测试：

- P4-a 上下文用量分解：analyze_context
- P4-b 会话导出：export_session
- P4-c 对话分支：fork_session
- P4-d 备份：create_backup / list_backups / restore_backup / prune
- 会话统计：list_session_stats

运行：python3 -m pytest tests/test_optimizations_p4.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_session(base, sid, msgs):
    d = os.path.join(base, "sessions", sid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "sessions.json"), "w", encoding="utf-8") as f:
        json.dump(msgs, f, ensure_ascii=False)
    return d


# ============================================================
# P4-a: 上下文用量分解
# ============================================================
class TestContextBreakdown:
    def test_analyze_basic(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "s1", [
            {"id": "m1", "role": "user", "content": "你好，帮我看看这个"},
            {"id": "m2", "role": "assistant", "content": "好的，我来处理。" * 20},
            {"id": "m3", "role": "assistant", "content": "<tool_result>工具返回结果</tool_result>"},
        ])
        res = session_tools.analyze_context(base, "s1", context_window_tokens=1000)
        assert res["status"] is True
        assert res["messages"] == 3
        assert res["total_tokens"] > 0
        keys = {s["key"] for s in res["segments"]}
        assert "conversation" in keys
        assert "tool_results" in keys

    def test_empty_session(self, tmp_path):
        from chat_client import session_tools
        res = session_tools.analyze_context(str(tmp_path), "nope")
        assert res["status"] is True
        assert res["total_tokens"] == 0
        assert res["segments"] == []

    def test_usage_ratio(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "s2", [{"id": "m1", "role": "user", "content": "x" * 2000}])
        res = session_tools.analyze_context(base, "s2", context_window_tokens=1000)
        assert res["usage_ratio"] > 0


# ============================================================
# P4-b: 会话导出
# ============================================================
class TestSessionExport:
    def test_export_markdown(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "s1", [
            {"id": "m1", "role": "user", "content": "问题"},
            {"id": "m2", "role": "assistant", "content": "回答"},
        ])
        res = session_tools.export_session(base, "s1", fmt="markdown")
        assert res["status"] is True
        assert "# 会话导出" in res["content"]
        assert "问题" in res["content"] and "回答" in res["content"]
        assert "🧑 用户" in res["content"]

    def test_export_json(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "s1", [{"id": "m1", "role": "user", "content": "hi"}])
        res = session_tools.export_session(base, "s1", fmt="json")
        assert res["status"] is True
        parsed = json.loads(res["content"])
        assert parsed[0]["content"] == "hi"

    def test_export_missing(self, tmp_path):
        from chat_client import session_tools
        res = session_tools.export_session(str(tmp_path), "ghost")
        assert res["status"] is False


# ============================================================
# P4-c: 对话分支
# ============================================================
class TestFork:
    def test_fork_all(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "src", [
            {"id": "m1", "role": "user", "content": "q1"},
            {"id": "m2", "role": "assistant", "content": "a1"},
        ])
        res = session_tools.fork_session(base, "src", new_session_id="fork1")
        assert res["status"] is True
        assert res["copied"] == 2
        # 原会话不变
        assert len(session_tools._load_session_json(base, "src")) == 2
        # 新会话内容一致
        forked = session_tools._load_session_json(base, "fork1")
        assert forked[0]["content"] == "q1"
        # meta 记录来源
        meta = json.load(open(os.path.join(base, "sessions", "fork1", "meta.json"), encoding="utf-8"))
        assert meta["forked_from"] == "src"

    def test_fork_up_to_message(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "src", [
            {"id": "m1", "role": "user", "content": "q1"},
            {"id": "m2", "role": "assistant", "content": "a1"},
            {"id": "m3", "role": "user", "content": "q2"},
        ])
        res = session_tools.fork_session(base, "src", up_to_message_id="m2", new_session_id="fork2")
        assert res["status"] is True
        assert res["copied"] == 2
        forked = session_tools._load_session_json(base, "fork2")
        assert len(forked) == 2
        assert forked[-1]["id"] == "m2"

    def test_fork_bad_message_id(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "src", [{"id": "m1", "role": "user", "content": "q"}])
        res = session_tools.fork_session(base, "src", up_to_message_id="nope")
        assert res["status"] is False

    def test_fork_duplicate_target(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "src", [{"id": "m1", "role": "user", "content": "q"}])
        session_tools.fork_session(base, "src", new_session_id="dup")
        res = session_tools.fork_session(base, "src", new_session_id="dup")
        assert res["status"] is False


# ============================================================
# P4-d: 备份
# ============================================================
class TestBackup:
    def _seed(self, base):
        with open(os.path.join(base, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"k": "v"}, f)
        _mk_session(base, "s1", [{"id": "m1", "role": "user", "content": "hi"}])

    def test_create_and_list(self, tmp_path):
        from chat_client import backup
        base = str(tmp_path)
        self._seed(base)
        res = backup.create_backup(base, label="test")
        assert res["status"] is True, res
        assert os.path.exists(res["path"])
        lst = backup.list_backups(base)
        assert lst["total"] >= 1
        assert any(b["name"] == res["name"] for b in lst["backups"])

    def test_restore_dry_run(self, tmp_path):
        from chat_client import backup
        base = str(tmp_path)
        self._seed(base)
        res = backup.create_backup(base, label="test")
        dr = backup.restore_backup(base, res["name"], dry_run=True)
        assert dr["status"] is True
        assert dr["dry_run"] is True
        assert any("config.json" in m for m in dr["would_restore"])

    def test_restore_real(self, tmp_path):
        from chat_client import backup
        base = str(tmp_path)
        self._seed(base)
        res = backup.create_backup(base, label="test")
        # 破坏配置后恢复
        with open(os.path.join(base, "config.json"), "w", encoding="utf-8") as f:
            json.dump({"k": "BROKEN"}, f)
        rr = backup.restore_backup(base, res["name"], dry_run=False)
        assert rr["status"] is True, rr
        restored = json.load(open(os.path.join(base, "config.json"), encoding="utf-8"))
        assert restored["k"] == "v"

    def test_prune_keeps_recent(self, tmp_path):
        from chat_client import backup
        base = str(tmp_path)
        self._seed(base)
        for i in range(5):
            backup.create_backup(base, label=f"b{i}", keep=3)
        lst = backup.list_backups(base)
        assert lst["total"] <= 3

    def test_restore_missing(self, tmp_path):
        from chat_client import backup
        res = backup.restore_backup(str(tmp_path), "nope.tar.gz")
        assert res["status"] is False


# ============================================================
# 会话统计
# ============================================================
class TestSessionStats:
    def test_list_stats(self, tmp_path):
        from chat_client import session_tools
        base = str(tmp_path)
        _mk_session(base, "s1", [{"id": "m1", "role": "user", "content": "hello"}])
        _mk_session(base, "s2", [{"id": "m2", "role": "user", "content": "world!!!"}])
        res = session_tools.list_session_stats(base)
        assert res["status"] is True
        assert res["total"] == 2
        assert all("est_tokens" in s for s in res["sessions"])
