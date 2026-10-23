"""后台任务持久化仓库（SQLite）

对齐 Octop 的 thread_history_projection 机制，但针对 BT 的后台任务场景简化：
- 每个任务一行，状态从 pending → queued → running → done/failed/cancelled
- delivered 标记结果是否已注入对话上下文
- 支持进程重启后恢复 running 状态的任务
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 数据库路径：与 memory_octop.sqlite 同目录
_DB_PATH = Path(__file__).parent.parent.parent / "background_tasks.sqlite"


class BgTaskRow:
    """任务数据行"""
    __slots__ = (
        "task_id", "tool", "args_json", "status", "result",
        "session_id", "delivered", "start_time", "updated_at", "error"
    )

    def __init__(
        self,
        task_id: str,
        tool: str,
        args_json: str,
        status: str,
        result: str = "",
        session_id: str = "",
        delivered: int = 0,
        start_time: float = 0.0,
        updated_at: float = 0.0,
        error: str = "",
    ):
        self.task_id = task_id
        self.tool = tool
        self.args_json = args_json
        self.status = status
        self.result = result
        self.session_id = session_id
        self.delivered = bool(delivered)
        self.start_time = start_time
        self.updated_at = updated_at
        self.error = error


class BgTaskRepo:
    """后台任务持久化仓库

    线程安全：所有方法内部加锁，支持多线程并发访问。
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or _DB_PATH
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
        """初始化数据库表结构"""
        with self._lock:
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bg_tasks (
                    task_id TEXT PRIMARY KEY,
                    tool TEXT NOT NULL,
                    args_json TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    result TEXT DEFAULT '',
                    session_id TEXT DEFAULT '',
                    delivered INTEGER DEFAULT 0,
                    start_time REAL DEFAULT 0,
                    updated_at REAL DEFAULT 0,
                    error TEXT DEFAULT ''
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bg_tasks_session 
                ON bg_tasks(session_id, status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bg_tasks_updated 
                ON bg_tasks(updated_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_bg_tasks_status 
                ON bg_tasks(status)
            """)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        """获取数据库连接（线程内缓存）"""
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path),
                timeout=5,
                check_same_thread=False  # 允许跨线程访问
            )
            self._conn.row_factory = sqlite3.Row
            # WAL 模式：提高并发性能
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def insert(self, task_id: str, tool: str, args: tuple,
               status: str = "pending", session_id: str = "") -> None:
        """注册新任务"""
        now = time.time()
        args_json = json.dumps(args, ensure_ascii=False)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO bg_tasks
                   (task_id, tool, args_json, status, session_id, start_time, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, tool, args_json, status, session_id, now, now)
            )
            conn.commit()

    def update_status(self, task_id: str, status: str, error: str = "") -> None:
        """更新任务状态"""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """UPDATE bg_tasks SET status = ?, error = ?, updated_at = ?
                   WHERE task_id = ?""",
                (status, error, time.time(), task_id)
            )
            conn.commit()

    def set_result(self, task_id: str, result: str, status: str) -> None:
        """设置任务结果并更新状态"""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """UPDATE bg_tasks SET result = ?, status = ?, updated_at = ?
                   WHERE task_id = ?""",
                (result, status, time.time(), task_id)
            )
            conn.commit()

    def mark_delivered(self, task_id: str) -> None:
        """标记结果已注入上下文"""
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """UPDATE bg_tasks SET delivered = 1, updated_at = ?
                   WHERE task_id = ?""",
                (time.time(), task_id)
            )
            conn.commit()

    def get(self, task_id: str) -> Optional[BgTaskRow]:
        """查询单条任务"""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT * FROM bg_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return BgTaskRow(
                task_id=row["task_id"],
                tool=row["tool"],
                args_json=row["args_json"] or "",
                status=row["status"],
                result=row["result"] or "",
                session_id=row["session_id"] or "",
                delivered=row["delivered"],
                start_time=row["start_time"] or 0.0,
                updated_at=row["updated_at"] or 0.0,
                error=row["error"] or "",
            )

    def get_by_session(self, session_id: str, status: Optional[str] = None) -> list[BgTaskRow]:
        """查询某会话的所有任务"""
        with self._lock:
            conn = self._get_conn()
            if status:
                rows = conn.execute(
                    "SELECT * FROM bg_tasks WHERE session_id = ? AND status = ? ORDER BY start_time",
                    (session_id, status)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bg_tasks WHERE session_id = ? ORDER BY start_time",
                    (session_id,)
                ).fetchall()
            return [
                BgTaskRow(
                    task_id=r["task_id"],
                    tool=r["tool"],
                    args_json=r["args_json"] or "",
                    status=r["status"],
                    result=r["result"] or "",
                    session_id=r["session_id"] or "",
                    delivered=r["delivered"],
                    start_time=r["start_time"] or 0.0,
                    updated_at=r["updated_at"] or 0.0,
                    error=r["error"] or "",
                )
                for r in rows
            ]

    def restore_running(self) -> list[BgTaskRow]:
        """进程重启后恢复 running 状态的任务"""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM bg_tasks WHERE status IN ('pending', 'queued', 'running') ORDER BY start_time"
            ).fetchall()
            return [
                BgTaskRow(
                    task_id=r["task_id"],
                    tool=r["tool"],
                    args_json=r["args_json"] or "",
                    status=r["status"],
                    result=r["result"] or "",
                    session_id=r["session_id"] or "",
                    delivered=r["delivered"],
                    start_time=r["start_time"] or 0.0,
                    updated_at=r["updated_at"] or 0.0,
                    error=r["error"] or "",
                )
                for r in rows
            ]

    def cleanup_completed(self, keep_seconds: int = 604800) -> int:
        """清理已完成的旧任务（默认保留 7 天）

        Returns:
            删除的任务数
        """
        cutoff = time.time() - keep_seconds
        with self._lock:
            conn = self._get_conn()
            # 删除已完成且超过保留期的任务
            cur = conn.execute(
                """DELETE FROM bg_tasks 
                   WHERE status IN ('done', 'failed', 'cancelled') 
                   AND updated_at < ?""",
                (cutoff,)
            )
            conn.commit()
            return cur.rowcount

    def close(self) -> None:
        """关闭数据库连接"""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# 全局单例
_repo: Optional[BgTaskRepo] = None
_repo_lock = threading.Lock()


def get_repo() -> BgTaskRepo:
    """获取全局仓库单例"""
    global _repo
    with _repo_lock:
        if _repo is None:
            _repo = BgTaskRepo()
        return _repo


def close_repo() -> None:
    """关闭全局仓库（服务关闭时调用）"""
    global _repo
    with _repo_lock:
        if _repo is not None:
            _repo.close()
            _repo = None
