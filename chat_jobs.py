"""后台聊天任务：ChatJob 事件流与 ChatJobManager 生命周期管理。

优-1 第九阶段：从 web_server.py 拆出的任务运行时。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_BASE_DIR = ""
_BASE_DIR_PROVIDER = None


def _base_dir() -> str:
    """每次动态读取路径：测试会 monkeypatch web_server.BASE_DIR。"""
    if _BASE_DIR_PROVIDER is not None:
        return _BASE_DIR_PROVIDER()
    return _BASE_DIR
class ChatJob:
    """一个会话的后台聊天任务：与 HTTP 连接解耦，事件缓存在内存供重连回放。
    事件增量落盘到 jobs/<key>.jsonl，服务重启后可恢复为中断/完成状态回放。"""
    TERMINAL_STATUSES = ("done", "error", "stopped")

    _MAX_EVENTS = int(os.environ.get("AI_AGENT_MAX_JOB_EVENTS", "2000"))

    def __init__(self, session_id, label="", persist=True, key=""):
        self.session_id = session_id
        self.label = (label or "")[:40]
        self.key = key or f"{session_id}::{uuid.uuid4().hex[:6]}"
        self.events = []            # [{"id": seq, "event": name, "data": ...}]，回放按 id 过滤，超出上限会丢弃最旧的
        # 事件游标：单调递增、永不回退。绝不可用 len(self.events) 当 id ——
        # 事件数超 _MAX_EVENTS 时缓冲区会截断，若 id 是数组下标就会整体回退，
        # 导致客户端 last_id 永远大于所有现存 id，snapshot_from 恒返回空，
        # SSE 续传死锁（表现为 last_id 卡在上限-1、工具计数不涨、"连接失败"）。
        self._seq = -1
        self.status = "running"     # running | done | error | stopped
        self.agent = None
        self.cancel_event = threading.Event()  # chat_stop 置位；Agent 循环在检查点据此提前退出
        self.created_at = time.time()
        # 结束时刻（time.time()）：孤儿回调据此判断父 job 是否已死透。
        # 不能用"任务完成时刻"当基准——长任务完成时父 job 可能已结束十几分钟，
        # 那样会误判成"还在窗口内"，把结果追加到一个没人订阅的死 job 上。
        self.finished_at = None
        self._lock = threading.Lock()
        # 事件持久化（JSONL 追加）
        self.persist = persist
        self.persist_path = os.path.join(_base_dir(), "jobs", f"{self.key.replace(chr(47), '_')}.jsonl")
        if persist:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)

    def attach_agent(self, agent):
        self.agent = agent

    def request_stop(self):
        """请求停止：置位取消标志，Agent 循环在检查点据此提前退出。

        注意：Event 一旦置位不可撤销；chat_stop 停止的是正在运行的任务，
        已终结的任务不会再被复用，因此无需重置。
        """
        self.cancel_event.set()

    def append(self, event, data=None):
        with self._lock:
            self._seq += 1
            self.events.append({"id": self._seq, "event": event, "data": data})
            # 限制事件缓存大小，避免长时间挂起的任务撑爆内存
            # 只截断，绝不重编号：id 来自单调递增的 _seq，截断后依然递增，
            # 客户端游标永远有效（旧游标最多落后被丢弃的区间，不会恒大于全部 id）。
            if len(self.events) > self._MAX_EVENTS:
                self.events = self.events[-self._MAX_EVENTS:]
        # 增量落盘（原子追加）
        if self.persist:
            try:
                with open(self.persist_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str) + "\n")
            except Exception:
                logger.warning("ChatJob.append persist 写入失败", exc_info=True)

    def snapshot_from(self, last_id, drop_events=None):
        """返回 (last_id 之后的事件, 当前状态)。

        drop_events：需要对本订阅者隐藏的事件名集合。按订阅者过滤而非任务级，
        因为同一 job 可能被网页端与小程序端同时订阅（渠道不同、可见性不同）。
        """
        with self._lock:
            pending = [
                e for e in self.events
                if e["id"] > last_id and (not drop_events or e["event"] not in drop_events)
            ]
            return pending, self.status

    def finish(self, status):
        with self._lock:
            # P1-5: 已处于终态（done/stopped/error）时不再覆盖，防止 chat_stop
            # 后 agent 线程异常把 "stopped" 写成 "error"
            if self.status in {"done", "stopped", "error"} and self.status != status:
                return
            self.status = status
            if status in {"done", "stopped", "error"} and self.finished_at is None:
                self.finished_at = time.time()
        if self.persist:
            try:
                with open(self.persist_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"__status__": status}, ensure_ascii=False) + "\n")
            except Exception:
                logger.warning("ChatJob.finish persist 写入失败", exc_info=True)

    @property
    def is_running(self):
        with self._lock:
            return self.status == "running"

    @classmethod
    def recover(cls, key):
        """从 jobs/<key>.jsonl 恢复任务（重启后回放中断/完成状态）"""
        p = os.path.join(_base_dir(), "jobs", f"{key.replace(chr(47), '_')}.jsonl")
        if not os.path.exists(p):
            return None
        sid = key.split("::")[0]
        job = cls(session_id=sid, label="(恢复)", persist=False, key=key)
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        logger.warning("处理时跳过异常", exc_info=True)
                        continue
                    if "__status__" in rec:
                        job._lock.acquire()
                        try:
                            job.status = rec["__status__"]
                        finally:
                            job._lock.release()
                    else:
                        job._lock.acquire()
                        try:
                            job._seq += 1
                            job.events.append({"id": job._seq,
                                               "event": rec.get("event", "message"),
                                               "data": rec.get("data")})
                        finally:
                            job._lock.release()
        except Exception:
            return None
        # 与 append() 一致的上限：磁盘上的 jsonl 是完整历史（无截断），
        # 恢复时若全量灌入会把 105MB 级别的历史一次性读进内存。
        # 只保留最近 _MAX_EVENTS 条；id 由 _seq 单调分配，截断时不得重编号，
        # 否则 id 回退会让前端游标失效（同 append 的 SSE 续传死锁）。
        if len(job.events) > cls._MAX_EVENTS:
            job.events = job.events[-cls._MAX_EVENTS:]
        # 进程重启后，写这个文件的 agent 线程已不存在，绝不可能仍在运行。
        # 若文件没有 __status__ 行（任务被 kill 而非正常结束），job.status 会停在
        # 构造时的 "running" 变成僵尸任务 —— 它会长期占用该会话的 MAX_PARALLEL
        # 名额，导致同会话无法再创建新任务。恢复的 job 一律视为已终结。
        if job.status == "running":
            job.status = "stopped"
        job.created_at = int(time.time())
        return job


class ChatJobManager:
    """后台聊天任务管理：同会话支持并行（上限 MAX_PARALLEL），按 job_key 索引"""

    JOB_TTL = 3600
    MAX_PARALLEL = int(os.environ.get("AI_AGENT_MAX_PARALLEL", "3"))
    # 磁盘上 jobs/*.jsonl 的保留时长（秒）。jobs/ 只增不删，105MB 且无清理逻辑，
    # 必须按 mtime 回收，否则磁盘无限增长。0 或负 = 不清理。
    JOB_FILE_TTL = int(os.environ.get("AI_AGENT_JOB_FILE_TTL", str(7 * 24 * 3600)))
    # 单次清理最多删除的文件数，避免首次运行时一次性 unlink 上千文件卡住事件循环
    JOB_FILE_PRUNE_BATCH = int(os.environ.get("AI_AGENT_JOB_FILE_PRUNE_BATCH", "500"))
    # 启动时最多恢复多少个 job（recover 会读盘，需与磁盘清理配合才有意义）
    MAX_RECOVER = int(os.environ.get("AI_AGENT_MAX_RECOVER", "50"))

    def __init__(self):
        self._jobs = {}
        self._lock = threading.RLock()  # RLock: 可重入，允许嵌套调用

    @staticmethod
    def make_key(session_id):
        return f"{session_id}::{uuid.uuid4().hex[:6]}"

    def get(self, key):
        with self._lock:
            return self._jobs.get(key)

    def active_for_session(self, session_id):
        with self._lock:
            return [(k, j) for k, j in self._jobs.items() if j.session_id == session_id]

    def count_running(self, session_id):
        with self._lock:
            return len([1 for _, j in self.active_for_session(session_id) if j.is_running])

    def create_if_idle(self, session_id, label=""):
        """并行创建；达到会话并行上限返回 None（原子操作，防竞态）"""
        with self._lock:
            # 原子检查+创建：在同一锁内完成计数判断与任务创建，避免并发窗口
            running_count = len([1 for k, j in self._jobs.items()
                                 if j.session_id == session_id and j.is_running])
            if running_count >= self.MAX_PARALLEL:
                return None
            self._prune_locked()
            key = self.make_key(session_id)
            # 必须把最终 key 直接传给构造函数：ChatJob.__init__ 会用它生成
            # persist_path。若先构造再覆盖 job.key，persist_path 仍指向构造时
            # 默认 key 的文件，导致所有 job 事件混写到一个随机文件、磁盘恢复
            # 时 key 对不上、SSE 续传死锁（last_id 卡住不前进）。
            job = ChatJob(session_id, label, key=key)
            self._jobs[key] = job
            return job

    def discard(self, key):
        with self._lock:
            j = self._jobs.get(key)
            if j and not j.events:
                self._jobs.pop(key, None)

    def stop(self, key=None, session_id=None):
        targets = []
        with self._lock:
            if key:
                j = self._jobs.get(key)
                if j:
                    targets.append(j)
            elif session_id:
                targets = [j for _, j in self.active_for_session(session_id)]
        for j in targets:
            if j.is_running:
                # 先置位取消标志（Agent 线程在检查点轮询），再置终态；顺序不可颠倒，
                # 否则 Agent 可能在本线程 close() 之后仍继续下一轮请求
                j.request_stop()
                j.finish("stopped")
                try:
                    if j.agent is not None and hasattr(j.agent, "close"):
                        j.agent.close()
                except Exception:
                    logger.warning("ChatJobManager agent.close 失败，已记录", exc_info=True)
        return targets

    def recover_all(self):
        """启动时从 jobs/ 恢复近期任务（供续播）。

        按 mtime 倒序只取最近的 MAX_RECOVER 个：磁盘历史可能上千个文件，
        全量恢复会把每个 jsonl 都读进内存（recover 内部另有单任务事件上限，
        但文件数本身仍是无界的）。
        """
        import glob as _glob
        jobs_dir = os.path.join(_base_dir(), "jobs")
        if not os.path.isdir(jobs_dir):
            return 0
        n = 0
        try:
            files = _glob.glob(os.path.join(jobs_dir, "*.jsonl"))
            # 只保留最近修改的一批，避免遍历+读盘上千个历史文件
            files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0,
                       reverse=True)
            files = files[:self.MAX_RECOVER]
        except Exception:
            logger.warning("[ChatJob] 枚举 jobs/ 失败", exc_info=True)
            return 0
        for p in files:
            key = os.path.basename(p)[:-6]
            # 已存在则不覆盖
            if key in self._jobs:
                continue
            job = ChatJob.recover(key)
            if job:
                with self._lock:
                    self._jobs[key] = job
                n += 1
        log_ = logging
        if n:
            log_.info(f"[ChatJob] 启动恢复 {n} 个任务")
        return n

    def prune_job_files(self):
        """按 mtime 清理 jobs/ 下过期的 .jsonl 文件。

        jobs/ 只有写入没有删除，磁盘占用只增不减；这里按 TTL 回收，
        单批次上限 JOB_FILE_PRUNE_BATCH，剩余留到下一轮，避免首次运行时
        一次性 unlink 上千文件卡住事件循环。
        """
        ttl = self.JOB_FILE_TTL
        if ttl <= 0:
            return 0
        import glob as _glob
        jobs_dir = os.path.join(_base_dir(), "jobs")
        if not os.path.isdir(jobs_dir):
            return 0
        now = time.time()
        removed = 0
        skipped = 0
        try:
            files = _glob.glob(os.path.join(jobs_dir, "*.jsonl"))
            # 先按 mtime 升序：旧文件优先清理，保证「至少清掉最老的 N 个」
            files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0.0)
        except Exception:
            logger.warning("[ChatJob] 枚举 jobs/ 失败", exc_info=True)
            return 0
        for p in files:
            try:
                if now - os.path.getmtime(p) < ttl:
                    continue
                # 仅清理已终结/非活跃的任务：内存里的运行中任务正在写这个文件
                key = os.path.basename(p)[:-6]
                with self._lock:
                    live = self._jobs.get(key)
                if live is not None and live.is_running:
                    continue
                os.remove(p)
                removed += 1
                if removed >= self.JOB_FILE_PRUNE_BATCH:
                    skipped = len(files) - removed
                    break
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning("[ChatJob] 清理 job 文件失败，已记录", exc_info=True)
        if removed:
            logging.info(f"[ChatJob] 清理过期 job 文件 {removed} 个"
                         + (f"，剩余 {skipped} 个待下一轮" if skipped else ""))
        return removed

    def _prune_locked(self):
        now = time.time()
        stale = [k for k, j in self._jobs.items()
                 if not j.is_running and now - j.created_at > self.JOB_TTL]
        for k in stale:
            self._jobs.pop(k, None)

    # 内存回收单批上限：避免一次性清理上千僵尸任务卡住事件循环
    MEMORY_PRUNE_BATCH = int(os.environ.get("AI_AGENT_MEMORY_PRUNE_BATCH", "500"))

    def prune_memory_jobs(self, max_batch: int | None = None):
        """周期性回收内存 _jobs 中已终结的僵尸任务，并释放 agent 引用。

        背景：内存增长主因是"对话后再也不活跃"的会话，其已完成 job 永久驻留
        _jobs 字典——_prune_locked 只在 create_if_idle（新消息到来）时触发，
        对长期静默的会话从不生效。job.agent 持有 MemoryManager.history 全量
        历史（sessions.json + journal），只要 job 不被移除，这些 Python 大对象
        就永远不会被 GC 回收，导致 RSS 越用越大。

        这里独立周期扫描：终结（done/stopped/error）且超过 JOB_TTL 的 job 直接
        移除，并置空 agent/events 引用，让 GC 及时回收历史与事件缓存。
        任务正 running 或仍在 TTL 内的 job 保留（用户可能续播）。
        """
        ttl = self.JOB_TTL
        if ttl <= 0:
            return 0
        batch = self.MEMORY_PRUNE_BATCH if max_batch is None else max_batch
        now = time.time()
        removed = 0
        with self._lock:
            stale = [k for k, j in self._jobs.items()
                     if not j.is_running and now - j.created_at > ttl]
            for k in stale:
                j = self._jobs.pop(k, None)
                if j is None:
                    continue
                # 释放大对象引用，帮助 GC 及时回收历史/事件缓存
                try:
                    if j.agent is not None and hasattr(j.agent, "close"):
                        j.agent.close()
                except Exception:
                    logger.warning("[ChatJob] 内存回收 agent.close 失败", exc_info=True)
                j.agent = None
                j.events = []
                removed += 1
                if removed >= batch:
                    break
        if removed:
            logger.info("[ChatJob] 内存回收 %d 个已终结僵尸任务", removed)
        return removed

    # 磁盘上 sessions/ 会话目录的保留时长（秒）。默认 3 天。
    # sessions/ 只增不删（每个会话一个目录，含 sessions.json / journal / meta），
    # 历史会话大量堆积占用磁盘，按目录 mtime 回收。0 或负 = 不清理。
    SESSION_TTL = int(os.environ.get("AI_AGENT_SESSION_TTL", str(3 * 24 * 3600)))
    # 单次清理最多删除的会话目录数，避免首次运行时一次性删除上千目录卡住事件循环
    SESSION_PRUNE_BATCH = int(os.environ.get("AI_AGENT_SESSION_PRUNE_BATCH", "500"))

    def prune_sessions(self):
        """按「会话内文件最新 mtime」清理 sessions/ 下超过 TTL 的会话目录。

        关键：目录 mtime 只在创建/删除/重命名子项时更新，追加写入 journal
        不会刷新目录 mtime。因此不能用目录 mtime 判断活跃时间（活跃会话
        也会被误判为过期）。这里改为取会话目录内所有文件（sessions.json /
        sessions.journal.jsonl / meta.json）的最新 mtime 作为最近活动时间。

        单批次上限 SESSION_PRUNE_BATCH，剩余留到下一轮。
        """
        ttl = self.SESSION_TTL
        if ttl <= 0:
            return 0
        sessions_dir = os.path.join(_base_dir(), "sessions")
        if not os.path.isdir(sessions_dir):
            return 0
        now = time.time()
        removed = 0
        skipped = 0
        try:
            dirs = [d for d in os.listdir(sessions_dir)
                     if os.path.isdir(os.path.join(sessions_dir, d))]
        except Exception:
            logger.warning("[ChatJob] 枚举 sessions/ 失败", exc_info=True)
            return 0
        # 按「会话内文件最新 mtime」升序：旧会话优先清理
        def _latest_mtime(d):
            spath = os.path.join(sessions_dir, d)
            latest = 0.0
            try:
                for name in os.listdir(spath):
                    fp = os.path.join(spath, name)
                    if os.path.isfile(fp):
                        latest = max(latest, os.path.getmtime(fp))
            except OSError:
                pass
            return latest
        try:
            dirs.sort(key=_latest_mtime)
        except Exception:
            logger.warning("[ChatJob] 统计会话 mtime 失败", exc_info=True)
            return 0
        for d in dirs:
            try:
                if now - _latest_mtime(d) < ttl:
                    continue
                import shutil as _shutil
                _shutil.rmtree(os.path.join(sessions_dir, d))
                removed += 1
                if removed >= self.SESSION_PRUNE_BATCH:
                    skipped = len(dirs) - removed
                    break
            except FileNotFoundError:
                continue
            except Exception:
                logger.warning("[ChatJob] 清理会话目录失败: %s", d, exc_info=True)
        if removed:
            logging.info(f"[ChatJob] 清理过期会话目录 {removed} 个"
                         + (f"，剩余 {skipped} 个待下一轮" if skipped else ""))
        return removed





# 模块级单例：web_server 与 agent_chat_flow 共享同一任务管理器实例。
# 实例化不依赖路径（__init__ 只用 dict/RLock），路径由 web_server 注入 provider。
chat_jobs = ChatJobManager()
