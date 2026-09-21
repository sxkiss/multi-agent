"""
@input: json, os, threading, time, uuid, typing
@output: MemoryManager — 会话历史原子读写，损坏自动备份
@position: Memory layer — 持久化每条会话的 message/event 记录
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# 追加日志累积到该字节数后合并回快照（sessions.json）。
# 只在 add_message 里检查，压缩一次 ~88ms(5.7MB)，按 256KB 触发摊薄到可忽略。
JOURNAL_COMPACT_BYTES = 256 * 1024

# 每个会话文件一把进程内锁：web_server 对同一会话可并行跑多个 chat job（各持独立的
# MemoryManager 实例），不加锁时 last-writer-wins 会互相覆盖丢消息
_session_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _get_session_lock(file_path: str) -> threading.RLock:
    with _locks_guard:
        if file_path not in _session_locks:
            _session_locks[file_path] = threading.RLock()
        return _session_locks[file_path]


class MemoryManager:
    def __init__(self, session_id: str, sessions_dir: str = "sessions", sliding_window_size: int = 10):
        self.session_id = session_id
        self.sliding_window_size = sliding_window_size

        self.session_dir = os.path.join(sessions_dir, session_id)
        self.file_path = os.path.join(self.session_dir, "sessions.json")
        # 增量追加日志：新消息只 append 这一行，避免每次全量重写 sessions.json
        self.journal_path = os.path.join(self.session_dir, "sessions.journal.jsonl")
        self._lock = _get_session_lock(os.path.abspath(self.file_path))
        self.history: list[dict[str, Any]] = []
        # 快照解析缓存：sessions.json 解析 5.7MB 需 ~65ms，而 add_message 每次都要重读
        # （并行 job 各自持独立实例）。按 (inode, mtime_ns, size) 判断文件是否变化，
        # 未变则复用上次解析结果，把重复读取从 65ms 降到 ~0ms。
        # os.replace 会换 inode，故文件被任何写者替换后缓存必然失效。
        self._snap_cache_key: tuple | None = None
        self._snap_cache: list[dict[str, Any]] = []
        self._ensure_sessions_dir()
        self.load_session()

    def _ensure_sessions_dir(self):
        if not os.path.exists(self.session_dir):
            os.makedirs(self.session_dir)

    def _read_journal_locked(self) -> list[dict[str, Any]]:
        """读取追加日志中的增量消息。坏行跳过而非丢整个文件，避免一条脏数据毁掉全部增量。"""
        if not os.path.exists(self.journal_path):
            return []
        out = []
        try:
            with open(self.journal_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (ValueError, TypeError):
                        # 单行损坏（如进程被杀写到一半）只丢这一条，保留其余
                        logger.warning("journal 行损坏已跳过: %s", line[:80], exc_info=True)
        except OSError:
            logger.warning("journal 读取失败，回退为快照", exc_info=True)
            return []
        return out

    def _clear_journal_locked(self):
        try:
            if os.path.exists(self.journal_path):
                os.remove(self.journal_path)
        except OSError:
            logger.warning("journal 清理失败，下次加载可能重复", exc_info=True)

    def _load_snapshot_locked(self) -> list[dict[str, Any]]:
        """读取并解析 sessions.json 快照，未变化则复用缓存。"""
        try:
            st = os.stat(self.file_path)
        except OSError:
            self._snap_cache_key = None
            self._snap_cache = []
            return []
        key = (st.st_ino, st.st_mtime_ns, st.st_size)
        if self._snap_cache_key == key:
            return self._snap_cache
        snap = []
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                snap = json.load(f)
        except Exception:
            # 文件损坏（写入中断等）：备份后从空历史开始，避免永久无法加载
            try:
                os.replace(self.file_path, self.file_path + ".corrupt")
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)
            snap = []
        self._snap_cache_key = key
        self._snap_cache = snap
        return snap

    def load_session(self):
        with self._lock:
            # 由于有全局锁，这里直接拿锁重新从磁盘读，确保并行 job 看到最新历史
            self._load_session_locked()

    def _load_session_locked(self):
        # 完整历史 = 快照(带缓存) + 增量日志(小，全量重读仅 ~0ms)
        snap = self._load_snapshot_locked()
        self.history = list(snap) + self._read_journal_locked()

    def save_session(self):
        # 原子写：先写临时文件再替换，防止进程中断产生半个/空的历史文件
        with self._lock:
            self._save_session_locked()

    def _save_session_locked(self):
        tmp_path = self.file_path + ".tmp"
        try:
            # 不加 indent：会话历史是机器读取的持久化文件，缩进只为人工可读。
            # 实测（2848 条 / 5.7MB 会话）：indent=2 写 99ms → 紧凑 88ms，省约 11%。
            # 历史越长收益越明显，且文件体积不增。（separators=(',',':') 实测略慢于默认，故不采用）
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False)
            os.replace(tmp_path, self.file_path)
            # 快照已包含全部历史，增量日志可丢弃
            self._clear_journal_locked()
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)

    def _compact_if_needed_locked(self):
        """日志过大时合并回快照并清空日志，避免日志无限增长拖慢每次加载。"""
        try:
            if os.path.exists(self.journal_path) and \
                    os.path.getsize(self.journal_path) >= JOURNAL_COMPACT_BYTES:
                self._save_session_locked()
        except OSError:
            logger.warning("journal 大小检查失败，跳过压缩", exc_info=True)

    def add_message(self, role: str, content: str | list[dict[str, Any]], id: str | None = None, **kwargs):
        with self._lock:
            # 每次都先从磁盘重读最新历史，避免并行 job 的实例间互相覆盖
            self._load_session_locked()
            msg = {
                "id": id if id else str(uuid.uuid4()),
                "role": role,
                "content": content,
                "timestamp": time.time(),
                **kwargs
            }
            self.history.append(msg)
            # 原为全量重写 sessions.json（O(N²)：2848 条时 188ms/次，单轮 6 次 ≈ 1.1s）。
            # 改为只 append 一行到 journal（O(1)，≈0.3ms），并在日志超阈值时才合并回快照。
            try:
                # 只 flush 不 fsync：与原有实现（json.dump + os.replace，同样未 fsync）
                # 保持一致的持久化语义——进程崩溃不丢（已在页缓存），机器掉电才可能丢。
                # 实测 fsync 每次多花 1.96ms（flush 仅 0.008ms），对本场景不值。
                with open(self.journal_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
                    f.flush()
            except OSError:
                # 追加失败不能丢消息：退回全量写，保证语义不变
                logger.warning("journal 追加失败，回退全量写", exc_info=True)
                self._save_session_locked()
            self._compact_if_needed_locked()
            return msg
    
    def _split_into_rounds(self) -> list[list[dict[str, Any]]]:
        """
        将历史消息分割为对话轮次。
        一轮对话定义为：从一个 'user' 消息开始，包含随后的所有 'assistant'/'tool' 消息，
        直到遇到下一个 'user' 消息或历史结束。
        """
        rounds = []
        current_round = []
        
        for msg in self.history:
            # 保留 reasoning_content：thinking 模式需要它随 tool_calls 一起回传
            clean_msg = msg.copy()

            if clean_msg['role'] == 'user':
                if current_round:
                    rounds.append(current_round)
                current_round = [clean_msg]
            else:
                # 兼容性：如果历史记录不是以 user 开头（罕见），也归入当前轮次（或创建新轮次）
                if not current_round and not rounds:
                    # 孤立的非 user 消息，作为第一轮
                    current_round = [clean_msg]
                else:
                    current_round.append(clean_msg)
        
        if current_round:
            rounds.append(current_round)
        
        return rounds
    
    def get_sliding_window(self) -> list[dict[str, Any]]:
        """
        返回可见窗口消息。
        若存在压缩摘要（参考 opencode: 摘要伪装成 assistant 消息 + SummaryMessageID 截断），
        则从该摘要处截断，旧消息逻辑隐藏（仍在 history 文件中，但不再进入窗口，因此不物理删除）。
        若无摘要，则回退为原滑动窗口逻辑：返回最后 N 轮对话。
        """
        with self._lock:
            self._load_session_locked()
            # 查找压缩摘要消息下标（取最后一个/最新的，对应 opencode 的 SummaryMessageID）
            summary_idx = self._find_summary_index_locked()
            if summary_idx is not None:
                window = self.history[summary_idx:]
                # 参考 opencode: 将摘要消息的角色从 assistant 转为 user
                # 这样模型能看到完整的上下文：摘要(user) + 最近对话
                if window and window[0].get("role") == "assistant" and window[0].get("is_summary"):
                    window[0] = window[0].copy()
                    window[0]["role"] = "user"
                return window

            # 无摘要时按轮次滑动窗口
            rounds = self._split_into_rounds()
            last_n_rounds = rounds[-self.sliding_window_size:]
            window_messages = []
            for r in last_n_rounds:
                window_messages.extend(r)
            return window_messages

    def _find_summary_index(self) -> int | None:
        with self._lock:
            self._load_session_locked()
            return self._find_summary_index_locked()

    def _find_summary_index_locked(self) -> int | None:
        """
        返回最新压缩摘要消息在 history 中的下标（参考 opencode: SummaryMessageID）。
        旧消息逻辑隐藏：它们仍保留在 history 文件中，但从该下标起才进入可见窗口。
        """
        idx = None
        for i, m in enumerate(self.history):
            content = m.get("content")
            if isinstance(content, str) and content.startswith("[自动压缩的历史摘要]"):
                idx = i  # 取最后一个（最新的）摘要
        return idx

    def get_full_history(self) -> list[dict[str, Any]]:
        with self._lock:
            self._load_session_locked()
            return list(self.history)

    def get_total_rounds(self) -> int:
        with self._lock:
            self._load_session_locked()
            return len(self._split_into_rounds())

    def remove_messages(self, ids: list[str]):
        """按 id 批量删除历史消息并保存"""
        if not ids:
            return
        with self._lock:
            self._load_session_locked()
            id_set = set(ids)
            self.history = [m for m in self.history if m.get("id") not in id_set]
            self._save_session_locked()

    def insert_compressed_summary(self, summary_content: str, after_ids: list[str] | None = None) -> str:
        """
        插入一条压缩摘要（参考 opencode: 摘要伪装成 assistant 消息，
        SummaryMessageID 截断，旧消息逻辑隐藏不物理删除）。
        - 角色为 assistant（兼容 opencode 续接会话语义）
        - after_ids: 被压缩的早期消息 id 列表，摘要插入到它们之后（紧邻最近一轮之前）
        - 返回摘要消息 id（对应 opencode 的 SummaryMessageID）
        """
        with self._lock:
            self._load_session_locked()
            mid = str(uuid.uuid4())
            msg = {
                "id": mid,
                "role": "assistant",
                "content": f"[自动压缩的历史摘要]\n{summary_content}",
                "timestamp": time.time(),
                "is_summary": True,
            }
            if after_ids:
                aid = set(after_ids)
                pos = 0
                for i, m in enumerate(self.history):
                    if m.get("id") in aid:
                        pos = i + 1
                self.history.insert(pos, msg)
            else:
                self.history.insert(0, msg)
            self._save_session_locked()
            return mid
