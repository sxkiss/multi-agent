"""CLI 配置 API（opencode / claude / codex）。

优-1 第四阶段：从 web_server.py 拆出的 CLI 配置与状态路由。
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_AGENT = None


def register_cli_config_routes(app, agent_main) -> None:
    global _AGENT
    _AGENT = agent_main

    @app.get("/api/opencode/config")
    async def api_opencode_config_get(request: Request):
        params = dict(request.query_params)
        return JSONResponse(_AGENT.opencode_get_config(params))

    @app.post("/api/opencode/config")
    async def api_opencode_config_post(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("plugin opencode_save 参数提取异常", exc_info=True)
        return JSONResponse(_AGENT.opencode_save_config(params))

    @app.get("/api/claude/config")
    async def api_claude_config_get(request: Request):
        params = dict(request.query_params)
        return JSONResponse(_AGENT.claude_get_config(params))

    @app.post("/api/claude/config")
    async def api_claude_config_post(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("plugin claude_save 参数提取异常", exc_info=True)
        return JSONResponse(_AGENT.claude_save_config(params))

    @app.get("/api/opencode/cli-config")
    async def api_opencode_cli_config_get(request: Request):
        params = dict(request.query_params)
        return JSONResponse(_AGENT.opencode_cli_get_config(params))

    @app.post("/api/opencode/cli-config")
    async def api_opencode_cli_config_post(request: Request):
        params = dict(request.query_params)
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("plugin opencode_cli_save 参数提取异常", exc_info=True)
        return JSONResponse(_AGENT.opencode_cli_save_config(params))

    @app.get("/api/codex/cli-config")
    async def api_codex_cli_config_get(request: Request):
        return JSONResponse(_AGENT.codex_cli_get_config({}))

    @app.post("/api/codex/cli-config")
    async def api_codex_cli_config_post(request: Request):
        params = {}
        try:
            body = await request.json()
            if isinstance(body, dict):
                params = body
        except Exception:
            logger.warning("plugin codex_cli_save 参数提取异常", exc_info=True)
        return JSONResponse(_AGENT.codex_cli_save_config(params))

    @app.get("/api/codex/status")
    async def api_codex_status(request: Request):
        return JSONResponse(_AGENT.codex_get_status({}))

    @app.post("/api/codex/test")
    async def api_codex_test(request: Request):
        return JSONResponse(_AGENT.codex_test_exec({}))

    @app.get("/api/opencode/status")
    async def api_opencode_status(request: Request):
        params = dict(request.query_params)
        return JSONResponse(_AGENT.opencode_status(params))
