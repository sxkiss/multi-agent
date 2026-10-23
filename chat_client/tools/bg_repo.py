"""后台任务持久化仓库（SQLite）

对齐 Octop 的 thread_history_projection 机制：
- 状态流转：pending → queued → running → done/failed/cancelled → ready
- ready = 结果已注入对话上下文，不会再被 drain_finished 重复消费
- 无 delivered 字段，用 status=ready 替代
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

_DB_PATH = Path(__file__).parent.parent.parent / "background_tasks.sqlite"

# 状态常量（对齐 Octop）
STATUS_PENDING   = "pending"
STATUS_QUEUED    = "queued"
STATUS_RUNNING   = "running"
STATUS_DONE      = "done"
STATUS_FAILED    = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_READY     = "ready"

ALL_STATUSES = (
    STATUS_PENDING, STATUS_QUEUED, STATUS_RUNNING,
    STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, STATUS_READY,
)

# 允许的状态迁移；空集合表示终态
_TRANSITIONS: dict[str, set[str]] = {
    STATUS_PENDING:   {STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLED},
    STATUS_QUEUED:    {STATUS_RUNNING, STATUS_CANCELLED},
    STATUS_RUNNING:   {STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED},
    STATUS_DONE:      {STATUS_READY},
    STATUS_FAILED:    {STATUS_READY},
    STATUS_CANCELLED: set(),
    STATUS_READY:     set(),
}

TERMINAL_STATUSES = frozenset({STATUS_CANCELLED, STATUS_READY})


class BgTaskRow:
    __slots__ = (
        "task_id", "tool", "args_json", "status", "result",
        "session_id", "start_time", "updated_at", "error",
    )

    def __init__(
        self,
        task_id: str,
        tool: str,
        args_json: str,
        status: str,
        result: str = "",
        session_id: str = "",
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
        self.start_time = start_time
        self.updated_at = updated_at
        self.error = error


class BgTaskRepo:
    """后台任务持久化仓库（SQLite）

    线程安全：所有方法内部加锁，支持多线程并发访问。
    状态机对齐 Octop：pending → queued → running → done/failed/cancelled → ready
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or _DB_PATH
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self) -> None:
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
                    start_time REAL DEFAULT 0,
                    updated_at REAL DEFAULT 0,
                    error TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bg_session ON bg_tasks(session_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bg_updated  ON bg_tasks(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bg_status  ON bg_tasks(status)")
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path), timeout=5, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn

    def insert(self, task_id: str, tool: str, args: tuple,
               status: str = STATUS_PENDING, session_id: str = "") -> None:
        now = time.time()
        args_json = json.dumps(args, ensure_ascii=False)
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO bg_tasks "
                "(task_id, tool, args_json, status, session_id, start_time, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_id, tool, args_json, status, session_id, now, now),
            )
            conn.commit()

    def transition(self, task_id: str, new_status: str, error: str = "") -> bool:
        """按状态机迁移任务状态。

        Returns:
            True = 迁移成功（或已是目标状态），False = 非法迁移。
        """
        if new_status not in ALL_STATUSES:
            logger.warning("[BgTask] 非法目标状态 %r task=%s", new_status, task_id[:12])
            return False
        current = self.get(task_id)
        if current is None:
            logger.warning("[BgTask] 任务不存在 task=%s", task_id[:12])
            return False
        if current.status == new_status:
            return True
        allowed = _TRANSITIONS.get(current.status, set())
        if new_status not in allowed:
            logger.warning(
                "[BgTask] 非法迁移 %s -> %s task=%s（允许：%s）",
                current.status, new_status, task_id[:12],
                sorted(allowed) or "终态",
            )
            return False
        self.update_status(task_id, new_status, error)
        return True

    def is_terminal(self, task_id: str) -> bool:
        """任务是否已到终态（cancelled / ready），终态不再产生新结果。"""
        row = self.get(task_id)
        return bool(row and row.status in TERMINAL_STATUSES)

    def update_status(self, task_id: str, status: str, error: str = "") -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE bg_tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                (status, error, time.time(), task_id),
            )
            conn.commit()

    def set_result(self, task_id: str, result: str, status: str) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "UPDATE bg_tasks SET result=?, status=?, updated_at=? WHERE task_id=?",
                (result, status, time.time(), task_id),
            )
            conn.commit()

    def get(self, task_id: str) -> Optional[BgTaskRow]:
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
                start_time=row["start_time"] or 0.0,
                updated_at=row["updated_at"] or 0.0,
                error=row["error"] or "",
            )

    def get_by_session(self, session_id: str, status: Optional[str] = None) -> list[BgTaskRow]:
        with self._lock:
            conn = self._get_conn()
            if status:
                rows = conn.execute(
                    "SELECT * FROM bg_tasks WHERE session_id=? AND status=? ORDER BY start_time",
                    (session_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bg_tasks WHERE session_id=? ORDER BY start_time",
                    (session_id,),
                ).fetchall()
            return [
                BgTaskRow(
                    task_id=r["task_id"], tool=r["tool"],
                    args_json=r["args_json"] or "", status=r["status"],
                    result=r["result"] or "", session_id=r["session_id"] or "",
                    start_time=r["start_time"] or 0.0,
                    updated_at=r["updated_at"] or 0.0,
                    error=r["error"] or "",
                )
                for r in rows
            ]

    def restore_running(self) -> list[BgTaskRow]:
        """进程重启后恢复 pending/queued/running 状态的任务。"""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT * FROM bg_tasks WHERE status IN (?,?,?) ORDER BY start_time",
                (STATUS_PENDING, STATUS_QUEUED, STATUS_RUNNING),
            ).fetchall()
            return [
                BgTaskRow(
                    task_id=r["task_id"], tool=r["tool"],
                    args_json=r["args_json"] or "", status=r["status"],
                    result=r["result"] or "", session_id=r["session_id"] or "",
                    start_time=r["start_time"] or 0.0,
                    updated_at=r["updated_at"] or 0.0,
                    error=r["error"] or "",
                )
                for r in rows
            ]

    def cleanup_completed(self, keep_seconds: int = 604800) -> int:
        """清理已完成且超过保留期的任务（默认 7 天）。"""
        cutoff = time.time() - keep_seconds
        with self._lock:
            conn = self._get_conn()
            cur = conn.execute(
                "DELETE FROM bg_tasks WHERE status IN (?,?,?) AND updated_at < ?",
                (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, cutoff),
            )
            conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# 全局单例
_repo: Optional[BgTaskRepo] = None
_repo_lock = threading.Lock()


def get_repo() -> BgTaskRepo:
    global _repo
    with _repo_lock:
        if _repo is None:
            _repo = BgTaskRepo()
        return _repo


def close_repo() -> None:
    global _repo
    with _repo_lock:
        if _repo is not None:
            _repo.close()
            _repo = None
