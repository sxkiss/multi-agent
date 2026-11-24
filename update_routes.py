"""框架自动更新 API 路由。

注册到 web_server.py：register_update_routes(app, BASE_DIR)

端点：
  GET  /api/update/check      检测更新（只读，生成/读 version.json，不安装）
  GET  /api/update/status     当前版本 + 待更新情报 + 通道
  POST /api/update/apply      执行更新（前端点「立即更新」才调用）
  POST /api/update/rollback   回滚到更新前快照

响应统一用 public.return_data：{"status", "data", "msg"}。
"""
from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

import public
import update_manager

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
            logger.warning("更新请求参数解析失败，已忽略 body", exc_info=True)
    return params


def _resp(res: dict) -> JSONResponse:
    """把 update_manager 的 {ok,msg,data} 转成项目统一响应。"""
    if res.get("ok"):
        return JSONResponse(public.return_data(True, data=res.get("data"), msg=res.get("msg", "")))
    return JSONResponse(public.return_data(False, data=res.get("data"), msg=res.get("msg", "")))


def register_update_routes(app, base_dir: str) -> None:
    global _BASE_DIR
    _BASE_DIR = base_dir

    @app.get("/api/update/check")
    async def api_update_check(request: Request):
        try:
            params = await _params(request)
            force = str(params.get("force", "")).lower() in ("1", "true", "yes")
            return _resp(update_manager.check_update(_BASE_DIR, force=force))
        except Exception as e:
            logger.exception("[update] check 失败")
            return _resp(update_manager._fail(f"检测失败：{e}"))

    @app.get("/api/update/status")
    async def api_update_status():
        try:
            # 启动后首次访问：没有 version.json 就生成基线（不提示更新）
            update_manager.bootstrap_version(_BASE_DIR)
            return _resp(update_manager.get_status(_BASE_DIR))
        except Exception as e:
            logger.exception("[update] status 失败")
            return _resp(update_manager._fail(f"读取状态失败：{e}"))

    @app.post("/api/update/apply")
    async def api_update_apply(request: Request):
        try:
            params = await _params(request)
            channel = str(params.get("channel") or "") or None
            return _resp(update_manager.apply_update(_BASE_DIR, channel=channel))
        except Exception as e:
            logger.exception("[update] apply 失败")
            return _resp(update_manager._fail(f"更新失败：{e}"))

    @app.post("/api/update/rollback")
    async def api_update_rollback(request: Request):
        try:
            params = await _params(request)
            name = str(params.get("name") or "") or None
            return _resp(update_manager.rollback(_BASE_DIR, name or ""))
        except Exception as e:
            logger.exception("[update] rollback 失败")
            return _resp(update_manager._fail(f"回滚失败：{e}"))
