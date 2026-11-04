"""聊天与会话 API。

优-1 第四阶段：从 web_server.py 拆出的聊天/会话域路由。
"""
from __future__ import annotations

import json
import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

import public

logger = logging.getLogger(__name__)

_BASE_DIR = ""
_AGENT = None


async def _params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("聊天接口参数解析失败，已忽略 body", exc_info=True)
    return params


def _assert_session_owner(request: Request, params: dict):
    """在非管理员请求中，验证 session_id 是否属于当前 token 对应的主人。"""
    from auth import extract_token, verify_token, is_admin_token

    token = extract_token(request)
    if not token or is_admin_token(_BASE_DIR, token):
        return
    payload = verify_token(_BASE_DIR, token)
    if not payload:
        raise HTTPException(status_code=403, detail="未授权")
    sub = str(payload.get("sub") or "")
    session_id = str(params.get("session_id") or "")
    if sub.startswith("card:"):
        allowed = f"card_{sub[5:]}"
        if not session_id.startswith(allowed):
            raise HTTPException(status_code=403, detail="无权访问该会话")
    elif sub.startswith("wx:"):
        if not session_id.startswith("wx_"):
            raise HTTPException(status_code=403, detail="无权访问该会话")
    elif sub.startswith("service:"):
        return
    else:
        raise HTTPException(status_code=403, detail="未授权")


def register_chat_routes(app, base_dir: str, agent_main) -> None:
    global _BASE_DIR, _AGENT
    _BASE_DIR = base_dir
    _AGENT = agent_main

    @app.get("/api/chat")
    async def api_chat_get(request: Request):
        params = dict(request.query_params)
        return StreamingResponse(
            _AGENT.chat(params),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.post("/api/chat")
    async def api_chat_post(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("plugin chat SSE 参数提取异常", exc_info=True)
        return StreamingResponse(
            _AGENT.chat(params),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.get("/api/chat/history")
    async def api_chat_history(request: Request):
        params = dict(request.query_params)
        return JSONResponse(_AGENT.get_chat_historys(params))

    @app.post("/api/chat/start")
    async def api_chat_start(request: Request):
        params = await _params(request)
        _assert_session_owner(request, params)
        return JSONResponse(_AGENT.chat_start(params))

    @app.get("/api/chat/events")
    async def api_chat_events(request: Request):
        params = dict(request.query_params)
        _assert_session_owner(request, params)
        return StreamingResponse(
            _AGENT.chat_events(params),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/chat/status")
    async def api_chat_status(request: Request):
        params = dict(request.query_params)
        _assert_session_owner(request, params)
        return JSONResponse(_AGENT.chat_status(params))

    @app.post("/api/chat/stop")
    async def api_chat_stop(request: Request):
        params = await _params(request)
        _assert_session_owner(request, params)
        return JSONResponse(_AGENT.chat_stop(params))

    @app.get("/api/chat/messages")
    async def api_chat_messages(request: Request):
        params = dict(request.query_params)
        _assert_session_owner(request, params)
        return JSONResponse(_AGENT.get_chat(params))

    @app.get("/api/session/context")
    async def api_session_context(request: Request):
        """上下文用量分解：估算会话各段 token 占用。"""
        params = dict(request.query_params)
        _assert_session_owner(request, params)
        from chat_client import session_tools
        sid = str(params.get("session_id") or "")
        win = int(params.get("window_tokens") or (
            _AGENT.config.get("context_window_kb", 256) * 1024
        ))
        return JSONResponse(public.return_data(
            True,
            data=session_tools.analyze_context(
                _BASE_DIR, sid, context_window_tokens=win
            ),
        ))

    @app.get("/api/session/export")
    async def api_session_export(request: Request):
        """导出会话为 Markdown/JSON。"""
        params = dict(request.query_params)
        _assert_session_owner(request, params)
        from chat_client import session_tools
        res = session_tools.export_session(
            _BASE_DIR,
            str(params.get("session_id") or ""),
            fmt=str(params.get("format") or "markdown"),
        )
        return JSONResponse(
            public.return_data(True, data=res) if res.get("status")
            else public.returnMsg(False, res.get("msg", "导出失败"))
        )

    @app.post("/api/session/fork")
    async def api_session_fork(request: Request):
        """从指定消息分叉出一个新会话（原会话不变）。"""
        params = await _params(request)
        _assert_session_owner(request, params)
        from chat_client import session_tools
        res = session_tools.fork_session(
            _BASE_DIR,
            str(params.get("session_id") or ""),
            up_to_message_id=str(params.get("message_id") or ""),
            new_session_id=str(params.get("new_session_id") or ""),
        )
        return JSONResponse(
            public.return_data(True, data=res) if res.get("status")
            else public.returnMsg(False, res.get("msg", "分叉失败"))
        )

    @app.get("/api/sessions/stats")
    async def api_sessions_stats(request: Request):
        """列出各会话规模统计。"""
        from chat_client import session_tools
        return JSONResponse(public.return_data(
            True, data=session_tools.list_session_stats(_BASE_DIR)
        ))

    @app.get("/api/context/archives")
    async def api_context_archives(session_id: str = ""):
        """工具结果归档统计（上下文治理观测）。"""
        from chat_client.tool_archive import archive_stats
        return JSONResponse(public.return_data(
            True, data=archive_stats(_BASE_DIR, session_id)
        ))

    @app.get("/api/context/breakdown")
    async def api_context_breakdown(
        session_id: str, context_window_tokens: int = 256 * 1024
    ):
        """会话上下文分段占用。"""
        from chat_client.session_tools import analyze_context
        try:
            sys_prompt = ""
            tool_defs = ""
            skills_txt = ""
            try:
                from chat_client.tools import registry as reg
                schemas = getattr(reg, "_schemas", None) or []
                tool_defs = json.dumps(schemas, ensure_ascii=False) if schemas else ""
            except Exception:
                pass
            try:
                from chat_client.skills import skill_manager
                skills_txt = skill_manager.generate_skill_summary() or ""
            except Exception:
                pass
            return JSONResponse(public.return_data(True, data=analyze_context(
                _BASE_DIR,
                session_id,
                context_window_tokens=context_window_tokens,
                system_prompt=sys_prompt,
                tool_definitions=tool_defs,
                skills=skills_txt,
            )))
        except Exception:
            logger.warning("上下文分析失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "分析上下文失败"))

    @app.post("/api/chat/delete")
    async def api_chat_delete(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("plugin del_chat 参数提取异常", exc_info=True)
        return JSONResponse(_AGENT.del_chat(params))

    @app.post("/api/chat/delete_batch")
    async def api_chat_delete_batch(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(public.returnMsg(False, "参数错误"))
        if not isinstance(body, dict):
            return JSONResponse(public.returnMsg(False, "参数错误"))
        ids = body.get("session_ids")
        if not isinstance(ids, list) or not ids:
            return JSONResponse(public.returnMsg(False, "缺少 session_ids"))
        deleted, failed = [], []
        for sid in ids:
            sid = str(sid).strip()
            if not sid:
                continue
            res = _AGENT.del_chat({"session_id": sid})
            if res.get("status"):
                deleted.append(sid)
            else:
                failed.append(sid)
        return JSONResponse(public.return_data(True, data={
            "deleted": deleted,
            "failed": failed,
            "count": len(deleted),
        }))

    @app.post("/api/chat/delete_message")
    async def api_chat_delete_message(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("delete_message 参数提取异常", exc_info=True)
        return JSONResponse(_AGENT.del_chat_msg(params))
