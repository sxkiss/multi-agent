"""后台任务持久化测试"""
import json
import os
import tempfile
import time
import pytest
from pathlib import Path


@pytest.fixture
def db_path(tmp_path):
    """临时数据库路径"""
    return tmp_path / "test_bg.sqlite"


def test_repo_create(db_path):
    """测试仓库创建"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    assert repo._db_path == db_path
    repo.close()


def test_insert_and_get(db_path):
    """测试插入和查询"""
    from chat_client.tools.bg_repo import BgTaskRepo, BgTaskRow
    repo = BgTaskRepo(db_path)
    
    # 插入
    repo.insert("task_001", "Bash", ("echo hello",), "running", "session_123")
    
    # 查询
    row = repo.get("task_001")
    assert row is not None
    assert row.task_id == "task_001"
    assert row.tool == "Bash"
    assert row.status == "running"
    assert row.session_id == "session_123"
    assert row.delivered == False
    
    repo.close()


def test_update_status(db_path):
    """测试状态更新"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    
    repo.insert("task_001", "Bash", (), "running", "session_1")
    repo.update_status("task_001", "failed", "timeout")
    
    row = repo.get("task_001")
    assert row.status == "failed"
    assert row.error == "timeout"
    
    repo.close()


def test_set_result(db_path):
    """测试结果设置"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    
    repo.insert("task_001", "Task", ("test",), "running", "session_1")
    repo.set_result("task_001", "result content", "done")
    
    row = repo.get("task_001")
    assert row.result == "result content"
    assert row.status == "done"
    
    repo.close()


def test_mark_delivered(db_path):
    """测试 delivered 标记"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    
    repo.insert("task_001", "Bash", (), "running", "session_1")
    repo.mark_delivered("task_001")
    
    row = repo.get("task_001")
    assert row.delivered == True
    
    repo.close()


def test_get_by_session(db_path):
    """测试按会话查询"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    
    repo.insert("task_001", "Bash", (), "running", "session_1")
    repo.insert("task_002", "Task", (), "done", "session_1")
    repo.insert("task_003", "Bash", (), "running", "session_2")
    
    rows = repo.get_by_session("session_1")
    assert len(rows) == 2
    assert all(r.session_id == "session_1" for r in rows)
    
    rows_running = repo.get_by_session("session_1", status="running")
    assert len(rows_running) == 1
    assert rows_running[0].task_id == "task_001"
    
    repo.close()


def test_restore_running(db_path):
    """测试恢复 running 任务"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    
    # 插入不同状态的任务
    repo.insert("task_001", "Bash", ("cmd1",), "running", "session_1")
    repo.insert("task_002", "Task", ("cmd2",), "done", "session_1")
    repo.insert("task_003", "Bash", ("cmd3",), "pending", "session_1")
    
    # 恢复
    rows = repo.restore_running()
    assert len(rows) == 2  # 只恢复 running 和 pending
    task_ids = {r.task_id for r in rows}
    assert "task_001" in task_ids
    assert "task_003" in task_ids
    
    repo.close()


def test_cleanup_completed(db_path):
    """测试清理已完成任务"""
    from chat_client.tools.bg_repo import BgTaskRepo
    repo = BgTaskRepo(db_path)
    
    now = time.time()
    # 插入旧任务（7 天前完成）
    repo.insert("old_1", "Bash", (), "done", "session_1")
    repo.update_status("old_1", "done")
    # 直接修改 updated_at 为 7 天前
    conn = repo._get_conn()
    conn.execute(
        "UPDATE bg_tasks SET updated_at = ? WHERE task_id = ?",
        (now - 604800, "old_1")
    )
    conn.commit()
    
    # 插入新任务（刚完成）
    repo.insert("new_1", "Bash", (), "done", "session_1")
    
    # 清理
    deleted = repo.cleanup_completed(keep_seconds=604800)
    assert deleted == 1
    
    # 验证
    assert repo.get("old_1") is None
    assert repo.get("new_1") is not None
    
    repo.close()


def test_single_instance(db_path):
    """测试单例模式"""
    from chat_client.tools.bg_repo import get_repo, close_repo
    
    # 清除旧实例
    close_repo()
    
    # 获取单例
    repo1 = get_repo()
    repo2 = get_repo()
    assert repo1 is repo2
    
    # 清理
    close_repo()


def test_background_task_manager_integration():
    """测试与 BackgroundTaskManager 的集成"""
    from chat_client.tools import get_background_manager, BackgroundTaskManager
    from concurrent.futures import Future
    
    bg = get_background_manager()
    assert isinstance(bg, BackgroundTaskManager)
    
    # 模拟任务注册
    task_id = "test_integration"
    session_id = "test_session"
    
    # 注册
    future = Future()
    bg.register(task_id, "TestTool", (), future, session_id)
    
    # 验证内存状态
    t = bg.get(task_id)
    assert t is not None
    assert t.status == "running"
    assert t.session_id == session_id
    
    # 模拟完成
    future.set_result("result")
    bg.notify_done(task_id, "result")
    
    # 验证状态
    t = bg.get(task_id)
    assert t.status == "done"
    assert t.delivered == False  # 还未 drain
    
    # Drain
    results = bg.drain_finished(session_id)
    assert len(results) == 1
    assert results[0]["task_id"] == task_id
    assert results[0]["status"] == "done"
    
    # 验证 delivered
    t = bg.get(task_id)
    assert t.delivered == True
    
    # 清理
    bg.remove(task_id)
