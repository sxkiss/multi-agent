"""备份管理 API。

优-1 第二阶段：从 web_server.py 拆出的备份路由。
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

import public

logger = logging.getLogger(__name__)

_BASE_DIR = ""


async def _params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("备份请求参数解析失败，已忽略 body", exc_info=True)
    return params


def register_backup_routes(app, base_dir: str) -> None:
    global _BASE_DIR
    _BASE_DIR = base_dir

    @app.get("/api/backups")
    async def api_backups():
        from chat_client import backup
        return JSONResponse(public.return_data(True, data=backup.list_backups(_BASE_DIR)))

    @app.post("/api/backups/create")
    async def api_backups_create(request: Request):
        from chat_client import backup
        params = await _params(request)
        res = backup.create_backup(
            _BASE_DIR,
            label=str(params.get("label") or ""),
            keep=int(params.get("keep") or 10),
        )
        return JSONResponse(
            public.return_data(True, data=res) if res.get("status")
            else public.returnMsg(False, res.get("msg", "备份失败"))
        )

    @app.post("/api/backups/restore")
    async def api_backups_restore(request: Request):
        from chat_client import backup
        params = await _params(request)
        res = backup.restore_backup(
            _BASE_DIR,
            str(params.get("name") or ""),
            dry_run=bool(params.get("dry_run", True)),
        )
        return JSONResponse(
            public.return_data(True, data=res) if res.get("status")
            else public.returnMsg(False, res.get("msg", "恢复失败"))
        )

    @app.post("/api/backups/auto")
    async def api_backups_auto(request: Request):
        """启动/停止自动备份。action=start|stop|status"""
        from chat_client import backup
        params = await _params(request)
        action = str(params.get("action") or "status").lower()
        if action == "start":
            res = backup.start_auto_backup(
                _BASE_DIR,
                interval_sec=int(params.get("interval_sec") or 3600),
                keep=int(params.get("keep") or 10),
            )
        elif action == "stop":
            res = backup.stop_auto_backup()
        else:
            res = backup.auto_backup_status()
        return JSONResponse(public.return_data(True, data=res))
