"""
@input: json, os, threading, time, typing, uuid
@position: Knowledge layer — 文档索引任务队列（信号量并发 + 原子替换）
@auto-doc: Update header and folder INDEX.md when this file changes


bt 此前 add_document 是**同步**的——大文档/批量入库会阻塞请求。本模块提供
异步索引队列：

- 提交任务：submit(kb, doc) → 返回 job_id
- 后台 worker：单线程顺序处理（或限并发），避免并发写坏向量库
- 状态查询：status(job_id) → pending/running/done/failed
- 队列统计：stats()

设计取舍：用信号量并发+原子替换；bt 单库场景用单 worker 顺序处理
即可（避免并发写 JSON 向量库的竞争）。
"""
import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger(__name__)


class IndexJobQueue:
    """知识库索引任务队列（单 worker 顺序处理）。"""

    def __init__(self, worker: Callable[[dict], Any] | None = None):
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._queue: list[str] = []
        self._worker = worker
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def submit(self, *, kb: Any, path: str = "", text: str = "",
               name: str = "") -> str:
        jid = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[jid] = {
                "id": jid, "path": path, "text": (text or "")[:200] + ("…" if len(text or "") > 200 else ""),
                "name": name, "status": "pending", "created_at": time.time(),
            }
            self._queue.append(jid)
            self._jobs[jid]["_kb"] = kb
            self._jobs[jid]["_full_text"] = text
        self._ensure_worker()
        return jid

    def _ensure_worker(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kb-index-worker", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                jid = self._queue.pop(0) if self._queue else None
            if jid is None:
                time.sleep(0.2)
                continue
            with self._lock:
                job = self._jobs.get(jid)
                if not job:
                    continue
                job["status"] = "running"
                job["started_at"] = time.time()
            try:
                if self._worker is not None:
                    self._worker(job)
                else:
                    kb = job.get("_kb")
                    kb.add_document(path=job.get("path") or None,
                                    text=job.get("_full_text") or None,
                                    name=job.get("name") or None)
                with self._lock:
                    job["status"] = "done"
                    job["finished_at"] = time.time()
            except Exception as e:
                logger.warning("[KB-Index] 任务 %s 失败: %s", jid, e)
                with self._lock:
                    job["status"] = "failed"
                    job["error"] = str(e)
                    job["finished_at"] = time.time()
            finally:
                with self._lock:
                    job.pop("_kb", None)
                    job.pop("_full_text", None)

    def status(self, jid: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return None
            return {k: v for k, v in job.items() if not k.startswith("_")}

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            jobs = [{k: v for k, v in j.items() if not k.startswith("_")}
                    for j in self._jobs.values()]
        jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        return jobs[:limit]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            states: dict[str, int] = {}
            for j in self._jobs.values():
                states[j["status"]] = states.get(j["status"], 0) + 1
            queued = len(self._queue)
        return {"total": len(self._jobs), "queued": queued, "by_status": states}

    def shutdown(self) -> None:
        self._stop.set()


_queues: dict[int, IndexJobQueue] = {}
_lock = threading.Lock()


def get_index_queue() -> IndexJobQueue:
    """取全局知识库索引队列（单例）。"""
    global _queues
    with _lock:
        if 0 not in _queues:
            _queues[0] = IndexJobQueue()
        return _queues[0]
