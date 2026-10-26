"""连接器 REST API —— /api/v2/connectors/*

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

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

import public
from connector_hub import connectors_catalog as cat
from connector_hub import connectors_gateway as gw
from connector_hub import connectors_service as svc

logger = logging.getLogger("connector-hub-api")

router = APIRouter(prefix="/api/v2/connectors", tags=["connectors"])


def _card_prefix(request: Request) -> str | None:
    """从请求中提取卡密前缀，非卡密用户返回 None。"""
    try:
        from auth import extract_token, verify_token, is_card_token
        from web_server import BASE_DIR as _BASE
        token = extract_token(request)
        if not token or not is_card_token(_BASE, token):
            return None
        payload = verify_token(_BASE, token)
        sub = str(payload.get("sub") or "")
        if not sub.startswith("card:"):
            return None
        return sub[5:]
    except Exception:
        return None


async def _sse_keepalive():
    """streamable_http 客户端初始化后保持 SSE 通道活跃。"""
    yield "event: message\ndata: \n\n"
    while True:
        await asyncio.sleep(3600)


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
async def api_list_instances(request: Request):
    prefix = _card_prefix(request)
    return _ok({"instances": svc.list_instances(owner=prefix or "")})


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
    prefix = _card_prefix(request)
    try:
        inst = svc.create_instance(kind, name, creds, enabled, owner=prefix or "")
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("create connector instance failed")
        return _err(f"创建失败: {exc}")
    return _ok(inst, "已创建")


@router.get("/instances/{instance_id}")
async def api_get_instance(instance_id: str, request: Request):
    """实例详情。

    返回 ``creds`` 供前端编辑时回填，但 **secret 字段一律脱敏**（返回
    "***"），只有非 secret 字段（app_id / name / email 等）返回明文。
    这样既能「回填已有凭证」，又不会把密钥明文下发到浏览器。
    """
    prefix = _card_prefix(request)
    inst = svc.get_instance(instance_id, owner=prefix or "", include_secrets=True)
    if not inst:
        return _err(f"实例不存在: {instance_id}")
    # 按 catalog 的 secret 标记脱敏
    entry = cat.get_catalog_entry(inst.get("kind", ""))
    if entry is not None and isinstance(inst.get("creds"), dict):
        secret_keys = {f.key for f in entry.credential_fields if getattr(f, "secret", False)}
        inst["creds"] = {
            k: ("***" if k in secret_keys and v else v)
            for k, v in inst["creds"].items()
        }
    return _ok(inst)


@router.patch("/instances/{instance_id}")
async def api_update_instance(instance_id: str, request: Request):
    body = await _body(request)
    creds = body.get("credentials") or body.get("creds")
    prefix = _card_prefix(request)
    try:
        inst = svc.update_instance(
            instance_id,
            name=body.get("name"),
            creds=creds if isinstance(creds, dict) else None,
            enabled=body.get("enabled"),
            owner=prefix or "",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("update connector instance failed")
        return _err(f"更新失败: {exc}")
    if not inst:
        return _err(f"实例不存在: {instance_id}")
    return _ok(inst, "已更新")


@router.delete("/instances/{instance_id}")
async def api_delete_instance(instance_id: str, request: Request):
    prefix = _card_prefix(request)
    ok = svc.delete_instance(instance_id, owner=prefix or "")
    return _ok({"deleted": ok}, "已删除") if ok else _err(f"实例不存在: {instance_id}")


@router.post("/instances/{instance_id}/test")
async def api_test_instance(instance_id: str, request: Request):
    prefix = _card_prefix(request)
    inst = svc.get_instance(instance_id, owner=prefix or "", include_secrets=True)
    if not inst:
        return _err(f"实例不存在: {instance_id}")
    res = await asyncio.to_thread(svc.test_instance, instance_id)
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
            from connector_hub.http_mcp import amap_list_tools

            def _probe_remote() -> int:
                spec3 = svc._remote_spec_for(kind, creds, entry)
                if not spec3:
                    raise ValueError("无法从凭据推导 MCP 连接配置")
                url, headers, transport = spec3
                import asyncio as _asyncio
                tools = _asyncio.run(
                    amap_list_tools(url, None, transport, headers=headers)
                )
                return len(tools)

            # 在线程池里执行，避免 asyncio.run 与 uvicorn 事件循环冲突
            tool_count = await asyncio.to_thread(_probe_remote)
            return _ok({"ok": True, "tool_count": tool_count})
        if cat.is_gateway_kind(kind):
            gw.probe_gateway_credentials(kind, creds)
            return _ok({"ok": True})
        return _err(f"该连接器模式暂不支持测试: {entry.mcp_mode}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("test credentials failed for %s", kind)
        return _err(str(exc))


# ── OAuth2 授权流程 ─────────────────────────────────────────
@router.post("/oauth2/authorize")
async def api_oauth2_authorize(request: Request):
    """启动 OAuth2 授权流程，返回授权 URL 和 state。

    请求: {"kind": "notion"}
    响应: {"auth_url": "https://...", "state": "..."}
    """
    from connector_hub import oauth_manager as oauth

    body = await _body(request)
    kind = str(body.get("kind") or "").strip()
    if not kind:
        return _err("缺少 kind")
    provider = oauth.get_provider(kind)
    if not provider:
        return _err(f"连接器 {kind} 不支持 OAuth2")
    # 无授权端点的连接器（qcc/openalex）不支持标准授权流程
    if not provider.authorization_endpoint:
        return _err(f"连接器 {kind} 无标准 OAuth 授权端点，请使用手动粘贴 Token 方式")

    try:
        redirect_uri = oauth.build_callback_url(request)
        auth_url, state = oauth.create_auth_url(kind, redirect_uri)
    except Exception as exc:  # noqa: BLE001
        logger.exception("oauth2 authorize failed for %s", kind)
        return _err(f"启动授权失败: {exc}")

    return _ok({"auth_url": auth_url, "state": state})


@router.get("/oauth2/callback")
async def api_oauth2_callback(request: Request):
    """OAuth2 回调处理。

    查询参数: code、state
    响应: {"ok": true, "instance_id": "abc123", "kind": "notion"}
    """
    from connector_hub import oauth_manager as oauth

    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        return _err("缺少 code 或 state 参数")

    try:
        redirect_uri = oauth.build_callback_url(request)
        instance_id, kind, creds = await oauth.handle_callback(code, state, redirect_uri)
    except Exception as exc:  # noqa: BLE001
        logger.exception("oauth2 callback failed")
        return _err(f"OAuth 回调处理失败: {exc}")

    return _ok({"ok": True, "instance_id": instance_id, "kind": kind})


@router.post("/oauth2/refresh")
async def api_oauth2_refresh(request: Request):
    """手动刷新 OAuth2 token。

    请求: {"instance_id": "abc123"}
    响应: {"ok": true, "access_token": "..."}（脱敏）
    """
    from connector_hub import oauth_manager as oauth

    body = await _body(request)
    instance_id = str(body.get("instance_id") or "").strip()
    kind = str(body.get("kind") or "").strip()
    if not instance_id:
        return _err("缺少 instance_id")

    creds = oauth.get_credentials(kind, instance_id) if kind else None
    if creds is None:
        # 尝试从所有 token_cache 查找
        if kind:
            for cid, c in oauth.get_credentials_by_kind(kind):
                if cid == instance_id:
                    creds = c
                    break
        if creds is None:
            return _err("未找到该实例的 OAuth2 凭据")

    try:
        new_creds = await oauth.refresh_token(kind or creds.kind, instance_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("oauth2 refresh failed for %s", instance_id)
        return _err(f"刷新 Token 失败: {exc}")

    return _ok({
        "ok": True,
        "access_token": new_creds.access_token[:8] + "..." if new_creds.access_token else "",
        "instance_id": instance_id,
        "kind": creds.kind,
    })


@router.post("/oauth2/manual-token")
async def api_oauth2_manual_token(request: Request):
    """手动粘贴 OAuth2 Token（兜底方案）。

    请求: {"kind": "openalex", "token": "..."}
    响应: {"ok": true, "instance_id": "abc123"}
    """
    from connector_hub import oauth_manager as oauth
    from connector_hub import connectors_service as svc2

    body = await _body(request)
    kind = str(body.get("kind") or "").strip()
    token = str(body.get("token") or body.get("access_token") or "").strip()
    if not kind:
        return _err("缺少 kind")
    if not token:
        return _err("缺少 token")
    provider = oauth.get_provider(kind)
    if not provider:
        return _err(f"连接器 {kind} 不支持 OAuth2")
    if not provider.supports_manual_token:
        return _err(f"连接器 {kind} 不支持手动粘贴 Token")

    try:
        # 创建 connector 实例
        name = provider.name_zh or kind
        inst = svc2.create_instance(
            kind=kind,
            name=name,
            creds={"manual_token": token},
            enabled=True,
        )
        instance_id = inst["instance_id"]
        # 保存凭据到 token_cache（含 manual_token）
        oauth.save_manual_token(kind, token, instance_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("oauth2 manual-token failed for %s", kind)
        return _err(f"保存 Token 失败: {exc}")

    return _ok({"ok": True, "instance_id": instance_id, "kind": kind})


@router.get("/oauth2/status")
async def api_oauth2_status():
    """查询当前 OAuth2 状态。"""
    from connector_hub import oauth_manager as oauth

    return _ok(oauth.get_status())


# ── gateway 适配器 ──────────────────────────────────────────
@router.get("/gateway/kinds")
async def api_gateway_kinds():
    return _ok({"kinds": gw.list_gateway_kinds(), "external_cli": sorted(gw.EXTERNAL_CLI_KINDS)})


@router.get("/gateway/{kind}/tools")
async def api_gateway_tools(kind: str):
    tools = gw.mcp_tools_for_kind(kind)
    return _ok({"kind": kind, "tools": tools, "count": len(tools)})


# ── CLI 安装（飞书/企微/Agently/Obsidian）────────────────────
@router.get("/cli/status")
async def api_cli_status_all():
    """列出全部可自动安装的 CLI 连接器及其安装状态。"""
    from connector_hub.gateway_adapters import cli_install as ci
    out = []
    for kind in ("feishu-cli", "wecom-cli", "agently-cli", "obsidian-cli"):
        try:
            out.append(ci.cli_install_status(kind))
        except Exception as exc:  # noqa: BLE001
            out.append({"kind": kind, "error": str(exc)})
    return _ok({"clis": out})


@router.get("/cli/{kind}/status")
async def api_cli_status(kind: str):
    from connector_hub.gateway_adapters import cli_install as ci
    try:
        return _ok(ci.cli_install_status(kind))
    except ValueError as exc:
        return _err(str(exc))


@router.post("/cli/{kind}/install")
async def api_cli_install(kind: str):
    """安装指定连接器的 CLI（npm 自动安装 / Obsidian 注册）。"""
    from connector_hub.gateway_adapters import cli_install as ci
    if ci.get_cli_install_spec(kind) is None:
        return _err(f"该连接器不支持 CLI 安装: {kind}")
    try:
        res = ci.install_connector_cli(kind)
    except Exception as exc:  # noqa: BLE001
        logger.exception("cli install failed for %s", kind)
        return _err(f"安装失败: {exc}")
    return _ok(res, "已安装" if res.get("ok") else "安装失败") if res.get("ok") else _err(res.get("error", "安装失败"))


# ── 缓存管理 ────────────────────────────────────────────────
@router.post("/catalog/cache/reset")
async def api_reset_catalog_cache():
    """清除连接器目录缓存，下次请求重新读取 catalog.json。"""
    import connector_hub.connectors_catalog as cat
    cat.reset_catalog_cache()
    return _ok({"reset": True}, "缓存已清除")


# ── 同步到 mcp/config.json ──────────────────────────────────
@router.post("/sync")
async def api_sync(request: Request):
    body = await _body(request)
    dry = bool(body.get("dry_run", False))
    res = svc.sync_to_mcp_config(dry_run=dry)
    return _ok(res, "已同步" if not dry else "试运行")


# ── 内部 HTTP MCP 端点（gateway 连接器）────────────────────
@router.post("/internal/mcp/{kind}/{instance_id}")
async def api_internal_mcp(kind: str, instance_id: str, request: Request, token: str = Query(...)):
    """gateway 连接器的本地 MCP 端点：校验 internal_token，处理 JSON-RPC。"""
    from connector_hub.connectors_gateway import handle_mcp_request
    creds = svc.verify_internal_token(instance_id, token)
    if creds is None:
        return _err("invalid internal token")
    try:
        body = await request.json()
    except Exception:
        return _err("invalid JSON body")
    if not isinstance(body, dict):
        return _err("expected JSON object")
    method = str(body.get("method") or "")
    if method == "notifications/initialized":
        return Response(status_code=202)
    resp = await asyncio.to_thread(
        handle_mcp_request, kind=kind, creds=creds, body=body
    )
    if not resp:
        return Response(status_code=202)
    return JSONResponse(content=resp, headers={"content-type": "application/json"})


@router.get("/internal/mcp/{kind}/{instance_id}")
async def api_internal_mcp_get(kind: str, instance_id: str, token: str = Query(...)):
    """SSE 长连接（streamable_http 客户端 initialize 后打开）。"""
    creds = svc.verify_internal_token(instance_id, token)
    if creds is None:
        return _err("invalid internal token")
    return StreamingResponse(
        _sse_keepalive(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "connection": "keep-alive"},
    )


# ── CLI 类连接器的设备码授权（飞书 lark-cli）─────────────────
def _cli_device_login(kind: str, creds: dict[str, Any], *, domain: str = "") -> dict[str, Any]:
    """调用 CLI 的设备码授权，返回 verification URL + device_code。

    只支持 feishu-cli（lark-cli auth login --no-wait --json）。
    """
    import subprocess

    from connector_hub.gateway_adapters.local_shims import resolve_binary

    if kind != "feishu-cli":
        raise ValueError(f"连接器 {kind} 不支持设备码授权")

    # 复用 feishu_cli 的 env 准备逻辑（内含 config init，用 app_id/app_secret 初始化）
    from connector_hub.gateway_adapters import feishu_cli as fc

    binary = resolve_binary("lark-cli")
    env = fc._prepare_env(creds)

    argv = [binary, "auth", "login", "--no-wait", "--json"]
    if domain:
        argv.extend(["--domain", domain])

    proc = subprocess.run(
        argv, capture_output=True, text=True, env=env, timeout=60.0
    )
    raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    try:
        payload = json.loads(raw) if raw.startswith("{") else {}
    except json.JSONDecodeError:
        payload = {}
    if not payload:
        raise ValueError(f"lark-cli 输出无法解析: {raw[:300]}")
    if payload.get("ok") is False:
        err = payload.get("error") or {}
        raise ValueError(f"启动授权失败: {err.get('message') or raw[:200]}")
    # 兼容不同版本的字段名
    url = (
        payload.get("verification_uri_complete")
        or payload.get("verification_uri")
        or payload.get("verification_url")
        or payload.get("auth_url")
        or ""
    )
    device_code = payload.get("device_code") or ""
    user_code = payload.get("user_code") or ""
    if not url and not device_code:
        raise ValueError(f"未获取到授权链接: {raw[:300]}")
    return {
        "verification_uri": url,
        "verification_uri_complete": url,
        "user_code": user_code,
        "device_code": device_code,
        "expires_in": payload.get("expires_in"),
        "interval": payload.get("interval") or 5,
        "raw": payload,
    }


def _cli_device_poll(kind: str, creds: dict[str, Any], device_code: str) -> dict[str, Any]:
    """轮询 CLI 设备码授权结果。

    策略：先用 --device-code 尝试快速完成；失败时（包含 code 已被浏览器消费的场景）
    降级查询 auth status，确认 user identity 已就绪即视为授权成功。
    """
    import subprocess

    from connector_hub.gateway_adapters.local_shims import resolve_binary, run_cli
    from connector_hub.gateway_adapters import feishu_cli as fc

    if kind != "feishu-cli":
        raise ValueError(f"连接器 {kind} 不支持设备码授权")

    binary = resolve_binary("lark-cli")
    env = fc._prepare_env(creds)

    # 1) 尝试用 device_code 直接完成（code 未被消费时走此路径）
    if device_code:
        try:
            proc = subprocess.run(
                [binary, "auth", "login", "--device-code", device_code, "--json"],
                capture_output=True, text=True, env=env, timeout=180.0,
            )
            raw = (proc.stdout or "").strip() or (proc.stderr or "").strip()
            try:
                payload = json.loads(raw) if raw.startswith("{") else {}
            except json.JSONDecodeError:
                payload = {}
            ok = proc.returncode == 0 and payload.get("ok") is not False
            if ok:
                return {"status": "success", "message": "授权成功"}
            # 走到这里说明 code 已过期/被消费/无效，继续走 status 兜底
        except Exception:  # noqa: BLE001
            pass

    # 2) 兜底：查 auth status，看 user identity 是否已就绪
    try:
        out = run_cli([binary, "auth", "status", "--json"], env=env, timeout_s=30.0)
        payload: dict[str, Any] = {}
        if out.lstrip().startswith("{"):
            payload = json.loads(out)
        elif "{" in out:
            payload = json.loads(out[out.find("{"):])
        idents = payload.get("identities") or {}
        user = idents.get("user") or {}
        if isinstance(user, dict) and user.get("available"):
            return {"status": "success", "message": "授权成功"}
        return {
            "status": "pending",
            "message": user.get("message")
            or user.get("status")
            or payload.get("status")
            or "等待手机扫码",
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "message": str(exc)}


@router.post("/cli/{kind}/auth/start")
async def api_cli_auth_start(kind: str, request: Request):
    """启动 CLI 设备码授权，返回验证链接与二维码。

    请求: {"instance_id": "xxx"} 或 {"app_id": "...", "app_secret": "..."}
    响应: {"verification_uri": "...", "qrcode_image": "data:image/png;base64,...", "device_code": "..."}
    """
    body = await _body(request)
    instance_id = str(body.get("instance_id") or "").strip()
    creds: dict[str, Any] = {}
    if instance_id:
        inst = svc.get_instance(instance_id, include_secrets=True)
        if not inst:
            return _err("实例不存在")
        creds = inst.get("creds") or inst.get("creds_decoded") or {}
    else:
        creds = {k: body.get(k) for k in ("app_id", "app_secret") if body.get(k)}
    if not creds.get("app_id") or not creds.get("app_secret"):
        return _err("请先保存 App ID 与 App Secret")

    domain = str(body.get("domain") or "").strip()
    try:
        data = await asyncio.to_thread(_cli_device_login, kind, creds, domain=domain)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cli auth start failed for %s: %s", kind, exc)
        return _err(str(exc))

    # 生成二维码 PNG（复用 qr_login 的渲染工具）
    url = data.get("verification_uri") or ""
    if url:
        try:
            from platform_ext import qr_login
            data["qrcode_image"] = qr_login._qr_png_data_uri(url)
        except Exception:  # noqa: BLE001
            pass
    return _ok(data)


@router.post("/cli/{kind}/auth/poll")
async def api_cli_auth_poll(kind: str, request: Request):
    """轮询 CLI 设备码授权结果。

    请求: {"instance_id": "xxx", "device_code": "..."}
    响应: {"status": "pending|success", "message": "..."}
    """
    body = await _body(request)
    instance_id = str(body.get("instance_id") or "").strip()
    device_code = str(body.get("device_code") or "").strip()
    if not instance_id:
        return _err("缺少 instance_id")
    inst = svc.get_instance(instance_id, include_secrets=True)
    if not inst:
        return _err("实例不存在")
    creds = inst.get("creds") or inst.get("creds_decoded") or {}
    try:
        data = await asyncio.to_thread(_cli_device_poll, kind, creds, device_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cli auth poll failed for %s: %s", kind, exc)
        return _err(str(exc))
    return _ok(data)


@router.get("/cli/{kind}/auth/status")
async def api_cli_auth_status(kind: str, instance_id: str = Query(...)):
    """查询 CLI 授权状态（bot / user 身份是否就绪）。"""
    inst = svc.get_instance(instance_id, include_secrets=True)
    if not inst:
        return _err("实例不存在")
    creds = inst.get("creds") or inst.get("creds_decoded") or {}
    try:
        from connector_hub.gateway_adapters.local_shims import resolve_binary, run_cli
        from connector_hub.gateway_adapters import feishu_cli as fc

        binary = resolve_binary("lark-cli")
        env = fc._prepare_env(creds)
        out = await asyncio.to_thread(
            run_cli, [binary, "auth", "status", "--json"], env=env, timeout_s=60.0
        )
        payload: dict[str, Any] = {}
        if out.lstrip().startswith("{"):
            payload = json.loads(out)
        elif "{" in out:
            payload = json.loads(out[out.find("{"):])
        idents = payload.get("identities") or {}
        return _ok({
            "bot": (idents.get("bot") or {}).get("available", False),
            "user": (idents.get("user") or {}).get("available", False),
            "identity": payload.get("identity", ""),
        })
    except Exception as exc:  # noqa: BLE001
        return _err(f"查询授权状态失败: {exc}")
