"""AgentMain 聊天流程职责。

优-1 第十六阶段：从 web_server.py 的 AgentMain 拆出的同步聊天、后台任务式
聊天（chat_start）、SSE 事件流、任务状态与停止；通过 mixin 复用提示词构建
（PromptMixin）与工作空间（WorkspaceMixin）方法。

注意：方法体内除 self.* 外还引用了 SSE 片段映射、终端渠道判定（定义在
channel_utils.py）与会话校验、任务管理器（agent_session / chat_jobs），
均为单向依赖，不再回指 web_server。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time

import public

from agent_session import _validate_session_id
from chat_jobs import chat_jobs
from channel_utils import (
    HIDDEN_EVENTS_FOR_TERMINAL,
    _client_channel,
    _is_terminal_channel,
    _map_agent_chunk,
)
from _chat_error_signal import is_fatal

logger = logging.getLogger(__name__)


class ChatFlowMixin:
    def chat(self, get):
        agent, user_input, err = self._build_chat_agent(get)
        if err:
            yield self.sse_pack(event=err["event"], data=err["data"])
            return

        try:
            for chunk in agent.chat(user_input):
                for ev, data in _map_agent_chunk(chunk):
                    yield self.sse_pack(event=ev, data=data)
            yield self.sse_pack(event="message_end")
        except Exception as e:
            yield self.sse_pack(event="error", data={"msg": f"聊天发生错误: {e!s}"})
        finally:
            try:
                agent.close()
            except Exception:
                logger.warning("chat agent.close 失败，已记录", exc_info=True)

    # ---------------- 后台任务式聊天（与前端连接解耦） ----------------

    def chat_start(self, get):
        """
        启动后台聊天任务，立即返回。
        任务在后端线程中运行，前端断开/刷新均不影响；通过 /api/chat/events 订阅事件流。
        """
        session_id = get.get('session_id', 'default_session')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")

        mode = str(get.get('mode', '')).strip().lower()
        user_input = str(get.get('message', '')).strip()
        if not user_input:
            return public.returnMsg(False, "缺少参数 message")

        label = str(user_input)[:36]
        job = chat_jobs.create_if_idle(session_id, label)
        if job is None:
            return public.returnMsg(
                False,
                f"该会话并行任务已满（{chat_jobs.MAX_PARALLEL}），等待任一任务完成后自动继续派发",
            )

        # ── per-thread HITL 审批模式（P1，2026-10-15）──────────────────
        # 请求可携带 hitl（ask/allow_all/allow_tools）+ hitl_tools（allow_tools 白名单）。
        # 会话级覆盖，作用于本次及后续该会话的高风险工具放行判断。
        try:
            _hitl = str(get.get('hitl', '') or '').strip().lower()
            if _hitl in ('ask', 'allow_all', 'allow_tools'):
                from chat_client.tools import set_session_hitl
                _hitl_tools = get.get('hitl_tools') or []
                if isinstance(_hitl_tools, str):
                    _hitl_tools = [t.strip() for t in _hitl_tools.split(',') if t.strip()]
                set_session_hitl(session_id, _hitl, _hitl_tools)
                logger.info("[chat_start] HITL 模式=%s tools=%s session=%s", _hitl, _hitl_tools, session_id)
        except Exception:
            logger.warning("[chat_start] 设置 HITL 模式失败", exc_info=True)

        # ── opencode 模式 ────────────────────────────────────────────
        if mode == 'opencode':
            from chat_client.opencode_bridge import run_opencode_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'

            # 提示词：system_prompt 直接传 → 模板选择 → 配置默认（三模式共用）
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')
            # RAG 上下文注入（全局 Mem0，适用于子进程模式）
            system_prompt = self._inject_rag_context(get, system_prompt, user_input)

            workspace = self._resolve_request_workspace(get, session_id)
            reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
            # 自定义 API 配置（优先前端传入，回退全局配置）
            custom_base_url = str(get.get('base_url', '')).strip() or self.config.get('api_base_url', '')
            custom_api_key = str(get.get('api_key', '')).strip() or self.config.get('api_key', '')
            # 持久化 workspace（CLI 自家 DB 可能不记录前端传入的 workspace）
            try:
                if workspace:
                    _md = os.path.join(self.plugin_path, 'sessions', session_id)
                    os.makedirs(_md, exist_ok=True)
                    _mf = os.path.join(_md, 'meta.json')
                    _m = {}
                    if os.path.exists(_mf):
                        try:
                            with open(_mf, 'r', encoding='utf-8') as _f:
                                _m = json.load(_f) or {}
                        except Exception:
                            _m = {}
                    _m['workspace'] = workspace
                    _m['source'] = 'opencode'
                    with open(_mf, 'w', encoding='utf-8') as _f:
                        json.dump(_m, _f, ensure_ascii=False)
            except Exception:
                logger.warning("opencode 模式持久化 workspace 到 meta.json 失败", exc_info=True)
            t = threading.Thread(
                target=run_opencode_chat,
                args=(session_id, user_input, job, model, system_prompt, workspace, reasoning_effort, custom_base_url, custom_api_key),
                name=f"oc-chat-{job.key}",
                daemon=True,
            )
            t.start()
            logger.info("[chat_start][opencode] tpl=%r sp=%r ws=%r base=%s", get.get('template', ''), (system_prompt or '')[:60], workspace, custom_base_url)
            return public.return_data(True, data={
                "session_id": session_id,
                "job": job.key,
                "started": True,
            })

        # ── claude 模式 ──────────────────────────────────────────────
        if mode == 'claude':
            from chat_client.claude_bridge import run_claude_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'

            # 提示词：system_prompt 直接传 → 模板选择 → 配置默认（三模式共用）
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')
            # RAG 上下文注入（全局 Mem0，适用于子进程模式）
            system_prompt = self._inject_rag_context(get, system_prompt, user_input)

            workspace = self._resolve_request_workspace(get, session_id)
            reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
            # 自定义 API 配置（优先前端传入，回退全局配置）
            custom_base_url = str(get.get('base_url', '')).strip() or self.config.get('api_base_url', '')
            custom_api_key = str(get.get('api_key', '')).strip() or self.config.get('api_key', '')
            try:
                if workspace:
                    _md = os.path.join(self.plugin_path, 'sessions', session_id)
                    os.makedirs(_md, exist_ok=True)
                    _mf = os.path.join(_md, 'meta.json')
                    _m = {}
                    if os.path.exists(_mf):
                        try:
                            with open(_mf, 'r', encoding='utf-8') as _f:
                                _m = json.load(_f) or {}
                        except Exception:
                            _m = {}
                    _m['workspace'] = workspace
                    _m['source'] = 'claude'
                    with open(_mf, 'w', encoding='utf-8') as _f:
                        json.dump(_m, _f, ensure_ascii=False)
            except Exception:
                logger.warning("claude 模式持久化 workspace 到 meta.json 失败", exc_info=True)
            t = threading.Thread(
                target=run_claude_chat,
                args=(session_id, user_input, job, model, system_prompt, workspace, reasoning_effort, custom_base_url, custom_api_key),
                name=f"claude-chat-{job.key}",
                daemon=True,
            )
            t.start()
            logger.info("[chat_start][claude] sp=%r ws=%r base=%s", (system_prompt or '')[:60], workspace, custom_base_url)
            return public.return_data(True, data={
                "session_id": session_id,
                "job": job.key,
                "started": True,
            })

        # ── codex 模式 ──────────────────────────────────────────────
        if mode == 'codex':
            from chat_client.codex_bridge import run_codex_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')
            # RAG 上下文注入（全局 Mem0，适用于子进程模式）
            system_prompt = self._inject_rag_context(get, system_prompt, user_input)
            workspace = self._resolve_request_workspace(get, session_id)
            reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
            custom_base_url = str(get.get('base_url', '')).strip() or self.config.get('api_base_url', '')
            custom_api_key = str(get.get('api_key', '')).strip() or self.config.get('api_key', '')
            try:
                if workspace:
                    _md = os.path.join(self.plugin_path, 'sessions', session_id)
                    os.makedirs(_md, exist_ok=True)
                    _mf = os.path.join(_md, 'meta.json')
                    _m = {}
                    if os.path.exists(_mf):
                        try:
                            with open(_mf, 'r', encoding='utf-8') as _f:
                                _m = json.load(_f) or {}
                        except Exception:
                            _m = {}
                    _m['workspace'] = workspace
                    _m['source'] = 'codex'
                    with open(_mf, 'w', encoding='utf-8') as _f:
                        json.dump(_m, _f, ensure_ascii=False)
            except Exception:
                logger.warning("codex 模式持久化 workspace 到 meta.json 失败", exc_info=True)
            t = threading.Thread(
                target=run_codex_chat,
                args=(session_id, user_input, job, model, system_prompt, workspace, reasoning_effort, custom_base_url, custom_api_key),
                name=f"codex-chat-{job.key}",
                daemon=True,
            )
            t.start()
            logger.info("[chat_start][codex] sp=%r ws=%r base=%s", (system_prompt or '')[:60], workspace, custom_base_url)
            return public.return_data(True, data={
                "session_id": session_id,
                "job": job.key,
                "started": True,
            })

        # ── native 模式（默认） ──────────────────────────────────────
        _t0 = time.time()
        agent, user_input, err = self._build_chat_agent(get)
        logger.info(f"[chat_start] _build_chat_agent took {time.time()-_t0:.2f}s")
        if err:
            chat_jobs.discard(job.key)
            return public.returnMsg(False, err["data"]["msg"])

        # 持久化 workspace 到 sessions/<id>/meta.json，供历史列表按工作目录分组
        try:
            _ws = (agent.config or {}).get('workspace', '') or ''
            if _ws:
                _meta_dir = os.path.join(self.plugin_path, 'sessions', session_id)
                os.makedirs(_meta_dir, exist_ok=True)
                _meta_file = os.path.join(_meta_dir, 'meta.json')
                _meta_data = {}
                if os.path.exists(_meta_file):
                    try:
                        with open(_meta_file, 'r', encoding='utf-8') as _mf:
                            _meta_data = json.load(_mf) or {}
                    except Exception:
                        _meta_data = {}
                _meta_data['workspace'] = _ws
                _meta_data['source'] = 'single'
                with open(_meta_file, 'w', encoding='utf-8') as _mf:
                    json.dump(_meta_data, _mf, ensure_ascii=False)
        except Exception:
            logger.warning("native 模式持久化 workspace 到 meta.json 失败", exc_info=True)

        job.attach_agent(agent)
        t = threading.Thread(
            target=self._run_chat_job,
            args=(job, agent, user_input),
            name=f"chat-job-{job.key}",
            daemon=True,
        )
        t.start()
        # 渠道日志：此前小程序请求在日志里与网页/CLI 完全无法区分，
        # 只能靠 jobs/ 下 wx_ 前缀反推。这里显式记录渠道，便于排查与审计。
        logger.info(
            "[chat_start] channel=%s session=%s mode=%s job=%s",
            _client_channel(get) or "web",
            session_id, get.get("mode", "") or "group", job.key,
        )
        return public.return_data(True, data={
            "session_id": session_id,
            "job": job.key,
            "started": True,
        })

    def _run_chat_job(self, job, agent, user_input):
        """后台线程：运行 agent.chat() 并把 chunk 写入 job 事件缓存"""
        from chat_client.tools import set_current_job
        set_current_job(job)  # 工具内可通过 emit_progress 推送实时进度
        fatal = False
        try:
            for chunk in agent.chat(user_input):
                # 2026-11-09：agent 外层兜底把异常 yield 成 error 事件而非 raise，
                # 生成器"正常"结束会照常 finish("done") → "带 error 事件但状态 done"
                # 的伪成功轮，上层误判本轮正常结束、不再补救。这里识别致命标记，
                # 让本轮落成 error；message_end 仍照发（SSE 流需要它收尾）。
                if is_fatal(chunk):
                    fatal = True
                # 工具调用/结果等透传事件需要完整 chunk
                for ev, data in _map_agent_chunk(chunk):
                    job.append(ev, data)
            job.append("message_end", None)
            job.finish("error" if fatal else "done")
            if fatal:
                logger.warning(
                    "[ChatJob] 本轮以失败收尾（agent 致命错误已作为 error 事件发出）"
                    " session_id=%s job=%s", job.session_id, job.key,
                )
        except Exception as e:
            job.append("error", {"msg": f"聊天发生错误: {e!s}"})
            job.append("message_end", None)
            job.finish("error")
        finally:
            try:
                agent.close()
            except Exception:
                logger.warning("chat_stop agent.close 失败，已记录", exc_info=True)

    async def chat_events(self, get):
        """
        SSE 事件流：先回放 last_id 之后的事件，再实时跟随，直到任务终态。
        前端断开后可带 last_id 重连续传，后端任务不受影响。
        """
        ok, sid = _validate_session_id(get.get('session_id'))
        if not ok:
            yield self.sse_pack(event="error", data={"msg": sid})
            return
        session_id = sid
        job_key = get.get('job', '')
        if job_key:
            job = chat_jobs.get(job_key)
        else:
            # 兼容：取该会话最新的任务
            act = chat_jobs.active_for_session(session_id)
            job = act[-1][1] if act else None
        if not job:
            yield self.sse_pack(event="error", data={"msg": "没有可订阅的任务（不存在或已过期）"})
            return

        # 渠道日志：SSE 订阅同样记录来源，便于小程序/clawbot 断流/续传问题定位
        _mp = _is_terminal_channel(get)
        logger.info(
            "[chat_events] channel=%s session=%s job=%s last_id=%s",
            _client_channel(get) or "web",
            session_id, job.key, get.get('last_id', -1),
        )
        # 小程序（终端用户）剥掉思维链等内部事件；网页管理端保留全部
        drop = HIDDEN_EVENTS_FOR_TERMINAL if _mp else None

        try:
            last_id = int(get.get('last_id', -1))
        except (TypeError, ValueError):
            last_id = -1

        sent_end = False
        idle_ticks = 0  # 空闲 tick 计数，用于 SSE keepalive，防止客户端 sock_read 超时断开
        # P2-38: SSE 超时可配置，默认 30min（原 900s=15min 对长任务不够）
        max_idle_seconds = int(os.environ.get("SSE_IDLE_TIMEOUT", "1800"))
        idle_start = asyncio.get_event_loop().time()
        # P1-75: message_end 后继续挂一小段时间（默认 10s），让后台任务结果
        # 有机会通过孤儿回调/notify_done 注入到同一个 job，前端能一次性收到。
        _ORPHAN_APPEND_WINDOW = float(os.environ.get("AI_AGENT_ORPHAN_APPEND_WINDOW", "10"))
        _post_end_deadline = None
        while True:
            pending, status = job.snapshot_from(last_id, drop)
            for e in pending:
                yield self.sse_pack(event=e["event"], id=e["id"], data=e["data"])
                last_id = e["id"]
                if e["event"] == "message_end":
                    sent_end = True
                    # 记录 message_end 到达时间，后续窗口期以此时为基准
                    _post_end_deadline = asyncio.get_event_loop().time() + _ORPHAN_APPEND_WINDOW
            # 判断是否退出：job 已结束 且 无新事件 且 过了追加窗口
            if status != "running" and not pending:
                if _post_end_deadline is None:
                    # 续传场景：last_id 已越过 message_end，本轮不会再看到该事件，
                    # 窗口计时永远无法启动。若不在此直接退出，循环会一直挂到
                    # max_idle_seconds（900s）才超时——表现为"续传后客户端卡死"。
                    break
                if asyncio.get_event_loop().time() >= _post_end_deadline:
                    break
            await asyncio.sleep(0.15)
            # 空闲 keepalive：任务运行中但暂时无新事件（模型思考/工具执行中），
            # 每 ~10s 发一条 SSE 注释行维持连接，避免客户端 aiohttp sock_read 超时断开
            idle_ticks += 1
            if not pending and idle_ticks >= 66:  # 0.15s * 66 ≈ 10s
                elapsed = asyncio.get_event_loop().time() - idle_start
                if elapsed >= max_idle_seconds:
                    yield self.sse_pack(event="error", data={"msg": f"SSE 订阅超时（{max_idle_seconds}s），请刷新页面重试"})
                    return
                yield ": keepalive\n\n"
                idle_ticks = 0

        # 兜底：确保前端一定收到结束信号
        if not sent_end:
            if status == "stopped":
                yield self.sse_pack(event="error", data={"msg": "任务已停止"})
            yield self.sse_pack(event="message_end", id=last_id + 1)

    def chat_status(self, get):
        ok, sid = _validate_session_id(get.get('session_id'))
        if not ok:
            return public.returnMsg(False, sid)
        session_id = sid
        jobs = chat_jobs.active_for_session(session_id)
        if not jobs:
            return public.return_data(True, data={"running": False, "status": "none",
                                                  "last_id": -1, "elapsed": 0, "jobs": []})
        jobs_list = []
        for key, j in sorted(jobs, key=lambda kv: kv[1].created_at):
            with j._lock:
                # 必须用单调递增的 _seq，不能用 len(events)-1：截断后 len 恒等于
                # _MAX_EVENTS，会把 last_id 永远上报成上限值，前端据此续传即卡死。
                lid = j._seq
                st = j.status
            jobs_list.append({
                "job": key,
                "label": j.label,
                "status": st,
                "running": j.is_running,
                "last_id": lid,
                "started_at": int(j.created_at),
                "elapsed": max(0, int(time.time() - j.created_at)),
            })
        newest = jobs_list[-1]
        # 顶层 elapsed：取仍在运行的任务中耗时最长者；若都已结束则取最新任务
        running_elapsed = [j["elapsed"] for j in jobs_list if j["running"]]
        elapsed_top = max(running_elapsed) if running_elapsed else newest["elapsed"]
        return public.return_data(True, data={
            "running": any(x["running"] for x in jobs_list),
            "status": newest["status"],
            "last_id": newest["last_id"],
            "elapsed": elapsed_top,
            "jobs": jobs_list,
        })

    def chat_stop(self, get):
        ok, sid = _validate_session_id(get.get('session_id'))
        if not ok:
            return public.returnMsg(False, sid)
        session_id = sid
        job_key = str(get.get('job', '') or '').strip()
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        targets = chat_jobs.stop(key=job_key or None, session_id=None if job_key else session_id)
        if not targets:
            return public.returnMsg(False, "没有可停止的任务")
        return public.return_data(True, data={"stopped": len(targets)})


