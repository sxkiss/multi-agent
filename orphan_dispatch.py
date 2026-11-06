"""孤儿后台任务主动回灌。

优-1 第十阶段：从 web_server.py 拆出的孤儿任务回调/重试/派发逻辑。
依赖的 agent_main、chat_jobs、_map_agent_chunk 通过 configure 注入，避免循环导入。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

logger = logging.getLogger(__name__)

_agent_main = None
_chat_jobs = None
_map_agent_chunk_impl = None
_orphan_cb_registered = False

# 等待窗口：父 job 在 notify_done 后 N 秒内结束，视为"仍在等待"
_ORPHAN_APPEND_WINDOW = float(os.environ.get("AI_AGENT_ORPHAN_APPEND_WINDOW", "10"))
_MAX_ORPHAN_RETRIES = int(os.environ.get("AI_AGENT_ORPHAN_MAX_RETRIES", "60"))
_ORPHAN_RETRY_DELAY = float(os.environ.get("AI_AGENT_ORPHAN_RETRY_DELAY", "3"))
_ORPHAN_RETRY_COUNTER: dict[str, int] = {}


def configure(agent_main, chat_jobs, map_agent_chunk) -> None:
    global _agent_main, _chat_jobs, _map_agent_chunk_impl
    _agent_main = agent_main
    _chat_jobs = chat_jobs
    _map_agent_chunk_impl = map_agent_chunk


def _map_agent_chunk(chunk):
    return _map_agent_chunk_impl(chunk)


_orphan_cb_registered = False


# 等待窗口：父 job 在 notify_done 后 N 秒内结束，视为"仍在等待"，
# 把结果追加到父 job 而非开孤儿 job（前端 SSE 还会继续收事件）。
_ORPHAN_APPEND_WINDOW = float(os.environ.get("AI_AGENT_ORPHAN_APPEND_WINDOW", "10"))
# R6：会话活跃重试参数 —— 主循环仍在跑时延迟重试，而不是直接丢弃。
# 重试超过上限说明主循环已死，强制走孤儿回复兜底。
_MAX_ORPHAN_RETRIES = int(os.environ.get("AI_AGENT_ORPHAN_MAX_RETRIES", "60"))
_ORPHAN_RETRY_DELAY = float(os.environ.get("AI_AGENT_ORPHAN_RETRY_DELAY", "3"))
# 每个 task 的重试计数（按 task_id 隔离），防止不同孤儿任务互相干扰。
_ORPHAN_RETRY_COUNTER: dict[str, int] = {}


def _register_orphan_callback():
    """在 _agent_main 初始化后注册孤儿回调。由 _setup_lifespan 调用一次。"""
    global _orphan_cb_registered
    if _orphan_cb_registered:
        return
    _orphan_cb_registered = True
    from chat_client.tools import get_background_manager
    bg = get_background_manager()

    # R1/R3 修复：per-session 锁，保证「检查 + 创建 job」是原子操作
    _session_orphan_locks: dict[str, threading.Lock] = {}
    _lock_session_orphans = threading.Lock()  # 保护 _session_orphan_locks 字典本身
    # R4 修复：追踪孤儿派发状态，避免并发重复派发
    _dispatching_tasks: set[str] = set()
    _lock_dispatching = threading.Lock()

    def _get_session_lock(session_id: str) -> threading.Lock:
        with _lock_session_orphans:
            return _session_orphan_locks.setdefault(session_id, threading.Lock())

    def _try_append_to_parent_job_inner(session_id: str, task_id: str, tool: str, status: str, result: str) -> tuple[bool, str]:
        """内部实现：返回 (success, reason)。
        
        reason: 'appended' | 'parent_running' | 'no_parent' | 'window_expired' | 'already_dispatched'
        """
        # R3 修复：用 bg_repo 状态机判断是否已消费（替代不安全的 _task_done_at）
        try:
            from .tools.bg_repo import get_repo, STATUS_READY
            row = get_repo().get(task_id)
            if row and row.status == STATUS_READY:
                logger.info("[Background] task %s 已在 bg_repo 中，跳过孤儿回调", task_id[:12])
                return True, 'already_consumed'
        except Exception:
            pass

        jobs = _chat_jobs.active_for_session(session_id)
        if not jobs:
            return False, 'no_parent'
        # 取最新结束的 job（不是 running 的）
        parent_job = None
        parent_finished_at = None
        for _k, _j in sorted(jobs, key=lambda x: x[1].created_at, reverse=True):
            if not _j.is_running:
                parent_job = _j
                parent_finished_at = getattr(_j, 'finished_at', None)
                break
        if parent_job is None:
            return False, 'no_parent'

        # R2 修复：父 job 仍 running 时返回 'parent_running'，让调用方决定不做任何事
        if parent_job.is_running:
            return False, 'parent_running'

        _window_age = time.time() - parent_finished_at if parent_finished_at else 9999
        if _window_age > _ORPHAN_APPEND_WINDOW:
            logger.info(
                "[Background] 孤儿回调父 job 已死透（%.1fs），不追加，走新会话回灌 "
                "session=%s job=%s finished_at=%s",
                _window_age, session_id[:24], parent_job.key,
                time.strftime('%H:%M:%S', time.localtime(parent_finished_at)))
            return False, 'window_expired'

        # 推前端事件（user 消息形式展示结果，纯文本，不带 XML 外壳）
        _plain = _strip_tool_xml(result)
        parent_job.append("background_result", {
            "task_id": task_id, "tool": tool, "status": status, "result": _plain
        })
        parent_job.append("message", _plain)
        parent_job.append("message_end", None)
        parent_job.finish("done")
        logger.info(
            "[Background] 孤儿回调追加到父 job task=%s session=%s job=%s window_age=%.1fs",
            task_id[:12], session_id[:24], parent_job.key, _window_age)
        return True, 'appended'

    def _retry_orphan_check(session_id: str, task_id: str, tool: str, status: str, result: str):
        """延迟重试入口：R6 主循环仍在运行时，稍后重新检查并触发孤儿回调。

        之所以要用独立线程而不是直接递归调用 _handler，是为了释放 session_lock，
        避免在 Timer 线程里长时间持有锁导致其他孤儿回调阻塞。
        """
        try:
            # 重新走完整 handler 逻辑（内部会再次检查 _dispatching_tasks 防重）。
            # 注意这里不再持有旧锁，重新走 _handler 会重新加锁，保证原子性。
            _handler(session_id, task_id, tool, status, result)
        except Exception:
            logger.warning(
                "[Background] 孤儿重试失败 session=%s task=%s",
                session_id[:24], task_id[:12], exc_info=True)

    def _handler(session_id: str, task_id: str, tool: str, status: str, result: str):
        """孤儿回调：父会话已结束，尝试把结果追加到父 job 或开新 job。

        优先级：
        1. 查父 job 是否在 10s 等待窗口内结束 → 追加到父 job（前端还能收到）
        2. 否则 → 开孤儿 job（兜底，结果可能已过期）

        R1/R4 修复：per-session 锁保证原子性，调度状态防止重复派发。
        R6 修复：以「父会话是否有 running job」为活跃判据（job 实例随时会死，
                 但会话维度稳定）。若主循环仍在运行则延迟重试，而非直接丢弃；
                 重试超限说明主循环已死，强制走孤儿 job 兜底回灌。
        """
        # 防御：测试遗留的占位任务（如 t-pend-1/t-wake-1/t-full-1）不得回灌，
        # 否则会形成「通知 → 新 job → 再通知」的死循环。
        if task_id.startswith("t-") and str(result or "").strip() in ("", "OK", "DONE_RESULT"):
            logger.info("[Background] 跳过测试遗留占位任务 task=%s", task_id[:12])
            return

        retries = _ORPHAN_RETRY_COUNTER.get(task_id, 0)
        try:
            # R4 修复：检查是否正在派发中
            if task_id in _dispatching_tasks:
                logger.info("[Background] task %s 正在派发中，跳过重复回调", task_id[:12])
                return
            _dispatching_tasks.add(task_id)

            session_lock = _get_session_lock(session_id)
            with session_lock:
                # R6：以「会话下是否存在 running job」判断主循环是否活跃
                # （job 实例随时会死/被 prune/重启后停掉，会话维度才是稳定判据）
                if _chat_jobs.count_running(session_id) > 0:
                    retries += 1
                    _ORPHAN_RETRY_COUNTER[task_id] = retries
                    if retries <= _MAX_ORPHAN_RETRIES:
                        logger.info(
                            "[Background] 父会话 %s 仍有 running job（第 %d 次），延迟重试等 drain 消费 task=%s",
                            session_id[:24], retries, task_id[:12])
                        _dispatching_tasks.discard(task_id)
                        threading.Timer(_ORPHAN_RETRY_DELAY, _retry_orphan_check,
                                        args=(session_id, task_id, tool, status, result)).start()
                        return
                    logger.warning(
                        "[Background] 父会话 %s 在 %d 次重试后仍有 running job，判定主循环已死，"
                        "强制走孤儿回复兜底 task=%s",
                        session_id[:24], _MAX_ORPHAN_RETRIES, task_id[:12])

                # 先尝试追加到父 job
                success, reason = _try_append_to_parent_job_inner(
                    session_id, task_id, tool, status, result)
                if success:
                    if reason == 'appended':
                        # 追加成功，直接派新 job 让模型消费
                        _dispatch_orphan_job_after_append(session_id, task_id, tool, status, result)
                    # 'already_consumed' 或 'parent_running' 都不需要再操作
                else:
                    # 父 job 不可用，开孤儿 job
                    if reason == 'parent_running':
                        # R6：外层重试已超限才可能走到这里（count_running 仍 >0 但已判定主循环死），
                        # 此时强制开孤儿 job 兜底回灌，而非只记日志等待（会永久丢失）。
                        logger.info("[Background] 父 job 仍在运行但重试已超限，强制开孤儿 job task=%s", task_id[:12])
                        _dispatch_orphan_job(session_id, task_id, tool, status, result)
                    elif reason == 'window_expired':
                        _dispatch_orphan_job(session_id, task_id, tool, status, result)
                    else:
                        # no_parent 或其他：也走孤儿路径
                        _dispatch_orphan_job(session_id, task_id, tool, status, result)
        except Exception:
            logger.warning(
                "[Background] 孤儿回调触发失败 session=%s task=%s",
                session_id[:24], task_id[:12], exc_info=True,
            )
        finally:
            _dispatching_tasks.discard(task_id)
            # R6：任务已得到最终处理（追加/开孤儿 job），清理重试计数，避免内存泄漏
            _ORPHAN_RETRY_COUNTER.pop(task_id, None)

    bg.set_orphan_callback(_handler)
    logger.info("[Background] 孤儿回调已注册（AI_AGENT_ORPHAN_DELAY=%ss）", bg._orphan_delay)


def _strip_tool_xml(result: str) -> str:
    """把工具结果中的 <tool> XML 外壳剥成纯文本，保留状态与正文。

    与 chat_client.tools.base.tool_result_for_model 保持一致，供 web_server
    内部（孤儿回复 / background_result 事件）在无 tools 依赖时做同样归一化。

    回灌给模型时不携带 XML：内层 toolcall_result 中的 <terminal_id>/
    <command_id> 等元数据一律剥掉，只保留可读文本；已转后台的 running
    任务额外提炼 command_id，提示模型用 CheckCommandStatus 查询。
    """
    if not isinstance(result, str) or "<tool>" not in result:
        return result
    body_m = re.search(r"<toolcall_result>(.*?)</toolcall_result>", result, re.DOTALL)
    if not body_m:
        return result
    name_m = re.search(r"<tool_name>(.*?)</tool_name>", result, re.DOTALL)
    status_m = re.search(r"<toolcall_status>(.*?)</toolcall_status>", result, re.DOTALL)
    name = (name_m.group(1).strip() if name_m else "") or "unknown"
    st = (status_m.group(1).strip() if status_m else "done") or "done"
    inner = body_m.group(1)

    # 剥掉全部内层 XML 标签，压缩多余空行
    cmd_m = re.search(r"<command_id>(.*?)</command_id>", inner, re.DOTALL)
    cmd_id = cmd_m.group(1).strip() if cmd_m else ""
    # 先移除纯元数据元素（terminal_id/terminal_cwd/command_id），避免正文里
    # 重复出现裸值；再剥掉其余标签。
    text = re.sub(
        r"<(terminal_id|terminal_cwd|command_id)>.*?</\1>",
        "", inner, flags=re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", "", text).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)

    if st == "running" and cmd_id:
        return (
            f"[后台任务] {name} 命令已转入后台继续运行。\n"
            f"command_id: {cmd_id}\n"
            f"{text}\n"
            f"请用 CheckCommandStatus(command_id=\"{cmd_id}\") 查询结果。"
        )
    header = f"[{name} 执行结果]" if st == "done" else f"[{name} 执行状态: {st}]"
    return f"{header}\n{text}"


def _dispatch_orphan_job_after_append(session_id: str, task_id: str, tool: str, status: str, result: str):
    """追加到父 job 后，派发新 job 让模型消费结果。
    
    R4 修复：调用前已由 _handler 持有 session_lock，保证原子性。
    """
    payload = {
        "session_id": session_id,
        "message": (
            f"【后台任务结果回复】\n"
            f"task_id: {task_id}\n"
            f"工具: {tool}\n"
            f"状态: {status}\n"
            f"结果:\n{_strip_tool_xml(result)}\n\n"
            f"以上是之前派发的后台任务执行结果，现已完成。\n"
            f"请基于以上结果回答用户的问题。如果之前用户在等你完成任务，"
            f"现在请给出完整答案。"
        ),
        "mode": "single",
    }
    orphan_job = _chat_jobs.create_if_idle(session_id, label=f"bg_orphan_{task_id[:8]}")
    if orphan_job is None:
        logger.warning(
            "[Background] 孤儿回复跳过（会话并行满）session=%s task=%s",
            session_id[:24], task_id[:12],
        )
    else:
        agent, user_input, err = _agent_main._build_chat_agent(payload)
        if agent is None or err:
            logger.warning("[Background] 孤儿回复 Agent 初始化失败 session=%s task=%s err=%s",
                          session_id[:24], task_id[:12], err)
            _chat_jobs.discard(orphan_job.key)
        else:
            orphan_job.attach_agent(agent)

            def _run():
                from chat_client.tools import set_current_job as _set_job
                _set_job(orphan_job)
                # 拉取最终文本（用于推回通道；没有则跳过）
                # 注意：_map_agent_chunk 对 type="content" 输出 ("message", str)
                # 而 chat_events 路径走 SSE 解析后 data 是 dict，两者不一致；
                # 这里必须同时兼容 string / dict 两种 data 形态。
                reply_text = ""
                try:
                    for chunk in agent.chat(user_input):
                        for ev, data in _map_agent_chunk(chunk):
                            orphan_job.append(ev, data)
                            if ev == "message":
                                if isinstance(data, dict):
                                    c = str(data.get("content") or data.get("text") or "")
                                else:
                                    c = str(data or "")
                                if c:
                                    reply_text += c
                    orphan_job.append("message_end", None)
                    orphan_job.finish("done")
                except Exception as e:
                    reply_text = f"任务执行出错: {e!s}"
                    orphan_job.append("error", {"msg": f"孤儿回复异常: {e!s}"})
                    orphan_job.append("message_end", None)
                    orphan_job.finish("error")
                finally:
                    # 推到 IM 通道（若路由可用）；失败只记日志不抛
                    if reply_text.strip():
                        try:
                            from platform_ext.gateway_processor import push_to_channel
                            push_to_channel(session_id, reply_text)
                        except Exception:  # noqa: BLE001
                            logger.debug(
                                "[Background] 回灌消息推送通道失败 session=%s task=%s",
                                session_id[:24], task_id[:12],
                                exc_info=True,
                            )
                    try:
                        agent.close()
                    except Exception:
                        pass

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            logger.info(
                "[Background] 孤儿回调派新 job 消费结果 task=%s session=%s orphan=%s",
                task_id[:12], session_id[:24], orphan_job.key)


def _try_append_to_parent_job(session_id: str, task_id: str, tool: str, status: str, result: str) -> bool:
    """尝试把后台任务结果追加到父 job（不新开 job）。

    适用场景：父 job 在 10s 等待窗口内结束，前端 SSE 连接可能还活着，
    直接追加到同一个 job 让前端一次性收到完整结果。

    @return True 表示已成功追加，False 表示跳过（窗口已过期或找不到父 job）。
    
    R3 修复：使用 bg_repo 状态机代替不安全的 _task_done_at 字典。
    """
    # R3 修复：检查是否已通过 drain_finished 消费
    try:
        from .tools.bg_repo import get_repo, STATUS_READY
        row = get_repo().get(task_id)
        if row and row.status == STATUS_READY:
            logger.info("[Background] task %s 已被 drain_finished 消费，跳过", task_id[:12])
            return True
    except Exception:
        pass

    jobs = _chat_jobs.active_for_session(session_id)
    if not jobs:
        return False
    # 取最新结束的 job（不是 running 的）
    parent_job = None
    parent_finished_at = None
    for _k, _j in sorted(jobs, key=lambda x: x[1].created_at, reverse=True):
        if not _j.is_running:
            parent_job = _j
            parent_finished_at = getattr(_j, 'finished_at', None)
            break
    if parent_job is None:
        return False
    # 以父 job 的结束时刻判断窗口：父 job 结束不超过 _ORPHAN_APPEND_WINDOW 才算"刚死"，
    # 追加还能被前端续传看到；超过则视为已死透，返回 False 让上层开新孤儿 job。
    _window_age = time.time() - parent_finished_at if parent_finished_at else 9999
    if _window_age > _ORPHAN_APPEND_WINDOW:
        logger.info(
            "[Background] 孤儿回调父 job 已死透（%.1fs），不追加，走新会话回灌 "
            "session=%s job=%s finished_at=%s",
            _window_age, session_id[:24], parent_job.key,
            time.strftime('%H:%M:%S', time.localtime(parent_finished_at)))
        return False

    # 推前端事件（user 消息形式展示结果，纯文本，不带 XML 外壳）
    _plain = _strip_tool_xml(result)
    parent_job.append("background_result", {
        "task_id": task_id, "tool": tool, "status": status, "result": _plain
    })
    parent_job.append("message", _plain)
    parent_job.append("message_end", None)
    parent_job.finish("done")
    logger.info(
        "[Background] 孤儿回调追加到父 job task=%s session=%s job=%s window_age=%.1fs",
        task_id[:12], session_id[:24], parent_job.key, _window_age)
    return True


def _dispatch_orphan_job(session_id: str, task_id: str, tool: str, status: str, result: str):
    """为孤儿 session 派发新一轮对话，把后台任务结果塞进去让模型消费。

    复用的是与 chat_start 相同的 Agent 构建逻辑，只是把 payload 中的 message
    替换成"系统任务结果回灌"格式，让模型把它当作刚收到的 background_task_done。
    """
    payload = {
        "session_id": session_id,
        "message": (
            f"【后台任务结果回复】\n"
            f"task_id={task_id} tool={tool} status={status}\n"
            f"结果：\n{_strip_tool_xml(result)}\n"
            f"以上是之前派发的后台任务执行结果，现已完成。\n"
            f"请基于以上结果回答用户的问题。如果之前用户在等你完成任务，"
            f"现在请给出完整答案。"
        ),
        "mode": "single",
    }
    job = _chat_jobs.create_if_idle(session_id, label=f"bg_orphan_{task_id[:8]}")
    if job is None:
        logger.warning(
            "[Background] 孤儿回复跳过（会话并行满）session=%s task=%s",
            session_id[:24], task_id[:12],
        )
        return

    agent, user_input, err = _agent_main._build_chat_agent(payload)
    if agent is None or err:
        logger.warning(
            "[Background] 孤儿回复 Agent 初始化失败 session=%s task=%s err=%s",
            session_id[:24], task_id[:12], err,
        )
        _chat_jobs.discard(job.key)
        return

    job.attach_agent(agent)

    def _run():
        from chat_client.tools import set_current_job as _set_job
        _set_job(job)
        # 拉取最终文本（用于推回通道；没有则跳过）
        # 注意：_map_agent_chunk 对 type="content" 输出 ("message", str)，
        # 与 chat_events 路径的 dict data 不一致，必须兼容两者。
        reply_text = ""
        try:
            for chunk in agent.chat(user_input):
                for ev, data in _map_agent_chunk(chunk):
                    job.append(ev, data)
                    if ev == "message":
                        if isinstance(data, dict):
                            c = str(data.get("content") or data.get("text") or "")
                        else:
                            c = str(data or "")
                        if c:
                            reply_text += c
            job.append("message_end", None)
            job.finish("done")
        except Exception as e:
            reply_text = f"任务执行出错: {e!s}"
            job.append("error", {"msg": f"孤儿回复异常: {e!s}"})
            job.append("message_end", None)
            job.finish("error")
        finally:
            # 推到 IM 通道（若路由可用）；失败只记日志不抛
            if reply_text.strip():
                try:
                    from platform_ext.gateway_processor import push_to_channel
                    push_to_channel(session_id, reply_text)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "[Background] 回灌消息推送通道失败 session=%s task=%s",
                        session_id[:24], task_id[:12],
                        exc_info=True,
                    )
            job.append("message_end", None)
            job.finish("done")
            try:
                agent.close()
            except Exception:
                pass

    t = threading.Thread(
        target=_run,
        name=f"orphan-{task_id[:8]}",
        daemon=True,
    )
    t.start()
    logger.info(
        "[Background] 孤儿回复已派发 session=%s task=%s job=%s",
        session_id[:24], task_id[:12], job.key,
    )
