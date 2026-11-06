"""TodoWrite / TodoItem.from_dict 脏数据健壮性测试。

线上 bug（2026-11-06）：模型传入缺 content 的 todo 条目，
TodoItem.from_dict 硬取 data["content"] 抛 KeyError，冒泡到 Agent.chat 拖垮整轮对话。

核心契约：工具永不因模型脏数据抛异常；非法条目降级跳过 + 返回可读提示；
合法条目照常写入；正常批次行为不变。
"""
import json

import pytest

from chat_client.tools.todo import (
    TodoItem,
    TodoManager,
    TodoPriority,
    TodoRead,
    TodoStatus,
    TodoWrite,
)


def _kw(tmp_path, session_id="todo-test-session"):
    """统一的会话上下文参数（sessions_dir 用 tmp_path 隔离，不污染真实 sessions/）。"""
    return {"session_id": session_id, "sessions_dir": str(tmp_path)}


def _read_back(tmp_path, session_id="todo-test-session"):
    """用独立 manager 从磁盘读回，验证真实落盘结果。"""
    return [t.to_dict() for t in TodoManager(session_id, str(tmp_path)).get_todos()]


# ── from_dict 单元级校验 ──────────────────────────────────────────────

class TestFromDict:
    def test_missing_content_raises_valueerror(self):
        with pytest.raises(ValueError, match="content"):
            TodoItem.from_dict({"id": "1", "status": "in_progress"})

    @pytest.mark.parametrize("bad", ["", "   ", None, 123, [], {}])
    def test_empty_or_nonstring_content_raises(self, bad):
        with pytest.raises(ValueError):
            TodoItem.from_dict({"id": "1", "content": bad})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError):
            TodoItem.from_dict("not a dict")

    def test_invalid_status_priority_fallback(self):
        item = TodoItem.from_dict({
            "content": "任务", "status": "???", "priority": "ULTRA"
        })
        assert item.status is TodoStatus.PENDING
        assert item.priority is TodoPriority.MEDIUM

    def test_missing_status_priority_use_defaults(self):
        item = TodoItem.from_dict({"content": "任务"})
        assert item.status is TodoStatus.PENDING
        assert item.priority is TodoPriority.MEDIUM

    def test_valid_enums_preserved(self):
        item = TodoItem.from_dict({
            "content": "任务", "status": "completed", "priority": "high"
        })
        assert item.status is TodoStatus.COMPLETED
        assert item.priority is TodoPriority.HIGH

    def test_enum_object_compatible(self):
        """merge 分支会传枚举对象，_coerce_enum 必须兼容（str Enum 直接构造）。"""
        item = TodoItem.from_dict({
            "content": "任务",
            "status": TodoStatus.IN_PROGRESS,
            "priority": TodoPriority.LOW,
        })
        assert item.status is TodoStatus.IN_PROGRESS
        assert item.priority is TodoPriority.LOW

    def test_to_dict_structure_unchanged(self):
        item = TodoItem.from_dict({
            "id": "42", "content": "任务", "status": "pending", "priority": "low"
        })
        d = item.to_dict()
        assert set(d) == {"id", "content", "status", "priority"}
        assert d["id"] == "42" and d["content"] == "任务"


# ── TodoWrite：原 bug 复现 + 混合批次 ──────────────────────────────────

class TestTodoWriteDirtyData:
    def test_original_bug_missing_content_no_exception(self, tmp_path):
        """原 bug 复现：{'id':'1','status':'in_progress'} 无 content，返回字符串且无异常。"""
        ret = TodoWrite(
            todos=[{"id": "1", "status": "in_progress"}], **_kw(tmp_path)
        )
        assert isinstance(ret, str)
        assert "跳过" in ret and "缺少 content" in ret

    def test_mixed_batch_legal_written_dirty_skipped(self, tmp_path):
        """merge=False：合法条目写入、脏条目跳过，提示里写明数量。"""
        ret = TodoWrite(todos=[
            {"id": "1", "content": "合法任务"},
            {"id": "2", "status": "in_progress"},  # 缺 content
        ], **_kw(tmp_path))
        assert "已写入 1 条" in ret and "跳过 1 条" in ret
        rows = _read_back(tmp_path)
        assert len(rows) == 1 and rows[0]["content"] == "合法任务"

    def test_all_dirty_no_empty_todo_created(self, tmp_path):
        ret = TodoWrite(todos=[{"id": "1"}], **_kw(tmp_path))
        assert "跳过 1 条" in ret
        assert _read_back(tmp_path) == []  # 不造无意义的空 todo

    def test_invalid_status_priority_fallback_no_crash(self, tmp_path):
        """非法枚举字符串不崩溃，落盘回退到默认 pending/medium。"""
        ret = TodoWrite(todos=[{
            "id": "1", "content": "枚举任务",
            "status": "WAT", "priority": "ULTRA",
        }], **_kw(tmp_path))
        assert "跳过" not in ret  # 该条合法（content 有效），仅枚举回退
        rows = _read_back(tmp_path)
        assert len(rows) == 1
        assert rows[0]["status"] == TodoStatus.PENDING.value
        assert rows[0]["priority"] == TodoPriority.MEDIUM.value

    @pytest.mark.parametrize("merge", [False, True])
    def test_dirty_data_both_merge_paths(self, tmp_path, merge):
        """merge=True 与 merge=False 两条路径都能防脏数据。"""
        # 先放一条合法基线
        TodoWrite(todos=[{"id": "base", "content": "基线"}], **_kw(tmp_path))
        ret = TodoWrite(todos=[
            {"id": "ok", "content": "新增合法"},
            {"id": "bad", "status": "completed"},  # 缺 content
        ], merge=merge, **_kw(tmp_path))
        assert isinstance(ret, str)
        assert "跳过 1 条" in ret and "缺少 content" in ret
        contents = [r["content"] for r in _read_back(tmp_path)]
        assert "新增合法" in contents
        assert "" not in contents  # 无空 todo

    def test_merge_existing_not_poisoned_by_dirty_update(self, tmp_path):
        """merge 更新已有条目时，脏 content / 非法枚举不覆盖原合法值。"""
        TodoWrite(todos=[{"id": "1", "content": "原内容",
                          "status": "in_progress", "priority": "high"}], **_kw(tmp_path))
        ret = TodoWrite(todos=[{"id": "1", "status": "???"}],
                        merge=True, **_kw(tmp_path))
        assert isinstance(ret, str)
        rows = _read_back(tmp_path)
        assert rows[0]["content"] == "原内容"
        assert rows[0]["status"] == TodoStatus.IN_PROGRESS.value
        assert rows[0]["priority"] == TodoPriority.HIGH.value


