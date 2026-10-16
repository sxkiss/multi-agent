"""连接器 REST API —— /api/v2/connectors/*

对齐 Octop connectors 路由的语义，但走 BT 风格（public.return_data 包裹）。
所有路由挂在新前缀 /api/v2/connectors/* 下，与现有 /api/* 完全隔离，
并由 BT 全局 auth 中间件自动保护（auth.enabled 时）。

端点：
  GET    /api/v2/connectors/catalog              连接器目录（27 个）
  GET    /api/v2/connectors/instances            实例列表
  POST   /api/v2/connectors/instances            创建实例
  GET    /api/v2/connectors/instances/{id}       实例详情
  PATCH  /api/v2/connectors/instances/{id}       更新实例
  DELETE /api/v2/connectors/instances/{id}       删除实例
  POST   /api/v2/connectors/instances/{id}/test  测试实例
  POST   /api/v2/connectors/test-credentials     测试任意凭据
  GET    /api/v2/connectors/gateway/kinds        gateway 适配器清单
  POST   /api/v2/connectors/sync                 同步到 mcp/config.json
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import public
from octop_bridge import connectors_catalog as cat
from octop_bridge import connectors_gateway as gw
from octop_bridge import connectors_service as svc

logger = logging.getLogger("octop-bridge-api")

router = APIRouter(prefix="/api/v2/connectors", tags=["connectors"])


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


# ── 目录 ────────────────────────────────────────────────────
@router.get("/catalog")
async def api_catalog(request: Request):
    """返回全部连接器目录（含本地化文案、凭据字段、是否支持）。"""
    locale = request.query_params.get("locale", "zh")
    entries = [e.to_api(locale) for e in cat.list_catalog_entries()]
    # 分组：按 category
    categories: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        categories.setdefault(e["category"], []).append(e)
    return _ok({
        "version": 1,
        "total": len(entries),
        "connectors": entries,
        "categories": categories,
    })


# ── 实例 CRUD ───────────────────────────────────────────────
@router.get("/instances")
async def api_list_instances():
    return _ok({"instances": svc.list_instances()})


@router.post("/instances")
async def api_create_instance(request: Request):
    body = await _body(request)
    kind = str(body.get("kind") or "").strip()
    name = str(body.get("name") or "").strip() or kind
    creds = body.get("credentials") or body.get("creds") or {}
    enabled = bool(body.get("enabled", True))
    if not kind:
        return _err("缺少 kind")
    if not isinstance(creds, dict):
        return _err("credentials 必须是对象")
    try:
        inst = svc.create_instance(kind, name, creds, enabled)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("create connector instance failed")
        return _err(f"创建失败: {exc}")
    return _ok(inst, "已创建")


@router.get("/instances/{instance_id}")
async def api_get_instance(instance_id: str):
    inst = svc.get_instance(instance_id)
    if not inst:
        return _err(f"实例不存在: {instance_id}")
    return _ok(inst)


@router.patch("/instances/{instance_id}")
async def api_update_instance(instance_id: str, request: Request):
    body = await _body(request)
    creds = body.get("credentials") or body.get("creds")
    try:
        inst = svc.update_instance(
            instance_id,
            name=body.get("name"),
            creds=creds if isinstance(creds, dict) else None,
            enabled=body.get("enabled"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("update connector instance failed")
        return _err(f"更新失败: {exc}")
    if not inst:
        return _err(f"实例不存在: {instance_id}")
    return _ok(inst, "已更新")


@router.delete("/instances/{instance_id}")
async def api_delete_instance(instance_id: str):
    ok = svc.delete_instance(instance_id)
    return _ok({"deleted": ok}, "已删除") if ok else _err(f"实例不存在: {instance_id}")


@router.post("/instances/{instance_id}/test")
async def api_test_instance(instance_id: str):
    res = svc.test_instance(instance_id)
    return _ok(res) if res.get("ok") else _err(res.get("error", "测试失败"))


@router.post("/test-credentials")
async def api_test_credentials(request: Request):
    """不落库，直接测试一组凭据（kind + credentials）。"""
    body = await _body(request)
    kind = str(body.get("kind") or "").strip()
    creds = body.get("credentials") or body.get("creds") or {}
    entry = cat.get_catalog_entry(kind)
    if entry is None:
        return _err(f"未知连接器: {kind}")
    try:
        if entry.mcp_mode == "remote":
            from octop_bridge.http_mcp import amap_list_tools
            url = svc._remote_url_for(kind, creds)
            token = creds.get("token") or creds.get("api_key")
            if not url:
                return _err("无法从凭据推导 MCP URL")
            import asyncio
            tools = asyncio.run(amap_list_tools(url, token, entry.remote_transport))
            return _ok({"ok": True, "tool_count": len(tools)})
        if cat.is_gateway_kind(kind):
            gw.probe_gateway_credentials(kind, creds)
            return _ok({"ok": True})
        return _err(f"该连接器模式暂不支持测试: {entry.mcp_mode}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("test credentials failed for %s", kind)
        return _err(str(exc))


# ── gateway 适配器 ──────────────────────────────────────────
@router.get("/gateway/kinds")
async def api_gateway_kinds():
    return _ok({"kinds": gw.list_gateway_kinds(), "external_cli": sorted(gw.EXTERNAL_CLI_KINDS)})


@router.get("/gateway/{kind}/tools")
async def api_gateway_tools(kind: str):
    tools = gw.mcp_tools_for_kind(kind)
    return _ok({"kind": kind, "tools": tools, "count": len(tools)})


# ── 同步到 mcp/config.json ──────────────────────────────────
@router.post("/sync")
async def api_sync(request: Request):
    body = await _body(request)
    dry = bool(body.get("dry_run", False))
    res = svc.sync_to_mcp_config(dry_run=dry)
    return _ok(res, "已同步" if not dry else "试运行")
