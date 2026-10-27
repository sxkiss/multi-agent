"""
@input: functools, inspect, json, logging, os, re, threading, time, typing
@output: ToolRegistry 单例、register_tool 装饰器、危险命令黑名单、权限上下文
@position: Tools core — 统一注册/校验/护栏/审计，所有工具通过此模块注册
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import functools
import inspect
import json
import logging
import uuid

logger = logging.getLogger(__name__)
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, get_type_hints

from .base import _xml_response, inject_tool_name, inject_duration, tool_result_for_model  # noqa: F401  (工具结果统一封装)

# 项目根目录（chat_client 的上一级），作为所有工具的默认工作目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 上游协议（OpenAI / sub2api 透传）对工具名有硬性规则：
# 只允许 a-z A-Z 0-9 _ -，其他字符（如 MCP 工具的点号 doc.copy_format）会触发
# "invalid name" 400，导致多轮对话 500「不符合 API 协议」。
# 这里把非法字符清洗为下划线，保证对外暴露名合规且稳定可还原。
_ALLOWED_TOOL_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_LEADING_NON_ALPHA = re.compile(r"^[^a-zA-Z]+")


def sanitize_tool_name(name: str) -> str:
    """把工具名清洗成上游协议允许的形状（点号等非法字符 -> 下划线）。"""
    if not name:
        return name
    cleaned = _ALLOWED_TOOL_NAME_CHARS.sub("_", name)
    # 首字符必须是字母，避免 _123 这类无法作为标识符的情况
    if _LEADING_NON_ALPHA.match(cleaned):
        cleaned = "t_" + cleaned
    return cleaned


# ============================================================
# 工具下发策略（Tool Disclosure Policy）
# 背景：单 Agent 模式默认把全部工具下发，工具数多时首 token 延迟偏高
#   1. CRITICAL_TOOLS：核心工具，永不被裁剪（保证基础能力）
#   2. 条件门控（gated）：按配置/环境启用的工具，默认不下发，需显式开启
#   3. 其余工具：正常下发
# 通过环境变量 AI_AGENT_TOOL_DISCLOSURE=trim 开启裁剪（默认 off = 全量，保持向后兼容）。
# 通过 AI_AGENT_TOOL_EXTRA_GATE 追加门控工具（逗号分隔）。
# ============================================================
CRITICAL_TOOLS: frozenset[str] = frozenset({
    "Read", "Glob", "Grep", "Write", "Edit", "Patch", "Bash",
    "Task", "TodoWrite", "Skills", "CheckCommandStatus", "StopCommand",
    "WebFetch", "WebSearch",
})

# 条件门控工具：默认不下发，需通过 config["tool_gate_enabled"] 或环境变量显式开启。
# 分组便于按需开启（如只开媒体组）。
GATED_TOOL_GROUPS: dict[str, frozenset[str]] = {
    # 媒体/多模态
    "media": frozenset({"WechatMedia", "SendMedia", "ImageGenerate"}),
    # 数据库
    "database": frozenset({"MySQLQuery", "SQLQuery", "DatabaseQuery"}),
    # 知识库（文档 RAG）
    "knowledge": frozenset({"KnowledgeAdd", "KnowledgeSearch", "KnowledgeList", "KnowledgeDelete"}),
    # 记忆
    "memory": frozenset({"mem0_search", "mem0_add", "mem0_list", "mem0_delete", "mem0_import",
                         "Mem0Search", "Mem0Add"}),
    # 市场发现
    "market": frozenset({"ExpertMarket", "GetExpertPrompt", "InstallExpert",
                         "SkillMarket", "SkillCategories", "ExpertScenes"}),
    # 定时任务
    "scheduler": frozenset({"ScheduleTask", "ListScheduledTasks", "CancelScheduledTask",
                            "RunScheduledTaskNow"}),
    # 网站/运维
    "website": frozenset({"SiteList", "SiteConf", "SiteLogs", "SiteAnalysis", "ServerStatus",
                          "FirewallStatus", "DockerInfo", "DockerContainers"}),
}


def get_gated_tools(enabled_groups: set[str] | None = None,
                    extra: set[str] | None = None) -> set[str]:
    """返回当前应被门控（不下发）的工具集合。

    enabled_groups：显式启用的门控组名（这些组**不**被门控，正常下发）。
    extra：额外门控的工具名（无条件门控）。
    """
    enabled_groups = enabled_groups or set()
    gated: set[str] = set()
    for grp, tools in GATED_TOOL_GROUPS.items():
        if grp not in enabled_groups:
            gated |= tools
    if extra:
        gated |= extra
    return gated - CRITICAL_TOOLS  # 核心工具永不被门控


def disclosure_mode() -> str:
    """当前工具下发模式：'trim'（裁剪）或 'full'（全量，默认）。"""
    return os.environ.get("AI_AGENT_TOOL_DISCLOSURE", "").strip().lower() or "full"


# ============================================================
# 后台任务管理器：所有工具超时后自动转入后台执行
# 对齐终端工具的 CommandManager，但覆盖范围是全部 66 个工具
# ============================================================
class _BgTask:
    """单条后台任务的状态数据"""
    __slots__ = ("task_id", "tool", "args", "future", "status", "result",
                 "start_time", "session_id", "parent_job_key")

    def __init__(self, task_id: str, tool: str, args: tuple, future,
                 session_id: str = "", parent_job_key: str = ""):
        self.task_id = task_id
        self.tool = tool
        self.args = args
        self.future = future
        self.status = "pending"      # pending | queued | running | done | failed | stopped
        self.result: str = ""
        self.start_time = time.time()
        self.session_id = session_id
        self.parent_job_key = parent_job_key


# ============================================================
# 持久化仓库：进程重启后恢复任务状态
# ============================================================
def _recover_running_tasks() -> list[dict]:
    """从 SQLite 恢复 running/pending/queued 状态的任务
    
    Returns:
        [{"task_id", "tool", "args", "session_id", "start_time"}, ...]
    """
    try:
        from .bg_repo import get_repo
        repo = get_repo()
        rows = repo.restore_running()
        result = []
        for row in rows:
            try:
                args = json.loads(row.args_json) if row.args_json else ()
            except Exception:
                args = ()
            result.append({
                "task_id": row.task_id,
                "tool": row.tool,
                "args": args if isinstance(args, tuple) else tuple(args),
                "session_id": row.session_id,
                "start_time": row.start_time,
            })
        logger.info("[Background] 恢复 %d 个未完成任务", len(result))
        return result
    except Exception as e:
        logger.warning("[Background] 恢复任务失败: %s", e)
        return []


class BackgroundTaskManager:
    """统一后台任务注册/查询/完成通知。

    设计要点：
    - 任务一旦注册就**不允许取消**：Python 线程池的 future.cancel() 对已
      运行的 task 无效（文档明确说返回 False），强行 cancel 只会让线程继续
      跑但结果被丢弃。不如直接纳入管理，让用户能查结果。
    - 完成通知通过 job.append 推 SSE 事件（同终端工具的通知链路）。
    - 内存上限 200 条，超时就淘汰最旧的；长时间任务可能撑爆内存就靠这个兜底。
    """
    def __init__(self):
        self._tasks: dict[str, _BgTask] = {}
        self._lock = threading.Lock()
        self._MAX = 200
        # 每个 session 一个 wake event：后台任务完成时 set()，唤醒主循环收尾段的
        # 挂起等待（取代旧版 "轮询 + 过早终止"）。避免主循环在后台任务未跑完时
        # 就 yield stop 终止会话、导致结果只推 SSE 进不了上下文。
        self._wake_events: dict[str, threading.Event] = {}
        # 孤儿结果回调：后台任务完成时，若父会话的主循环已经结束（本轮 message_end
        # 之后才完成的），结果就没有任何消费者——只会推 SSE、永远进不了对话。
        # 表现是"上一轮说结果会自动回来，下一轮却什么都没有"。
        # 由 web_server 在启动时注册：收到回调后自行判断该会话有无 running job，
        # 没有就用该结果发起一轮新的对话，把结果主动推给模型。
        self._orphan_callback = None
        # 判定延迟（秒）：给主循环留出 drain 的时间窗口。主循环正常运行时
        # 会在 1 秒内完成注入并把状态推进到 ready，不会误触发。
        self._orphan_delay = float(os.environ.get("AI_AGENT_ORPHAN_DELAY", "6"))

    def set_orphan_callback(self, callback) -> None:
        """注册孤儿结果回调。

        callback(session_id: str, task_id: str, tool: str, status: str, result: str)
        仅在"任务已完成但迟迟无人 drain"时调用（说明父会话本轮已结束）。
        """
        self._orphan_callback = callback

    def _schedule_orphan_check(self, task_id: str) -> None:
        """延迟检查：任务完成后若状态仍非 ready，判定为孤儿结果并回调。

        为什么要延迟而不是立刻判断：正常路径下主循环会先 drain（状态变为 ready），
        但这个 drain 发生在子代理回调线程之后，立刻判断必然误报。
        """
        def _check():
            try:
                with self._lock:
                    t = self._tasks.get(task_id)
                    if t is None:
                        return
                    if t.status not in ("done", "failed"):
                        return
                    _sid, _tool, _status, _res = (
                        t.session_id, t.tool, t.status, t.result)
                if not _sid:
                    return
                cb = self._orphan_callback
                if cb is None:
                    return
                logger.info(
                    "[Background] 检测到孤儿结果（无人消费），触发主动回调 "
                    "task=%s tool=%s session=%s", task_id[:12], _tool, _sid[:24])
                cb(_sid, task_id, _tool, _status, _res)
            except Exception:
                logger.warning("[Background] 孤儿结果回调失败 task=%s", task_id, exc_info=True)

        timer = threading.Timer(max(1.0, self._orphan_delay), _check)
        timer.daemon = True
        timer.start()

    def register(self, task_id: str, tool: str, args: tuple,
                 future, session_id: str, parent_job_key: str = "") -> None:
        with self._lock:
            if len(self._tasks) >= self._MAX:
                # LRU：淘汰最早注册的
                oldest = min(self._tasks, key=lambda k: self._tasks[k].start_time)
                self._tasks.pop(oldest, None)
            self._tasks[task_id] = _BgTask(task_id, tool, args, future, session_id, parent_job_key)
        # 持久化：确保进程重启后能恢复
        try:
            from .bg_repo import get_repo
            get_repo().insert(task_id, tool, args, "pending", session_id)
        except Exception as e:
            logger.warning("[Background] 持久化任务失败 task=%s: %s", task_id[:12], e)

    def mark_queued(self, task_id: str) -> bool:
        """pending -> queued，通过状态机校验。"""
        from .bg_repo import STATUS_QUEUED
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return False
            t.status = "queued"
        return self._repo_transition(task_id, STATUS_QUEUED)

    def mark_running(self, task_id: str) -> bool:
        """queued -> running，通过状态机校验。"""
        from .bg_repo import STATUS_RUNNING
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return False
            t.status = "running"
        return self._repo_transition(task_id, STATUS_RUNNING)

    def _repo_transition(self, task_id: str, new_status: str) -> bool:
        """调用 repo.transition() 校验并推进状态。"""
        try:
            from .bg_repo import get_repo
            return get_repo().transition(task_id, new_status)
        except Exception as e:
            logger.warning("[Background] repo_transition 失败 task=%s: %s", task_id[:12], e)
            return False

    def cancel(self, task_id: str, reason: str = "") -> bool:
        """标记任务为 cancelled（不强制杀线程，仅状态层面终止消费）。

        与 register() 的"不允许取消"原则一致：线程仍在跑完，但结果不再注入
        对话、不再触发孤儿回调。用于用户主动放弃任务的场景。
        """
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return False
            if t.status in ("done", "failed", "stopped"):
                return False
            t.status = "stopped"
            t.result = f"[已取消] {reason}" if reason else "[已取消]"
        try:
            from .bg_repo import get_repo, STATUS_CANCELLED
            get_repo().transition(task_id, STATUS_CANCELLED, reason)
        except Exception as e:
            logger.warning("[Background] 持久化取消状态失败 task=%s: %s", task_id[:12], e)
        self._wake(t.session_id)
        return True

    def get(self, task_id: str) -> _BgTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def remove(self, task_id: str) -> None:
        with self._lock:
            self._tasks.pop(task_id, None)

    def get_wake_event(self, session_id: str) -> threading.Event:
        """取（或惰性创建）本会话的 wake event；主循环收尾段挂起等待它。"""
        with self._lock:
            ev = self._wake_events.get(session_id)
            if ev is None:
                ev = threading.Event()
                self._wake_events[session_id] = ev
            return ev

    def _wake(self, session_id: str) -> None:
        """唤醒该会话挂起的主循环（若已存在 wake event）。"""
        with self._lock:
            ev = self._wake_events.get(session_id)
        if ev is not None:
            ev.set()

    def _persist_result(self, task_id: str, tool: str, status: str, result: str) -> None:
        """把完整结果落盘到 jobs/<key>.jsonl，进程重启后仍能回查（旧版只存 500 字预览）。"""
        try:
            job = getattr(_current_job_local, "job", None)
            if job is not None and hasattr(job, "append"):
                job.append("background_result", {
                    "task_id": task_id,
                    "tool": tool,
                    "status": status,
                    "result": result,
                })
        except Exception:
            logger.warning("后台任务结果落盘失败 task=%s", task_id, exc_info=True)

    def notify_done(self, task_id: str, result: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t.status = "done"
            t.result = result
            _sid = t.session_id
        # 1) 完整结果落盘：进程重启后仍能回查（旧版 emit_progress 仅推 500 字预览）
        self._persist_result(task_id, t.tool, "done", result)
        # 2) 持久化到 SQLite（状态机：running -> done）
        try:
            from .bg_repo import get_repo, STATUS_DONE
            _repo = get_repo()
            _repo.transition(task_id, STATUS_DONE)
            _repo.set_result(task_id, result, with_status=False)
        except Exception as e:
            logger.warning("[Background] 持久化结果失败 task=%s: %s", task_id[:12], e)
        # 3) 推 SSE 预览事件，前端可见状态
        try:
            emit_progress("background_done", {
                "task_id": task_id,
                "tool": t.tool,
                "result_preview": result[:500] if result else "",
            })
        except Exception:
            logger.warning("后台任务完成通知失败 task=%s", task_id, exc_info=True)
        # 4) 唤醒挂起的主循环收尾段，使其 drain_finished 注入结果后正常收尾
        self._wake(_sid)
        # 5) 孤儿回调（兜底）：若主循环已结束，本轮不会再 drain，结果会"飞掉"。
        #    延迟检查，等主循环的 drain 窗口结束；仍未 ready 则主动回调。
        #    同时记录完成时间，供孤儿回调判断是否在"等待窗口"内追加到父 job。
        self._task_done_at = getattr(self, '_task_done_at', {})
        self._task_done_at[task_id] = time.monotonic()
        self._schedule_orphan_check(task_id)

    def notify_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if not t:
                return
            t.status = "failed"
            t.result = f"[执行失败] {error}"
            _sid = t.session_id
        self._persist_result(task_id, t.tool, "failed", t.result)
        # 持久化失败状态（状态机：running -> failed）
        try:
            from .bg_repo import get_repo, STATUS_FAILED
            _repo = get_repo()
            _repo.transition(task_id, STATUS_FAILED)
            _repo.set_result(task_id, t.result, with_status=False)
        except Exception as e:
            logger.warning("[Background] 持久化失败状态 task=%s: %s", task_id[:12], e)
        try:
            emit_progress("background_done", {
                "task_id": task_id,
                "tool": t.tool,
                "result_preview": t.result[:500],
            })
        except Exception:
            logger.warning("后台任务失败通知失败 task=%s", task_id, exc_info=True)
        self._wake(_sid)
        # 孤儿回调同样适用于失败结果
        self._task_done_at = getattr(self, '_task_done_at', {})
        self._task_done_at[task_id] = time.monotonic()
        self._schedule_orphan_check(task_id)

    def get_parent_job_key(self, task_id: str) -> str:
        """查询后台任务派发时的父 job key，供孤儿回调精确判断。"""
        with self._lock:
            t = self._tasks.get(task_id)
            return t.parent_job_key if t else ""

    def pending_count(self, session_id: str) -> int:
        """本会话中仍在运行、且结果尚未注入上下文的后台任务数。

        主循环用它判断"能否结束本轮"：若还有任务在跑就结束，结果就永远
        回不到上下文（完成通知只推 SSE），等于把后台任务做成了黑盒。
        """
        with self._lock:
            return sum(
                1 for t in self._tasks.values()
                if t.session_id == session_id and t.status == "running"
            )

    def drain_finished(self, session_id: str, max_chars: int = 8000) -> list[dict]:
        """取出本会话中"已完成但尚未注入对话"的后台任务结果。

        为什么需要：完成通知走 emit_progress → job.append → SSE，只推到前端
        界面，不会进入 Agent 的 messages，模型因此拿不到结果——表现就是
        "转后台后结果永远回不来，只能靠 TaskStatus 主动轮询"。

        主循环每轮迭代调用一次，把结果以 system 消息注入上下文，模型便能
        在下一轮直接读到。状态机推进到 ready 保证同一条结果只注入一次。

        @param max_chars 单条结果截断上限，防止超长输出撑爆上下文
        @returns [{"task_id","tool","status","result"}, ...]
        """
        out = []
        with self._lock:
            for t in self._tasks.values():
                # 只取本会话的：跨会话注入会造成信息泄漏与上下文污染
                if t.session_id != session_id:
                    continue
                if t.status not in ("done", "failed"):
                    continue
                # 检查 DB 状态：已 ready 说明已被消费过
                try:
                    from .bg_repo import get_repo, STATUS_READY
                    row = get_repo().get(t.task_id)
                    if row and row.status == STATUS_READY:
                        continue
                except Exception:
                    pass
                result = t.result or ""
                if len(result) > max_chars:
                    result = result[:max_chars] + f"\n[...结果过长已截断，共 {len(t.result)} 字符，完整结果可用 TaskStatus 查询...]"
                out.append({
                    "task_id": t.task_id,
                    "tool": t.tool,
                    "status": t.status,
                    "result": result,
                    # 后台任务总耗时（秒，保留两位小数）：从注册到完成
                    "duration_s": round(time.time() - t.start_time, 2),
                })
        # Y2 修复：状态机推进要在同一锁内原子完成，防止多线程重复注入
        if out:
            try:
                from .bg_repo import get_repo, STATUS_READY
                _repo = get_repo()
                # 先检查再更新：在锁内原子完成 check-then-act
                to_transition = []
                for item in out:
                    row = _repo.get(item["task_id"])
                    if row and row.status == STATUS_READY:
                        continue
                    to_transition.append(item["task_id"])
                # 批量推进状态机
                for task_id in to_transition:
                    _repo.transition(task_id, STATUS_READY)
            except Exception as e:
                logger.warning("[Background] 推进状态到 ready 失败: %s", e)
        return out


_BG = BackgroundTaskManager()


def get_background_manager() -> BackgroundTaskManager:
    """取全局后台任务管理器（供 Agent 主循环注入完成结果，避免直接引用私有名）"""
    return _BG


def recover_running_tasks() -> list[dict]:
    """恢复进程重启时未完成的后台任务（供 web_server 启动时调用）"""
    return _recover_running_tasks()
# - 键：session_id（来自 ChatJob.session_id）
# - 值：是否放行高风险工具（true 表示非严格/破甲模式成员代理）
# - 无 job/session 上下文时保守返回 False，避免跨会话泄漏
# ============================================================
_session_allow_high: dict[str, bool] = {}
_allow_high_lock = threading.Lock()


def set_session_allow_high(session_id: str, enabled: bool) -> None:
    """设置指定会话的工具权限上下文（在 chat() 进入/退出时由 Agent 调用）"""
    with _allow_high_lock:
        if enabled:
            _session_allow_high[session_id] = True
        else:
            _session_allow_high.pop(session_id, None)


# 兼容旧调用名（agent.py 迁移用），新代码优先调用 set_session_allow_high
set_thread_allow_high = set_session_allow_high


def _current_session_id() -> str | None:
    """从当前任务上下文推导 session_id；无任务上下文返回 None"""
    job = getattr(_current_job_local, "job", None)
    if job is None:
        return None
    return getattr(job, "session_id", None)


def is_allow_high() -> bool:
    """查询当前执行上下文中是否允许高风险工具。

    逻辑：从 _current_job_ref 取 session_id → 查会话字典。
    - 无 job / 无 session_id：保守返回 False（拦截所有高风险命令）
    - 已注册 True：放行；未注册或显式 False：拦截
    """
    sid = _current_session_id()
    if not sid:
        return False
    with _allow_high_lock:
        return _session_allow_high.get(sid, False)


# ============================================================
# 三档：ask（默认，高风险需确认）/ allow_all（破甲，放行全部）/
#       allow_tools（放行白名单内的高风险工具）
# 独立于 _session_allow_high（后者由 strict_tools 决定，是基线），
# HITL 是在基线之上由用户显式选择的会话级覆盖。
# ============================================================
_session_hitl: dict[str, dict[str, Any]] = {}
_hitl_lock = threading.Lock()

HITL_MODES = ("ask", "allow_all", "allow_tools")

# - 确认类工具永不跳过（即使用户开了 allow_all 也必须弹确认）
# - 工具名长度/数量校验，防注入超长名单
_HITL_NEVER_SKIP: frozenset[str] = frozenset({"AskUserQuestion", "ask_user_question"})
_HITL_MAX_TOOLS = 64
_HITL_MAX_TOOL_NAME = 128


def set_session_hitl(session_id: str, mode: str = "ask", tools: list[str] | None = None) -> None:
    """设置指定会话的 HITL 审批模式（ask/allow_all/allow_tools）。

    P7 硬保护：确认类工具（AskUserQuestion）永不进入白名单；工具名超长/超量
    """
    if not session_id:
        return
    mode = (mode or "ask").strip().lower()
    if mode not in HITL_MODES:
        mode = "ask"
    # 校验工具白名单：过滤确认类、超长、超量
    clean: set[str] = set()
    for t in (tools or []):
        name = str(t).strip()
        if not name or name in _HITL_NEVER_SKIP:
            continue
        if len(name) > _HITL_MAX_TOOL_NAME:
            continue
        if len(clean) >= _HITL_MAX_TOOLS:
            break
        clean.add(name)
    with _hitl_lock:
        if mode == "ask":
            _session_hitl.pop(session_id, None)
        else:
            _session_hitl[session_id] = {"mode": mode, "tools": clean}


def get_session_hitl(session_id: str) -> dict[str, Any]:
    """返回会话的 HITL 设置（默认 ask）。"""
    with _hitl_lock:
        cfg = _session_hitl.get(session_id)
        if not cfg:
            return {"mode": "ask", "tools": set()}
        return {"mode": cfg["mode"], "tools": set(cfg["tools"])}


def is_high_allowed(tool_name: str = "") -> bool:
    """综合判断当前会话是否放行指定工具的高风险执行。

    优先级：HITL allow_all > HITL allow_tools(命中) > 基线 allow_high。
    P7 硬保护：确认类工具（AskUserQuestion）永不跳过——即使 allow_all 也返回 False。
    """
    if tool_name and tool_name in _HITL_NEVER_SKIP:
        return False
    sid = _current_session_id()
    if sid:
        with _hitl_lock:
            cfg = _session_hitl.get(sid)
        if cfg:
            if cfg["mode"] == "allow_all":
                return True
            if cfg["mode"] == "allow_tools" and tool_name and tool_name in cfg["tools"]:
                return True
    return is_allow_high()


# ============================================================
# 线程安全的 job 上下文（替代原模块级 _current_job_ref）
# - web_server 的 chat 线程通过 set_current_job(job) 设置
# - 工具在线程池执行时，_guarded_execute 提交任务前捕获当前 job，
#   并在 worker 线程显式恢复，避免多会话并发时全局变量互相覆盖
# ============================================================
_current_job_local = threading.local()


def set_current_job(job):
    """在调用线程上设置当前 job（用于 emit_progress / is_allow_high 等）"""
    _current_job_local.job = job


def get_current_job():
    return getattr(_current_job_local, "job", None)


def _restore_job_ctx(job):
    """在 worker 线程中恢复 job 上下文"""
    if job is not None:
        _current_job_local.job = job


# ============================================================
# Token 用量上报：LLM 主循环收到 usage 后调用，随下一条审计记录落盘
# ============================================================
_pending_tokens = None


def report_token_usage(usage):
    """Token 用量上报入口（非阻塞，异常静默不影响主流程）"""
    global _pending_tokens
    try:
        if not usage:
            return
        inc = {"input": int(usage.get("input_tokens", 0) or 0),
               "output": int(usage.get("output_tokens", 0) or 0)}
        if _pending_tokens:
            _pending_tokens["input"] += inc["input"]
            _pending_tokens["output"] += inc["output"]
        else:
            _pending_tokens = inc
    except Exception:
        logger.warning("异常被静默吞掉，已记录", exc_info=True)


def emit_progress(event_name: str, data):
    """工具执行中实时推送进度事件到前端 SSE"""
    job = getattr(_current_job_local, "job", None)
    if job:
        job.append(event_name, data)


# ============================================================
# 危险命令黑名单（执行时强制拦截，所有工具的字符串参数都会被扫描）
# ============================================================
_DANGEROUS_CMD_PATTERNS = [
    r"rm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|$)",   # rm -rf /
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?[a-zA-Z]*\s+/[^/\w]",              # rm -r* /<非字母>
    r"mkfs(\.\w+)?\b",                                             # mkfs 格式化文件系统
    r"\bdd\b[^|;&]*\bof=/dev/(?:sd|vd|nvme|hd)",                    # dd 直接写块设备
    r":\(\)\s*\{.*\}\s*;\s*:",                                  # fork bomb
    r"\b(?:shutdown|halt|poweroff|reboot|init\s+[06])\b",           # 关机/重启类
    r">\s*/dev/sd[a-z]",                                            # 覆盖磁盘设备
    r"\bchmod\s+-R\s+777\s+/(?:\s|$)",                            # 递归 777 根目录
    r"\bwipefs\b",
    r"\bhistory\s+-c\b",
]


def _scan_dangerous_payload(kwargs: dict[str, Any]):
    """扫描工具参数中的高危命令特征，命中返回 (True, 描述)。

    P7（2026-10-15）：优先用**可编辑 YAML 护栏规则库**（chat_client/guard_rules.py，
    """
    import re as _re
    texts = []
    for v in kwargs.values():
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, (list, tuple)):
            texts.extend(str(i) for i in v if isinstance(i, str))
    payload = "\n".join(texts)
    # 1) YAML 护栏规则库（可编辑、可热加载）
    try:
        from ..guard_rules import get_guard_rules
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        blocked, rule = get_guard_rules(base_dir).scan(payload)
        if blocked:
            return True, f"{rule.get('id')}:{rule.get('description')}"
    except Exception:
        pass  # 规则库异常不阻断，回退硬编码
    # 2) 回退：硬编码黑名单（向后兼容）
    for pat in _DANGEROUS_CMD_PATTERNS:
        try:
            m = _re.search(pat, payload)
        except _re.error:
            continue
        if m:
            return True, pat
    return False, None


def _risk_denied_response(reason: str, tool_name: str = "") -> str:
    xml = (
        f"\n<tool>\n<toolcall_status>error</toolcall_status>\n"
        f"<toolcall_result>\n{reason}\n</toolcall_result>\n</tool>\n"
    )
    return inject_tool_name(xml, tool_name)


class ToolRegistry:
    # 状态持久化文件路径
    STATE_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 
        "tools_state.json"
    )

    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._schemas: list[dict[str, Any]] = []
        self._metadata: dict[str, dict[str, Any]] = {}
        self._states: dict[str, dict[str, Any]] = self._load_states()
        # 执行时风险护栏：默认开启，拦截 high 风险工具与危险命令黑名单
        # 可通过环境变量 AI_AGENT_RISK_GUARD=off 关闭严格拦截（黑名单始终生效）
        _env = os.environ.get("AI_AGENT_RISK_GUARD", "").strip().lower()
        # 默认关闭 high 拦截（自我进化需要）；安全由黑名单/白名单/参数校验保障
        # 设置 AI_AGENT_RISK_GUARD=on 可开启严格确认模式
        self.risk_guard_enabled = _env in ("1", "true", "on", "yes")
        # 统一执行超时（秒）：防止单个工具卡死整条任务；可被注册时 timeout 参数覆盖
        # 前台等待上限钳制在 [10, 120]：超过即转后台，绝不卡死前台上下文。
        # 优先级：AI_AGENT_TOOL_TIMEOUT 环境变量 → 内置默认 120。
        _t = os.environ.get("AI_AGENT_TOOL_TIMEOUT", "").strip()
        try:
            self.default_tool_timeout = self._clamp_timeout(int(_t)) if _t else 120
        except ValueError:
            self.default_tool_timeout = 120
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-exec")
        self.audit_log_path = os.environ.get(
            "AI_AGENT_TOOL_AUDIT",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "tool_audit.jsonl"),
        )
        self._audit_lock = threading.Lock()
        self._meta_lock = threading.Lock()

    # 前台等待上限：无论配置多长都不超过 120s，超过即转后台；下限 10s
    MIN_TOOL_TIMEOUT = 10
    MAX_TOOL_TIMEOUT = 120

    @classmethod
    def _clamp_timeout(cls, seconds) -> int:
        """把前台超时钳制到 [MIN_TOOL_TIMEOUT, MAX_TOOL_TIMEOUT]"""
        try:
            v = int(seconds)
        except (TypeError, ValueError):
            v = cls.MAX_TOOL_TIMEOUT
        return min(max(cls.MIN_TOOL_TIMEOUT, v), cls.MAX_TOOL_TIMEOUT)

    def set_tool_timeout(self, seconds) -> int:
        """设置工具前台等待上限（秒）。入参自动钳制到 [10, 120]。

        由 Agent 初始化时从配置注入（config.json 的 agent.tool_timeout）。
        超过该上限的工具调用不会被终止，而是转入后台继续执行。
        """
        self.default_tool_timeout = self._clamp_timeout(seconds)
        return self.default_tool_timeout

    def set_risk_guard(self, enabled: bool):
        """开/关高风险工具执行时拦截（危险命令黑名单不受此开关影响）"""
        self.risk_guard_enabled = bool(enabled)

    def get_tool_risk_level(self, name: str) -> str:
        meta = self._metadata.get(name)
        return meta.get("risk_level", "low") if meta else "low"

    def _validate_args(self, name: str, kwargs: dict[str, Any]) -> str | None:
        """按生成的 Schema 前置校验参数（类型/必填），返回错误说明或 None"""
        schema = next((s for s in self._schemas if s["function"]["name"] == name), None)
        if not schema:
            return None
        params = schema["function"].get("parameters", {})
        props = params.get("properties", {}) or {}
        required = params.get("required", []) or []

        missing = [r for r in required if r not in kwargs]
        if missing:
            return f"缺少必填参数: {', '.join(missing)}。请补齐后重新调用。"

        type_map = {"string": str, "integer": int, "number": (int, float),
                    "boolean": bool, "array": (list, tuple), "object": dict}
        for k, v in kwargs.items():
            spec = props.get(k)
            if not spec:
                continue  # 未知参数交给工具自行处理
            want = spec.get("type")
            py_t = type_map.get(want)
            if not py_t:
                continue
            ok = isinstance(v, py_t) and not (want in ("integer", "number") and isinstance(v, bool))
            if not ok:
                return (
                    f"参数 {k} 类型错误：应为 {want}，实际收到 {type(v).__name__}（值: {str(v)[:80]}）。"
                    f"请修正参数类型后重试。"
                )
        return None

    def _audit(self, tool_id: str, risk: str, kwargs: dict, duration_ms: int, status: str, result_head: str):
        """全链路审计留痕（JSONL，一行一次调用）"""

        def _redact(o):
            _H = ("api_key", "apikey", "access_key", "access-key", "secret",
                  "token", "password", "passwd", "credential", "private_key")
            if isinstance(o, dict):
                return {k: ("****" if any(h in str(k).lower() for h in _H) else _redact(v))
                        for k, v in o.items()}
            if isinstance(o, list):
                return [_redact(v) for v in o]
            return o

        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool_id,
                "risk": risk,
                "duration_ms": duration_ms,
                "status": status,
                "args": json.dumps(_redact(kwargs), ensure_ascii=False, default=str)[:500],
                "result_head": result_head[:200],
            }
            # 附加 token 用量（若 LLM 主循环有上报）；无则不写该字段，保持旧格式兼容
            global _pending_tokens
            if _pending_tokens:
                entry["tokens"] = dict(_pending_tokens)
                _pending_tokens = None
            os.makedirs(os.path.dirname(self.audit_log_path), exist_ok=True)
            # 简易轮转：超过 5MB 归档为 .1
            try:
                if os.path.exists(self.audit_log_path) and os.path.getsize(self.audit_log_path) > 5 * 1024 * 1024:
                    os.replace(self.audit_log_path, self.audit_log_path + ".1")
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)
            with self._audit_lock:
                with open(self.audit_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("异常被静默吞掉，已记录", exc_info=True)

    def _guarded_execute(self, func: Callable, *args, **kwargs):
        """工具执行统一入口：黑名单 → 风险拦截 → 参数校验 → 超时执行 → 审计"""
        name = func.__name__
        t0 = time.time()
        meta = self._metadata.get(name, {})
        tool_label = meta.get("id", name)
        risk = meta.get("risk_level", "low")
        audit_kwargs = dict(kwargs)

        # P3-b 可观测性：工具调用指标（延迟导入避免循环依赖）
        try:
            from ..metrics import default_persist_path, get_metrics
            # 用统一的路径解析：测试通过 BT_METRICS_DIR 隔离，避免污染生产统计
            _metrics = get_metrics(persist_path=default_persist_path())
        except Exception:
            _metrics = None

        # 1. 危险命令黑名单：仅集团模式（严格模式，is_allow_high()=False）下强制拦截
        #    单 Agent / Codex-X 破甲模式不拦截，放行专业操作
        if not is_allow_high():
            danger, pattern = _scan_dangerous_payload(kwargs)
            if danger:
                logger.warning("[RiskGuard] 工具 %s 命中危险命令黑名单 (%s)，已拦截", tool_label, pattern)
                self._audit(tool_label, risk, audit_kwargs, 0, "blocked-dangerous", pattern or "")
                if _metrics:
                    _metrics.record_tool(tool_label, (time.time() - t0) * 1000, "blocked-dangerous")
                return _risk_denied_response(
                    "此操作命中危险命令黑名单，已被安全策略拦截，拒绝执行。"
                    "请勿尝试绕过，请告知用户该操作被禁止。",
                    tool_name=tool_label,
                )

        # 2. high 风险工具：需用户确认；成员代理豁免（招聘即授权）
        #    HITL 三档覆盖：allow_all / allow_tools(命中) 直接放行。
        if (self.risk_guard_enabled and risk == "high"
                and not is_high_allowed(tool_label)):
            logger.warning("[RiskGuard] 拦截高风险工具调用: %s", tool_label)
            self._audit(tool_label, risk, audit_kwargs, 0, "blocked-high-risk", "")
            if _metrics:
                _metrics.record_tool(tool_label, (time.time() - t0) * 1000, "blocked-high-risk")
            return _risk_denied_response(
                f"此操作（{tool_label}）为高风险操作，执行前必须获得用户的明确确认。"
                "请停止调用该工具，向用户说明将要执行的操作内容与潜在风险，"
                "待用户明确同意后再重试；若用户已同意但工具仍被拦截，"
                "请联系管理员在面板设置或通过环境变量 AI_AGENT_RISK_GUARD=off 调整安全策略。",
                tool_name=tool_label,
            )

        # 3. medium 风险：放行并记录日志
        if risk == "medium":
            logger.info("[RiskGuard] 执行 medium 风险工具: %s", tool_label)

        # 4. 参数前置校验：不合法直接拦截并给出修正指引（不执行、不崩溃）
        verr = self._validate_args(name, kwargs)
        if verr:
            self._audit(tool_label, risk, audit_kwargs, 0, "invalid-params", verr)
            return _risk_denied_response(f"参数校验未通过：{verr}", tool_name=tool_label)

        # 5. 带超时执行：防止单个工具卡死整条任务
        # 无论全局默认还是注册时指定的 timeout，前台等待上限都钳制到 [10, 120]；
        # 超过即转后台，不被终止。
        _raw_to = meta.get("timeout") or self.default_tool_timeout
        timeout_s = self._clamp_timeout(_raw_to)
        started = time.time()
        # 捕获当前线程的 job，确保 worker 线程持有正确上下文（避免多会话并发时全局覆盖）
        ctx_job = getattr(_current_job_local, "job", None)
        try:
            def _wrapped():
                _restore_job_ctx(ctx_job)
                return func(*args, **kwargs)

            fut = self._executor.submit(_wrapped)
            try:
                result = fut.result(timeout=timeout_s)
            except _FutureTimeout:
                # 超时后不取消任务：Python 线程池的 cancel() 对已运行的 task
                # 无效（文档明确返回 False），取消只是丢弃 future 引用，
                # 线程仍在跑且无法回收。不如直接纳入后台管理——用户能通过
                # TaskStatus 查结果，且不会被白嫖算力而得不到反馈。
                task_id = str(uuid.uuid4())
                _sid = getattr(ctx_job, "session_id", "") if ctx_job else ""
                _BG.register(task_id, tool_label, args, fut, _sid)
                # 注册完成回调：无论 done/failed 都通知 job
                fut.add_done_callback(
                    lambda f, tid=task_id, t=tool_label: (
                        _BG.notify_done(tid, f.result())
                        if not f.cancelled() and f.exception() is None
                        else _BG.notify_failed(tid, str(f.exception()))
                    )
                )
                msg = (
                    f"工具 {tool_label} 已达到 {timeout_s} 秒前台等待上限，"
                    f"已自动转为后台执行。<task_id>{task_id}</task_id>\n"
                    f"【重要】该工具已在后台运行，禁止用相同参数重复调用它——"
                    f"重复调用会并发执行第二份，造成结果错乱与资源浪费。\n"
                    f"请先继续处理其他工作，但【不要结束当前会话/停止对话】——"
                    f"只有 AG 仍在本会话运行中，后台任务完成后结果才会自动回灌到对话上下文；"
                    f"若停止会话或断开连接，结果将无法回传，只能下次用 TaskStatus 查询。"
                )
                logger.info("[ToolBackground] %s 转后台 task=%s", tool_label, task_id)
                self._audit(tool_label, risk, audit_kwargs,
                            int((time.time() - started) * 1000), "background", task_id)
                # 转后台通知也带前台等待耗时（秒），模型可感知等待开销
                _bg_xml = _xml_response("running", msg)
                return inject_tool_name(inject_duration(_bg_xml, time.time() - started), tool_label)
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            self._audit(tool_label, risk, audit_kwargs, dur, "error", str(e))
            if _metrics:
                _metrics.record_tool(tool_label, dur, "error")
            raise

        dur = int((time.time() - t0) * 1000)
        self._audit(tool_label, risk, audit_kwargs, dur, "ok", str(result)[:200])
        if _metrics:
            _metrics.record_tool(tool_label, dur, "ok")
        # 注入本次工具调用耗时（秒）：沿用 Bash 既有 duration 约定，便于模型
        # 感知各工具开销；工具自带 duration 时（如 Bash）不覆盖。
        _elapsed = time.time() - t0
        return inject_tool_name(inject_duration(result, _elapsed), tool_label)

    def _load_states(self) -> dict[str, Any]:
        """从文件加载工具状态"""
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("工具状态文件损坏（%s），已回退空状态: %s", self.STATE_FILE, e)
            except OSError as e:
                logger.warning("读取工具状态文件失败（%s）: %s", self.STATE_FILE, e)
        return {}

    def _save_states(self):
        """将工具状态保存到文件（原子写：tmp + os.replace）"""
        tmp = self.STATE_FILE + ".tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._states, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.STATE_FILE)
        except OSError as e:
            logger.warning("写入工具状态文件失败（%s）: %s", self.STATE_FILE, e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        except (TypeError, ValueError) as e:
            # 状态对象含不可序列化字段（如 set/datetime），降级为尽力序列化
            logger.warning("工具状态含不可序列化字段，降级写入: %s", e)
            self._states = json.loads(json.dumps(self._states, ensure_ascii=False, default=str))
    
    def tool_exists(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tools
    
    def is_tool_enabled(self, name: str, enabled_ids: list[str]) -> bool:
        """
        检查工具是否在允许列表中。

        Args:
            name: 工具名称（函数名）
            enabled_ids: 允许使用的工具ID列表

        Returns:
            bool: 工具是否被允许使用
        """
        meta = self._metadata.get(name)
        if not meta:
            return False
        return meta["id"] in enabled_ids
    
    def get_tool_id(self, name: str) -> str | None:
        """获取工具的ID"""
        meta = self._metadata.get(name)
        if meta:
            return meta["id"]
        return None

    def register_tool(self, tool_id: str | Callable | type | None = None, **kwargs):
        """
        注册工具的装饰器。
        支持:
        @register_tool
        @register_tool("my_tool_id")
        @register_tool(id="my_tool_id")
        @register_tool(id="my_tool_id", category="system")
        @register_tool(id="my_tool_id", category="system", name_cn="系统服务")
        
        以及装饰类:
        @register_tool
        class MyTool:
            def execute(self, ...): ...
        """
        category = kwargs.get("category", "default")
        name_cn = kwargs.get("name_cn", "")
        risk_level = kwargs.get("risk_level", "low")
        tool_timeout = kwargs.get("timeout")  # 秒；None 用全局默认

        # 处理 id="xxx" 的关键字参数情况
        if tool_id is None and "id" in kwargs:
            tool_id = kwargs["id"]
        
        # 如果是类 (作为装饰器无参数直接使用 @register_tool)
        if inspect.isclass(tool_id):
            return self._register_class(tool_id, None, category, name_cn, risk_level, tool_timeout)

        # 如果是函数 (作为普通装饰器使用 @register_tool (无参数))
        if callable(tool_id):
            func = tool_id
            return self._register_func(func, None, category, name_cn, risk_level, tool_timeout)
            
        # 如果带有参数 @register_tool(...)
        def decorator(obj):
            if inspect.isclass(obj):
                return self._register_class(obj, tool_id, category, name_cn, risk_level, tool_timeout)
            else:
                return self._register_func(obj, tool_id, category, name_cn, risk_level, tool_timeout)
        return decorator

    def _register_class(self, clazz: type, tool_id: str | None, category: str, name_cn: str, risk_level: str, timeout: int | None = None):
        # 实例化类
        try:
            instance = clazz()
        except Exception as e:
            raise ValueError(f"Failed to instantiate tool class {clazz.__name__}: {e}")

        # 查找入口方法
        func = None
        if hasattr(instance, 'execute') and callable(instance.execute):
            func = instance.execute
        elif callable(instance):
            func = instance.__call__
        else:
            raise ValueError(f"Class {clazz.__name__} must implement 'execute' method or be callable.")

        # 确定工具ID
        if not tool_id:
            tool_id = clazz.__name__
            
        # 创建包装器以保持正确的名称和文档
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
            
        wrapper.__name__ = tool_id
        
        # 如果方法没有文档字符串，尝试使用类的文档字符串
        if not wrapper.__doc__:
            wrapper.__doc__ = inspect.getdoc(clazz)
            
        # 注册包装后的函数
        # 注意：这里我们返回 clazz，以便类定义保持不变，
        # 但我们在内部注册了 wrapper 函数作为工具执行体。
        self._register_func(wrapper, tool_id, category, name_cn, risk_level, timeout)
        return clazz

    def _register_func(self, func: Callable, tool_id: str | None, category: str, name_cn: str, risk_level: str, timeout: int | None = None):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return self._guarded_execute(func, *args, **kwargs)

        name = func.__name__
        # 如果没有提供ID，使用函数名作为ID
        final_id = tool_id if tool_id else name
        
        # 协议层工具名清洗：MCP/腾讯文档等工具名可能含点号（如 doc.copy_format），
        # sub2api 转 OpenAI 协议时要求工具名只含 a-z A-Z 0-9 _ -，点号会导致
        # "invalid name: Only a-z, A-Z, 0-9, underscores, and dashes are allowed" 400，
        # BT 侧表现为第二轮对话 500「不符合 API 协议」。
        # 这里把 . 替换为 _ 作为对外暴露名（safe_name），同时保留原始名注册，
        # 使 get_tool_func/safe_name 能命中同一 wrapper 执行。
        safe_name = sanitize_tool_name(name)

        self._tools[name] = wrapper
        schema = self._generate_schema(func)
        # 对外 schema 使用清洗后的工具名，避免上游协议校验失败
        if safe_name != name:
            schema["function"]["name"] = safe_name
        self._schemas.append(schema)
        
        # 存储元数据
        state = self._states.get(final_id, {"show": True})
        self._metadata[name] = {
            "id": final_id,
            "name": name,
            "name_cn": name_cn,
            "category": category,
            "risk_level": risk_level,
            "description": schema["function"]["description"],
            "show": state.get("show", True),
            "timeout": int(timeout) if timeout else None,
        }
        # 清洗名别名：让主循环用模型返回的 safe_name 也能找到工具
        if safe_name != name:
            self._tools[safe_name] = wrapper
            self._metadata[safe_name] = dict(self._metadata[name])
            self._metadata[safe_name]["name"] = safe_name
        
        return wrapper

    def set_tool_show_status(self, tool_id: str | None = None, show: bool = True, category: str | None = None) -> bool:
        """设置工具或分类下工具的显示状态"""
        found = False
        
        # 如果提供了 category，按分类批量设置
        if category:
            for name, meta in self._metadata.items():
                if meta.get("category") == category:
                    meta["show"] = show
                    self._states[meta["id"]] = {"show": show}
                    found = True
        
        # 如果提供了 tool_id，按 ID 设置
        elif tool_id:
            for name, meta in self._metadata.items():
                if meta["id"] == tool_id:
                    meta["show"] = show
                    self._states[tool_id] = {"show": show}
                    found = True
        
        if found:
            self._save_states()
            return True
        return False

    def get_openai_tools(self, enabled_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """
        返回 OpenAI 格式的工具定义。
        
        Args:
            enabled_ids: 允许使用的工具ID列表。如果不传，则返回所有(兼容旧行为)。
        """
        if enabled_ids is None:
            return self._dedup_tools(self._schemas)
        filtered_schemas = []
        for schema in self._schemas:
            name = schema["function"]["name"]
            meta = self._metadata.get(name)
            # 只有 ID 在启用列表中才返回
            if meta and meta["id"] in enabled_ids:
                filtered_schemas.append(schema)
        return self._dedup_tools(filtered_schemas)

    @staticmethod
    def _dedup_tools(schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按 function.name 去重，保留首次出现的（内置工具先注册，故优先）。

        背景：_schemas 是 list.append 累积、_register_func 不去重，而 MCP 加载时
        desktop-commander 的 kill_process 会与内置同名工具共存两条 schema。
        上游模型不接受同名顶层工具，会返回 400 "duplicate top-level executable
        tool name"，被中转池再包装成 503 no_healthy_account。
        """
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for schema in schemas:
            name = schema.get("function", {}).get("name")
            if name in seen:
                logger.warning("[Tools] 跳过重复工具名（上游不允许同名）: %s", name)
                continue
            seen.add(name)
            out.append(schema)
        return out

    def dedup_tools(self, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """公开入口：供 agent 在合并 MCP 工具后做最终去重。"""
        return self._dedup_tools(schemas)

    def get_all_tools_info(self) -> list[dict[str, Any]]:
        """获取所有工具的详细信息列表 (用于前端展示)"""
        infos = []
        for name, meta in self._metadata.items():
            # 创建副本以免修改原始元数据
            info = meta.copy()
            # 如果存在 name_cn，替换 name 字段
            if info.get("name_cn"):
                info["name"] = info["name_cn"]
            infos.append(info)
        return infos

    def get_tool_func(self, name: str) -> Callable | None:
        return self._tools.get(name)

    def _generate_schema(self, func: Callable) -> dict[str, Any]:
        """根据文档字符串和类型提示生成 OpenAI 函数 Schema"""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description provided."
        
        parameters = {
            "type": "object",
            "properties": {},
            "required": []
        }
        
        type_hints = get_type_hints(func)
        
        # 解析 docstring 的 Args 段，提取每个参数的描述（Google 风格，支持多行续行）
        arg_docs = {}
        if doc and "Args:" in doc:
            try:
                args_section = doc.split("Args:", 1)[1]
                stop_at = None
                for kw in ("Returns:", "Raises:", "Example", "Usage", "Note"):
                    i = args_section.find(kw)
                    if i != -1:
                        stop_at = i if stop_at is None else min(stop_at, i)
                if stop_at is not None:
                    args_section = args_section[:stop_at]
                cur_name, cur_desc = None, []
                for ln in args_section.splitlines():
                    m = re.match(r"^\s{2,}(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", ln)
                    if m:
                        if cur_name:
                            arg_docs[cur_name] = " ".join(cur_desc).strip()
                        cur_name, cur_desc = m.group(1), [m.group(2).strip()]
                    elif cur_name and ln.strip():
                        cur_desc.append(ln.strip())
                if cur_name:
                    arg_docs[cur_name] = " ".join(cur_desc).strip()
            except (re.error, AttributeError):
                # 正则解析失败时降级为无参数描述，不影响工具注册
                arg_docs = {}
            except Exception:
                logger.warning("解析参数文档失败，已降级为无描述", exc_info=True)
                arg_docs = {}

        for name, param in sig.parameters.items():
            if name == "self" or name == "cls":
                continue

            # Skip *args and **kwargs
            if param.kind == inspect.Parameter.VAR_POSITIONAL or param.kind == inspect.Parameter.VAR_KEYWORD:
                continue

            param_type = type_hints.get(name, str)
            json_type = self._python_type_to_json_type(param_type)

            param_info = {"type": json_type}
            desc = arg_docs.get(name)
            if desc:
                param_info["description"] = desc
            if param.default != inspect.Parameter.empty:
                d = param.default
                if isinstance(d, (str, int, float, bool)) or d is None:
                    param_info["default"] = d
                elif isinstance(d, (list, tuple)):
                    try:
                        param_info["default"] = list(d)
                    except Exception:
                        logger.warning("异常被静默吞掉，已记录", exc_info=True)

            parameters["properties"][name] = param_info

            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
                
        return {
            "type": "function",
            "function": {
                "name": func.__name__,
                "strict": True,
                "description": doc,
                "parameters": parameters
            }
        }

    def _python_type_to_json_type(self, py_type) -> str:
        if py_type == int:
            return "integer"
        elif py_type == float:
            return "number"
        elif py_type == bool:
            return "boolean"
        elif py_type == list or getattr(py_type, "__origin__", None) == list:
            return "array"
        elif py_type == dict or getattr(py_type, "__origin__", None) == dict:
            return "object"
        else:
            return "string"

# 全局注册实例
registry = ToolRegistry()

# 装饰器别名
def register_tool(tool_id=None, **kwargs):
    return registry.register_tool(tool_id, **kwargs)


# ============================================================
# MCP 工具按需加载（Tool Search）
#
# 背景（2026-10-21 调研）：
#   BT 启动时全量注入 MCP 工具 schema，实测 275 个 MCP 工具 + 83 个内置工具，
#   schema 体积远超内置部分，且历史统计显示 MCP 工具调用次数为 0。
#   对照 Codex CLI：MCP 工具默认 defer（不注入 schema），模型搜索后才展开，
#   内置工具只有 3~15 个，因此固定开销极低。
#
# 方案：
#   - MCP 工具仍正常注册到 registry（执行链路不变，get_tool_func 照常命中）；
#   - 但默认不下发 schema（从 tools 数组里剔除），只下发一个 ToolSearch 工具；
#   - 模型需要 MCP 能力时调用 ToolSearch 关键词搜索，命中后本会话内解锁，
#     后续请求才带上这些工具的完整 schema。
#   - 解锁状态按 session 隔离，避免一个会话开了工具影响其他会话。
#   - 配置：mcp_tool_defer（默认 true）/ mcp_defer_always_include（始终下发名单）
# ============================================================

# session_id -> 已解锁的 MCP 工具名集合
_mcp_unlocked: dict[str, set[str]] = {}
_mcp_unlocked_lock = threading.Lock()


def unlock_mcp_tools(session_id: str, names: set[str]):
    """把若干 MCP 工具标记为本会话已解锁（下一轮请求会带上其 schema）。"""
    if not session_id or not names:
        return
    with _mcp_unlocked_lock:
        _mcp_unlocked.setdefault(session_id, set()).update(names)


def get_unlocked_mcp_tools(session_id: str) -> set[str]:
    if not session_id:
        return set()
    with _mcp_unlocked_lock:
        return set(_mcp_unlocked.get(session_id) or ())


def clear_unlocked_mcp_tools(session_id: str):
    with _mcp_unlocked_lock:
        _mcp_unlocked.pop(session_id, None)


def is_mcp_tool(name: str) -> bool:
    """该工具是否为 MCP 来源（注册时 category == "mcp"）。"""
    meta = registry._metadata.get(name) or {}
    return meta.get("category") == "mcp"


def is_builtin_defer_tool(name: str) -> bool:
    """该内置工具是否属于「非核心、可延迟下发」集合。
    MCP 工具（含点号原名及清洗别名）不属于此列，返回 False。"""
    meta = registry._metadata.get(name) or {}
    if meta.get("category") == "mcp":
        return False
    if name not in DEFAULT_CORE_TOOL_NAMES:
        return True
    return False


def _seq_match(haystack: str, sub: str) -> bool:
    """sub 是否为 haystack 的子序列（各字符按序出现），用于工具名模糊匹配。"""
    i = 0
    for c in sub:
        i = haystack.find(c, i)
        if i < 0:
            return False
        i += 1
    return True


# session_id -> 已解锁的内置工具名集合
_builtin_unlocked: dict[str, set[str]] = {}
_builtin_unlocked_lock = threading.Lock()


def unlock_builtin_tools(session_id: str, names: set[str]):
    """把若干内置工具标记为本会话已解锁。"""
    if not session_id or not names:
        return
    with _builtin_unlocked_lock:
        _builtin_unlocked.setdefault(session_id, set()).update(names)


def get_unlocked_builtin_tools(session_id: str) -> set[str]:
    if not session_id:
        return set()
    with _builtin_unlocked_lock:
        return set(_builtin_unlocked.get(session_id) or set())


def clear_builtin_unlocked_tools(session_id: str):
    with _builtin_unlocked_lock:
        _builtin_unlocked.pop(session_id, None)


@register_tool(category="系统", name_cn="搜索并解锁工具", risk_level="low")
def ToolSearch(query: str = "", limit: int = 15) -> str:
    """
    - 搜索当前已连接但尚未下发的工具（MCP 工具和内置非核心工具）
    - 适用：需要文档/表格/幻灯片、地图、服务器运维、知识库等能力，但当前工具列表里没有对应工具时
    - 参数：query（关键词，如 "文档 表格"、"slide"、"地图"、"Docker"、"日志"；留空列出全部可解锁工具）、
            limit（最多返回条数，默认 15）
    - 命中后会立即解锁这些工具，下一次回复即可直接调用

    设计说明：非核心工具默认不注入模型上下文以节省固定开销。搜索是唯一入口，
    解锁仅对当前会话生效。
    """
    try:
        limit = max(1, min(int(limit or 15), 50))
    except Exception:
        limit = 15
    sid = _current_session_id() or ""

    # --- 内置非核心工具 ---
    builtin_unlocked = get_unlocked_builtin_tools(sid)
    builtin_candidates: list[tuple[str, str]] = []
    seen_ids: set[str] = set()
    for name in registry._metadata:
        if name in DEFAULT_CORE_TOOL_NAMES:
            continue
        tid = registry._metadata.get(name, {}).get("id") or name
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        if name in builtin_unlocked or (tid and tid in builtin_unlocked):
            continue
        desc = (registry._metadata.get(name, {}).get("description") or "")[:200]
        builtin_candidates.append((name, desc))

    # --- MCP 工具 ---
    mcp_unlocked = get_unlocked_mcp_tools(sid)
    mcp_candidates: list[tuple[str, str]] = []
    for name, meta in registry._metadata.items():
        if meta.get("category") != "mcp":
            continue
        tid = meta.get("id") or name
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        if name in mcp_unlocked or tid in mcp_unlocked:
            continue
        desc = (meta.get("description") or "")[:200]
        mcp_candidates.append((name, desc))

    all_candidates = builtin_candidates + mcp_candidates

    if not all_candidates:
        return tool_result_for_model(
            "当前没有待解锁的工具（可能已全部解锁，或未连接任何 MCP 服务器）。"
        )

    q = (query or "").strip().lower()
    if q:
        terms = [t for t in re.split(r"[\s,，、]+", q) if t]
        scored: list[tuple[int, str, str]] = []
        for name, desc in all_candidates:
            name_lower = name.lower()
            hay = (name + " " + desc).lower()
            meta = registry._metadata.get(name) or {}
            name_cn = (meta.get("name_cn") or "").lower()
            # 模糊评分：中文名子串 10，工具名字序列 5，英文子串 3，描述子串 1
            score = 0
            for t in terms:
                if t in name_cn:
                    score += 10
                elif _seq_match(name_lower, t):
                    score += 5
                elif t in name_lower:
                    score += 3
                elif t in hay:
                    score += 1
            scored.append((score, name, desc))
        scored.sort(key=lambda x: (-x[0], x[1]))
        has_match = any(s > 0 for s, _, _ in scored)
        if not has_match:
            _hint = (
                f"没有找到「{query}」相关工具（工具名/描述多为英文）。"
                f"以下列出全部 {len(all_candidates)} 个可解锁工具供挑选：\n"
            )
        else:
            _hint = ""
        picked_items = [(n, d) for _, n, d in scored[:limit]]
    else:
        _hint = ""
        picked_items = sorted(all_candidates, key=lambda x: x[0])[:limit]

    # 解锁内置工具
    for n, _ in picked_items:
        if is_builtin_defer_tool(n):
            unlock_builtin_tools(sid, {n})
    # 解锁 MCP 工具（同时登记原名和清洗名）
    for n, _ in picked_items:
        if not is_builtin_defer_tool(n):
            names_unlock: set[str] = {n}
            _tid = (registry._metadata.get(n) or {}).get("id")
            if _tid:
                names_unlock.add(_tid)
            unlock_mcp_tools(sid, names_unlock)

    lines = []
    if _hint:
        lines.append(_hint.rstrip())
        lines.append("")
    lines.append(f"已解锁 {len(picked_items)} 个工具（本会话有效），现在可以直接调用：")
    for n, d in picked_items:
        src = "built-in" if is_builtin_defer_tool(n) else "MCP"
        lines.append(f"- {n} [{src}]: {d or '(无描述)'}")
    if len((scored if q else sorted(all_candidates, key=lambda x: x[0]))) > len(picked_items):
        total = len(scored if q else sorted(all_candidates, key=lambda x: x[0]))
        lines.append(f"……另有 {total - len(picked_items)} 个工具，可提高 limit 或细化 query。")
    return tool_result_for_model("\n".join(lines))



# ============================================================
# TaskStatus / TaskStop：查询/停止后台任务
# 所有工具超时后转入后台，都通过这两个工具管理
# ============================================================
@register_tool(category="系统", name_cn="查询后台任务状态", risk_level="low")
def TaskStatus(task_id: str) -> str:
    """
    - 查询由 TaskStatus 自动转入后台的工具执行结果
    - 适用：工具已自动转后台（返回 task_id），用来查进度或最终结果
    - 参数：task_id（后台任务 ID，格式如 123e4567-e89b-12d3-a456-426614174000）

    防轮询提醒：任务完成后系统将自动推送通知，无需主动轮询。重复调用只会消耗
    资源，不加速结果返回。
    """
    task = _BG.get(task_id)
    if not task:
        return _xml_response("error", f"未找到任务 {task_id}，可能已被清理或 ID 错误")
    result = task.result or "[等待中…]"
    status = task.status
    # 运行时还在跑：提醒用户停止轮询
    if status == "running":
        result += (
            "\n\n提醒：你不需要重复调用 TaskStatus。任务完成后你将自动收到通知。"
            "请停止轮询并等待——重复调用会浪费计算资源和用户配额。"
            "如果你当前正在循环轮询或使用 Sleep-轮询模式，请立即停止。"
        )
    return _xml_response(
        status,
        f"<task_id>{task.task_id}</task_id>\n"
        f"<task_status>{status}</task_status>\n"
        f"<tool_name>{task.tool}</tool_name>\n"
        f"<command_run_logs>\n{result}\n</command_run_logs>\n",
    )


@register_tool(category="系统", name_cn="停止后台任务", risk_level="medium")
def TaskStop(task_id: str) -> str:
    """
    - 停止正在运行的后台任务（如果还在运行）
    - 适用：任务太久或用户不需要了，想取消执行
    - 参数：task_id（后台任务 ID）

    注意：后台任务实际在 Python 线程池里跑，stop 只是改状态，
    线程无法强制杀死（Python 线程无 abort）。已完成的不会被影响。
    """
    task = _BG.get(task_id)
    if not task:
        return _xml_response("error", f"未找到任务 {task_id}")
    if task.status == "running":
        task.status = "stopped"
        _BG.remove(task_id)
        return _xml_response("done", f"任务 {task_id} 已标记为停止（线程继续运行但结果不再返回）")
    return _xml_response("done", f"任务 {task_id} 已处于终态 ({task.status})，无需停止")


# 导入所有工具以确保它们被注册
from . import (  # noqa: F401  (导入以触发各工具的 @register_tool 注册副作用)
    agent_tools,
    browser,
    edit,
    knowledge,
    market,
    mcp_tool,
    mem0,
    scheduler,
    skill,
    summary,
    sys_ops,
    task,
    terminal,
    todo,
    tool_archive,
    webfetch,
    websearch,
    wechat_media,
    wechat_route,  # WeChatRoute 消息回复工具（文本/图片/视频/语音/链接/文件/名片/音乐卡片）
    gateway_media_send,  # 统一出站媒体发送工具
    view_image,  # 查看本地图片（元信息 + base64 data URL）
)

# ============================================================
# 内置工具按需加载（Defer）
#
# 背景：单 Agent 模式下全量下发 85 个工具会导致首轮 prompt 达到 109 KB，
# 其中 ~42 个低频工具（web/系统/知识/市场等）实际很少用到。
# 方案：核心工具常驻，其余通过 ToolSearch 按需解锁。
# ============================================================

# 默认常驻的核心工具集合（单 Agent 模式下始终下发）
DEFAULT_CORE_TOOL_NAMES: frozenset[str] = frozenset([
    # 文件操作
    "Bash", "Read", "Write", "Edit", "Grep", "Glob", "Patch",
    "LargeFileEdit", "ReadToolArchive",
    # 命令管理
    "CheckCommandStatus", "StopCommand",
    # Agent/子任务
    "Task", "TaskStatus", "TaskStop", "TaskSummary",
    "TodoRead", "TodoWrite",
    # 媒体/微信
    "WeChatRoute", "ClawbotSendMedia", "SendMedia", "ViewImage",
    # 浏览器
    "browser_use",
    # 搜索/知识
    "WebSearch", "WebFetch", "curl_url", "http_check",
    # 市场/技能/专家
    "Skills", "SkillMarket", "ExpertMarket", "GetExpertPrompt",
    "InstallExpert", "SkillCategories", "ExpertScenes",
    # 按需加载入口
    "ToolSearch", "MCP",
    # 记忆/知识库
    "mem0_search", "mem0_add", "mem0_list",
    "KnowledgeSearch", "KnowledgeAdd",
    # 定时任务
    "ScheduleTask", "ListScheduledTasks",
    # 系统
    "get_current_time",
])

# 高频工具（有历史统计数据时使用，替代 DEFAULT_CORE_TOOL_NAMES）
# 来源：metrics 累计调用次数，≥10 次视为高频；按实际使用频率裁剪
HIGH_FREQ_TOOL_NAMES: frozenset[str] = frozenset([
    "Bash", "Read", "Write", "Edit", "Grep", "Glob", "Patch",
    "CheckCommandStatus", "StopCommand",
    "Task", "TaskStatus", "TaskStop", "TaskSummary",
    "TodoRead", "TodoWrite",
    "WeChatRoute", "ClawbotSendMedia", "SendMedia", "ViewImage",
    "browser_use",
    "WebSearch", "WebFetch", "curl_url", "http_check",
    "ToolSearch", "MCP",
    "mem0_search", "mem0_add", "mem0_list",
    "KnowledgeSearch", "KnowledgeAdd",
    "ScheduleTask", "ListScheduledTasks",
    "get_current_time",
])
