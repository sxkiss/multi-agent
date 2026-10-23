"""后台任务持久化测试（对齐 Octop 状态机）"""
import time
import pytest
from concurrent.futures import Future
from pathlib import Path


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test_bg.sqlite"


def test_repo_create(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    assert repo._db_path == db_path
    repo.close()


def test_insert_and_get(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_RUNNING, STATUS_DONE
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Bash", ("echo hello",), STATUS_RUNNING, "session_123")
    row = repo.get("task_001")
    assert row is not None
    assert row.task_id == "task_001"
    assert row.tool == "Bash"
    assert row.status == STATUS_RUNNING
    assert row.session_id == "session_123"
    repo.close()


def test_update_status(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_RUNNING, STATUS_FAILED
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Bash", (), STATUS_RUNNING, "session_1")
    repo.transition("task_001", STATUS_FAILED, "timeout")
    row = repo.get("task_001")
    assert row.status == STATUS_FAILED
    assert row.error == "timeout"
    repo.close()


def test_set_result(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_RUNNING, STATUS_DONE
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Task", ("test",), STATUS_RUNNING, "session_1")
    repo.set_result("task_001", "result content", STATUS_DONE)
    row = repo.get("task_001")
    assert row.result == "result content"
    assert row.status == STATUS_DONE
    repo.close()


def test_transition_to_ready(db_path):
    """done → ready 是合法迁移"""
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_DONE, STATUS_READY
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Bash", (), STATUS_DONE, "session_1")
    assert repo.transition("task_001", STATUS_READY) == True
    row = repo.get("task_001")
    assert row.status == STATUS_READY
    repo.close()


def test_invalid_transition(db_path):
    """ready → running 非法"""
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_READY, STATUS_RUNNING
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Bash", (), STATUS_READY, "session_1")
    assert repo.transition("task_001", STATUS_RUNNING) == False
    repo.close()


def test_is_terminal(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_READY, STATUS_CANCELLED, STATUS_RUNNING
    repo = BgTaskRepo(db_path)
    repo.insert("t1", "Bash", (), STATUS_READY, "s")
    repo.insert("t2", "Bash", (), STATUS_CANCELLED, "s")
    repo.insert("t3", "Bash", (), STATUS_RUNNING, "s")
    assert repo.is_terminal("t1") == True
    assert repo.is_terminal("t2") == True
    assert repo.is_terminal("t3") == False
    repo.close()


def test_get_by_session(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Bash", (), "running", "session_1")
    repo.insert("task_002", "Task", (), "done", "session_1")
    repo.insert("task_003", "Bash", (), "running", "session_2")
    rows = repo.get_by_session("session_1")
    assert len(rows) == 2
    assert all(r.session_id == "session_1" for r in rows)
    repo.close()


def test_restore_running(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo, STATUS_DONE, STATUS_RUNNING, STATUS_PENDING
    repo = BgTaskRepo(db_path)
    repo.insert("task_001", "Bash", ("cmd1",), STATUS_RUNNING, "session_1")
    repo.insert("task_002", "Task", ("cmd2",), STATUS_DONE, "session_1")
    repo.insert("task_003", "Bash", ("cmd3",), STATUS_PENDING, "session_1")
    rows = repo.restore_running()
    assert len(rows) == 2
    task_ids = {r.task_id for r in rows}
    assert "task_001" in task_ids
    assert "task_003" in task_ids
    repo.close()


def test_cleanup_completed(db_path):
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    now = time.time()
    repo.insert("old_1", "Bash", (), "done", "session_1")
    conn = repo._get_conn()
    conn.execute("UPDATE bg_tasks SET updated_at=? WHERE task_id=?", (now - 604800, "old_1"))
    conn.commit()
    repo.insert("new_1", "Bash", (), "done", "session_1")
    deleted = repo.cleanup_completed(keep_seconds=604800)
    assert deleted == 1
    assert repo.get("old_1") is None
    assert repo.get("new_1") is not None
    repo.close()


def test_single_instance(db_path):
    from chat_client.tools.bg_repo import get_repo, close_repo
    close_repo()
    repo1 = get_repo()
    repo2 = get_repo()
    assert repo1 is repo2
    close_repo()


def test_background_task_manager_integration():
    """端到端：注册 → 完成 → drain → ready"""
    from chat_client.tools import get_background_manager
    bg = get_background_manager()
    task_id = "test_integration"
    session_id = "test_session"
    future = Future()
    bg.register(task_id, "TestTool", (), future, session_id)
    t = bg.get(task_id)
    assert t is not None
    assert t.status == "running"
    future.set_result("result")
    bg.notify_done(task_id, "result")
    t = bg.get(task_id)
    assert t.status == "done"
    results = bg.drain_finished(session_id)
    assert len(results) == 1
    assert results[0]["task_id"] == task_id
    assert results[0]["status"] == "done"
    from chat_client.tools.bg_repo import get_repo, STATUS_READY
    row = get_repo().get(task_id)
    assert row is not None
    assert row.status == STATUS_READY
    bg.remove(task_id)
