"""平台扩展 REST API —— /api/v2/{teams,channels,storage}/*

对齐 Octop 对应路由语义，走 BT 风格（public.return_data 包裹）。
所有路由挂在新前缀 /api/v2/* 下，与现有 /api/* 完全隔离，
由 BT 全局 auth 中间件自动保护。

端点：
  teams   : GET/POST /api/v2/teams, GET/PATCH/DELETE /api/v2/teams/{id}, GET /api/v2/teams/template
  channels: GET /api/v2/channels, GET /api/v2/channels/types, POST /api/v2/channels/dispatch
  storage : GET/POST /api/v2/storage/backends, GET/PATCH/DELETE /api/v2/storage/backends/{name},
            POST /api/v2/storage/backends/{name}/probe, GET /api/v2/storage/files
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import public
from platform_ext import channels as ch
from platform_ext import storage as st
from platform_ext import teams as tm

logger = logging.getLogger("platform-ext-api")

router = APIRouter(prefix="/api/v2", tags=["platform-ext"])


async def _body(request: Request) -> dict[str, Any]:
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            data = await request.json()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return dict(request.query_params)


def _ok(data: Any = None, msg: str = "") -> JSONResponse:
    return JSONResponse(public.return_data(True, data=data, msg=msg))


def _err(msg: str) -> JSONResponse:
    return JSONResponse(public.returnMsg(False, msg))


# ══════════════════════ Teams ══════════════════════════════
@router.get("/teams")
async def api_teams_list():
    return _ok({"teams": tm.list_teams()})


@router.get("/teams/template")
async def api_teams_template():
    return _ok(tm.team_template())


@router.post("/teams")
async def api_teams_create(request: Request):
    body = await _body(request)
    try:
        team = tm.create_team(
            str(body.get("name") or ""),
            body.get("member_ids") or [],
            description=str(body.get("description") or ""),
            host_id=str(body.get("host_id") or ""),
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("create team failed")
        return _err(f"创建失败: {exc}")
    return _ok(team, "已创建")


@router.get("/teams/{team_id}")
async def api_teams_get(team_id: str):
    t = tm.get_team(team_id)
    if not t:
        return _err(f"团队不存在: {team_id}")
    return _ok(t)


@router.patch("/teams/{team_id}")
async def api_teams_update(team_id: str, request: Request):
    body = await _body(request)
    try:
        t = tm.update_team(
            team_id,
            name=body.get("name"),
            description=body.get("description"),
            member_ids=body.get("member_ids"),
            host_id=body.get("host_id"),
        )
    except ValueError as exc:
        return _err(str(exc))
    if not t:
        return _err(f"团队不存在: {team_id}")
    return _ok(t, "已更新")


@router.delete("/teams/{team_id}")
async def api_teams_delete(team_id: str):
    ok = tm.delete_team(team_id)
    return _ok({"deleted": ok}, "已删除") if ok else _err(f"团队不存在: {team_id}")


# ══════════════════════ Channels ═══════════════════════════
@router.get("/channels")
async def api_channels_list():
    reg = ch.get_registry()
    return _ok({"channels": reg.list(), "types": reg.list_types()})


@router.get("/channels/types")
async def api_channels_types():
    return _ok({"types": ch.get_registry().list_types()})


@router.post("/channels/dispatch")
async def api_channels_dispatch(request: Request):
    """把一条文本消息投递给指定通道（用于测试/外部集成）。"""
    body = await _body(request)
    channel_id = str(body.get("channel_id") or "").strip()
    user_key = str(body.get("user_key") or "").strip()
    text = str(body.get("text") or "")
    if not channel_id or not user_key:
        return _err("缺少 channel_id 或 user_key")
    try:
        await ch.get_registry().dispatch_text(channel_id, user_key, text, body.get("metadata") or {})
    except KeyError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("channel dispatch failed")
        return _err(f"投递失败: {exc}")
    return _ok({"dispatched": True}, "已投递")


# ══════════════════════ Storage ════════════════════════════
@router.get("/storage/backends")
async def api_storage_list():
    return _ok({"backends": st.list_backends(), "supported_kinds": sorted(st.SUPPORTED_KINDS)})


@router.post("/storage/backends")
async def api_storage_create(request: Request):
    body = await _body(request)
    try:
        b = st.create_backend(
            str(body.get("kind") or "local"),
            str(body.get("name") or ""),
            root=body.get("root"),
            docker_image=body.get("docker_image"),
            enabled=bool(body.get("enabled", True)),
        )
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("create storage backend failed")
        return _err(f"创建失败: {exc}")
    return _ok(b, "已创建")


@router.get("/storage/backends/{name}")
async def api_storage_get(name: str):
    b = st.get_backend(name)
    if not b:
        return _err(f"后端不存在: {name}")
    return _ok(b)


@router.patch("/storage/backends/{name}")
async def api_storage_update(name: str, request: Request):
    body = await _body(request)
    b = st.update_backend(
        name,
        enabled=body.get("enabled"),
        root=body.get("root"),
        docker_image=body.get("docker_image"),
    )
    if not b:
        return _err(f"后端不存在: {name}")
    return _ok(b, "已更新")


@router.delete("/storage/backends/{name}")
async def api_storage_delete(name: str):
    ok = st.delete_backend(name)
    return _ok({"deleted": ok}, "已删除") if ok else _err(f"后端不存在: {name}")


@router.post("/storage/backends/{name}/probe")
async def api_storage_probe(name: str):
    res = st.probe_backend(name)
    return _ok(res) if res.get("ok") else _err(res.get("error", "探测失败"))


@router.get("/storage/files")
async def api_storage_files(request: Request):
    """列出当前活动后端某目录下的文件。"""
    rel = request.query_params.get("path", "")
    return _ok({"path": rel, "entries": st.list_dir(rel), "resolved": st.safe_resolve(rel)})