# ── todos 整体非 list ──────────────────────────────────────────────────

class TestNonListInput:
    @pytest.mark.parametrize("bad", [{"a": 1}, None, "oops", 42])
    def test_non_list_returns_error_no_exception(self, tmp_path, bad):
        """经注册 wrapper 调用：非 list 被参数校验层或内部防御降级为 error，绝不外抛。"""
        ret = TodoWrite(todos=bad, **_kw(tmp_path))
        assert isinstance(ret, str)
        assert "error" in ret  # 框架统一文案或内部降级文案，均为 error 状态

    @pytest.mark.parametrize("bad", [{"a": 1}, None, "oops", 42])
    def test_inner_defense_non_list(self, tmp_path, bad):
        """直接调原始函数（__wrapped__），验证 update_todos 的非 list 防御真实生效。"""
        fn = getattr(TodoWrite, "__wrapped__", TodoWrite)
        ret = fn(todos=bad, **_kw(tmp_path))
        assert isinstance(ret, str)
        assert "error" in ret and "数组" in ret and type(bad).__name__ in ret

    def test_update_todos_non_list_marks_invalid(self, tmp_path):
        m = TodoManager("todo-test-session", str(tmp_path))
        res = m.update_todos(None)
        assert isinstance(res, list)  # 返回现有列表，不抛异常
        assert m.last_input_invalid is True
        assert m.last_errors and "数组" in m.last_errors[0]

    def test_non_list_does_not_wipe_existing(self, tmp_path):
        TodoWrite(todos=[{"id": "1", "content": "保留"}], **_kw(tmp_path))
        # 用原始函数绕过 wrapper 类型拦截，验证内部防御不清空既有数据
        fn = getattr(TodoWrite, "__wrapped__", TodoWrite)
        fn(todos=None, **_kw(tmp_path))
        assert [r["content"] for r in _read_back(tmp_path)] == ["保留"]


# ── 正常批次 + 读回一致 ────────────────────────────────────────────────

class TestNormalBehavior:
    def test_valid_batch_roundtrip(self, tmp_path):
        todos = [
            {"id": "1", "content": "任务A", "status": "pending", "priority": "high"},
            {"id": "2", "content": "任务B", "status": "in_progress", "priority": "low"},
        ]
        ret = TodoWrite(todos=todos, **_kw(tmp_path))
        assert "跳过" not in ret
        rows = _read_back(tmp_path)
        assert [r["content"] for r in rows] == ["任务A", "任务B"]
        assert [r["status"] for r in rows] == ["pending", "in_progress"]
        assert [r["priority"] for r in rows] == ["high", "low"]
        assert [r["id"] for r in rows] == ["1", "2"]

    def test_todoread_returns_written_data(self, tmp_path):
        TodoWrite(todos=[{"id": "1", "content": "可读回"}], **_kw(tmp_path))
        ret = TodoRead(**_kw(tmp_path))
        assert isinstance(ret, str) and "可读回" in ret

    def test_real_session_id_with_colons(self, tmp_path):
        """真实会话 id（含冒号）不应被误伤，且脏数据仍被防御。"""
        sid = "single:assistant:sxkiss_com:20261106-1"
        ret = TodoWrite(todos=[{"id": "1", "status": "in_progress"}],
                        **_kw(tmp_path, sid))
        assert isinstance(ret, str) and "跳过" in ret


# ── 历史脏文件 + 意外异常兜底 ──────────────────────────────────────────

class TestRobustness:
    def test_get_todos_skips_corrupted_history(self, tmp_path):
        """落盘文件里混入缺 content 的历史脏条目，只读跳过该条、保留其余。"""
        m = TodoManager("todo-test-session", str(tmp_path))
        with open(m.file_path, "w", encoding="utf-8") as f:
            json.dump([
                {"id": "1", "content": "合法"},
                {"id": "2", "status": "completed"},  # 历史脏条目
            ], f, ensure_ascii=False)
        rows = [t.to_dict() for t in m.get_todos()]
        assert len(rows) == 1 and rows[0]["content"] == "合法"

    def test_unexpected_exception_degrades_to_error(self, tmp_path, monkeypatch):
        """兜底：save_todos 抛未预期异常时，TodoWrite 返回 error 而非外抛。"""
        monkeypatch.setattr(
            TodoManager, "save_todos",
            lambda self, todos: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        ret = TodoWrite(todos=[{"id": "1", "content": "任务"}], **_kw(tmp_path))
        assert isinstance(ret, str) and "error" in ret and "boom" in ret
