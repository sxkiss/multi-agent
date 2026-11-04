"""首页与 Agent SSE 入口路由。

优-1 第八阶段：把 web_server.py 最后的内联 API 路由抽出，
保留应用启动、中间件和基础设施初始化在主文件。
"""
from __future__ import annotations

import logging
import os

from fastapi import Request
from fastapi.responses import FileResponse, StreamingResponse

logger = logging.getLogger(__name__)


def register_entry_routes(app, base_dir: str, agent_main) -> None:
    @app.get("/")
    async def index(request: Request):
        """Serve the main HTML page. 卡密用户重定向到卡密登录页。"""
        from auth import extract_token, is_card_token
        from fastapi.responses import RedirectResponse
        token = extract_token(request)
        if token and is_card_token(base_dir, token):
            return RedirectResponse(url="/static/card-login.html", status_code=302)
        index_path = os.path.join(base_dir, "index.html")
        response = FileResponse(index_path, media_type="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/agent/run")
    async def api_agent_run_get(request: Request):
        params = dict(request.query_params)
        return StreamingResponse(
            agent_main.run_agent(params),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    @app.post("/api/agent/run")
    async def api_agent_run_post(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("plugin SSE streaming 参数提取异常", exc_info=True)
        return StreamingResponse(
            agent_main.run_agent(params),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
