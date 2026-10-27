"""卡密用户独立通道连接器管理。

为每个卡密用户提供隔离的通道配置与运行能力：卡密用户登录后登录后看到自己的通道列表，
可创建 / 配置 / 启停 / 删除通道实例。复用 platform_ext.gateway 的底层 ChannelManager，
实例 ID 以 card_<prefix>_<chid> 隔离。

API（挂在 /api/v2/card/*，需卡密 token）：
  GET    /api/v2/card/channels            列出我自己的通道
  POST   /api/v2/card/channels            创建通道
  GET    /api/v2/card/channels/types      可选的通道类型 + 字段定义
  PATCH  /api/v2/card/channels/{channel_id}  更新通道配置
  DELETE /api/v2/card/channels/{channel_id}  删除通道
  POST   /api/v2/card/channels/{channel_id}/start  启动
  POST   /api/v2/card/channels/{channel_id}/stop   停止
  POST   /api/v2/card/channels/{kind}/qrcode/generate  生成二维码（仅支持的扫码平台）
  POST   /api/v2/card/channels/{kind}/qrcode/poll      轮询扫码结果
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import public
from platform_ext import PROJECT_ROOT
from platform_ext import gateway as gw

logger = logging.getLogger("platform-ext-card-gateway")

router = APIRouter()

CARD_CHANNELS_FILE = "card_channels.json"

# 保护底层 ChannelManager 单例的并发访问（不同卡密用户同时启停时）
_manager_lock = threading.Lock()


# ── 数据存取（独立文件，与 card_keys.json 同目录，不入库） ──────────────
def _card_channels_path() -> str:
    return os.path.join(PROJECT_ROOT, CARD_CHANNELS_FILE)


def _load_all() -> dict[str, Any]:
    path = _card_channels_path()
    if not os.path.exists(path):
        return {}
    try:
        import json

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("[card-gw] card_channels.json 读取失败", exc_info=True)
        return {}


def _save_all(data: dict[str, Any]) -> bool:
    import json
    import tempfile

    path = _card_channels_path()
    try:
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except Exception:
        logger.error("[card-gw] card_channels.json 写入失败", exc_info=True)
        return False


def _user_record(prefix: str) -> dict[str, Any]:
    all_data = _load_all()
    return all_data.get(str(prefix), {"channels": []})


def _set_user_record(prefix: str, record: dict[str, Any]) -> bool:
    all_data = _load_all()
    all_data[str(prefix)] = record
    return _save_all(all_data)


# ── 鉴权辅助 ───────────────────────────────────────────────
def _prefix_from_request(request: Request) -> str | None:
    """从请求 token 中取出卡密前缀；非卡密 token 返回 None。"""
    from auth import extract_token, verify_token, is_card_token
    token = extract_token(request)
    if not token or not is_card_token(PROJECT_ROOT, token):
        return None
    payload = verify_token(PROJECT_ROOT, token)
    sub = str(payload.get("sub") or "")
    if not sub.startswith("card:"):
        return None
    return sub[5:]


def _require_card(request: Request):
    """鉴权检查：返回 (prefix, err_response)。err_response 为 None 表示通过。"""
    prefix = _prefix_from_request(request)
    if not prefix:
        return None, JSONResponse(
            public.returnMsg(False, "仅卡密用户可访问"), status_code=403)
    return prefix, None


# ── 对外视图 ───────────────────────────────────────────────
def _channel_public(ch: dict[str, Any]) -> dict[str, Any]:
    kind = str(ch.get("kind") or "")
    meta = gw.PLATFORM_KINDS.get(kind, {})
    instance_id = str(ch.get("instance_id") or ch.get("channel_id") or "")
    _qr_kinds = set()
    try:
        from platform_ext import qr_login
        _qr_kinds = set(qr_login.supported_qr_kinds())
    except Exception:
        pass
    return {
        "channel_id": ch.get("channel_id"),
        "kind": kind,
        "name": ch.get("name") or meta.get("label", kind),
        "label": meta.get("label", kind),
        "note": meta.get("note", ""),
        "enabled": bool(ch.get("enabled", True)),
        "configured": bool(ch.get("configured")),
        "running": instance_id in gw._running_instance_ids(),
        "sdk_installed": gw._sdk_available(kind),
        "supported_qr": kind in _qr_kinds,
        "created_at": ch.get("created_at", ""),
        "template": ch.get("template", ""),
        "mode": ch.get("mode", ""),
        "workspace": ch.get("workspace", ""),
        "config_fields": gw._config_fields_for(kind, ch.get("config", {})),
    }


# ── CRUD 逻辑 ──────────────────────────────────────────────
def list_channels(prefix: str) -> list[dict[str, Any]]:
    rec = _user_record(prefix)
    return [_channel_public(c) for c in rec.get("channels", [])]


def create_channel(prefix: str, kind: str, name: str, config: dict[str, Any]) -> dict[str, Any] | None:
    if kind not in gw.PLATFORM_KINDS:
        return None
    rec = _user_record(prefix)
    channels = rec.get("channels", [])
    ch_id = "ch_" + uuid.uuid4().hex[:12]
    instance_id = f"card_{prefix}_{ch_id}"
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    ch = {
        "channel_id": ch_id,
        "instance_id": instance_id,
        "kind": kind,
        "name": (name or gw.PLATFORM_KINDS[kind].get("label", kind))[:64],
        "enabled": True,
        "configured": gw._compute_configured(kind, config or {}),
        "created_at": now,
        "template": "",
        "mode": "",
        "workspace": "",
        "config": config or {},
    }
    channels.append(ch)
    rec["channels"] = channels
    _set_user_record(prefix, rec)
    return ch


def update_channel(prefix: str, channel_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    rec = _user_record(prefix)
    for ch in rec.get("channels", []):
        if ch.get("channel_id") == channel_id:
            # 更新允许字段
            if "name" in patch:
                ch["name"] = str(patch["name"])[:64]
            if "enabled" in patch:
                ch["enabled"] = bool(patch["enabled"])
            if "template" in patch:
                ch["template"] = str(patch.get("template") or "")[:64]
            if "mode" in patch:
                ch["mode"] = str(patch.get("mode") or "")[:32]
            if "workspace" in patch:
                ch["workspace"] = str(patch.get("workspace") or "")[:256]
            if "config" in patch and isinstance(patch["config"], dict):
                ch["config"].update(patch["config"])
                ch["configured"] = gw._compute_configured(ch["kind"], ch["config"])
            _set_user_record(prefix, rec)
            return ch
    return None


def delete_channel(prefix: str, channel_id: str) -> bool:
    rec = _user_record(prefix)
    before = len(rec.get("channels", []))
    rec["channels"] = [c for c in rec.get("channels", []) if c.get("channel_id") != channel_id]
    if len(rec["channels"]) == before:
        return False
    _set_user_record(prefix, rec)
    return True


async def start_channel(prefix: str, channel_id: str) -> tuple[bool, str]:
    rec = _user_record(prefix)
    ch = next((c for c in rec.get("channels", []) if c.get("channel_id") == channel_id), None)
    if not ch:
        return False, "通道不存在"
    kind = ch["kind"]
    cfg = gw._norm_cfg(kind, ch.get("config", {}))
    instance_id = ch["instance_id"]
    with _manager_lock:
        try:
            await gw._start_instance(instance_id, kind, cfg)
            rec = _user_record(prefix)
            for c in rec.get("channels", []):
                if c.get("channel_id") == channel_id:
                    c["running"] = True
                    break
            _set_user_record(prefix, rec)
            return True, "已启动"
        except Exception as e:
            logger.warning("[card-gw] 启动通道失败 %s/%s: %s", prefix, channel_id, e, exc_info=True)
            return False, str(e)


async def stop_channel(prefix: str, channel_id: str) -> bool:
    rec = _user_record(prefix)
    ch = next((c for c in rec.get("channels", []) if c.get("channel_id") == channel_id), None)
    if not ch:
        return False
    instance_id = ch["instance_id"]
    with _manager_lock:
        try:
            await gw._stop_instance(instance_id)
        except Exception as e:
            logger.warning("[card-gw] 停止通道失败 %s/%s: %s", prefix, channel_id, e, exc_info=True)
        rec = _user_record(prefix)
        for c in rec.get("channels", []):
            if c.get("channel_id") == channel_id:
                c["running"] = False
                break
        _set_user_record(prefix, rec)
        return True


# ── 路由 ───────────────────────────────────────────────────
@router.get("/card/channels")
async def api_card_channels_list(request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    return JSONResponse(public.return_data(True, data={"channels": list_channels(prefix)}))


@router.get("/card/channels/types")
async def api_card_channels_types(request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    kinds = {}
    for k, meta in gw.PLATFORM_KINDS.items():
        kinds[k] = {
            "label": meta.get("label", k),
            "note": meta.get("note", ""),
            "config_fields": meta.get("config_fields", []),
        }
    return JSONResponse(public.return_data(True, data={"types": kinds}))


@router.post("/card/channels")
async def api_card_channel_create(request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = str(body.get("kind") or "").strip()
    name = str(body.get("name") or "").strip()
    config = body.get("config") or {}
    if kind not in gw.PLATFORM_KINDS:
        return JSONResponse(public.returnMsg(False, f"不支持的通道类型: {kind}"), status_code=400)
    ch = create_channel(prefix, kind, name, config)
    if not ch:
        return JSONResponse(public.returnMsg(False, "创建失败"), status_code=500)
    return JSONResponse(public.return_data(True, data=ch))


@router.patch("/card/channels/{channel_id}")
async def api_card_channel_update(channel_id: str, request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    ch = update_channel(prefix, channel_id, body)
    if not ch:
        return JSONResponse(public.returnMsg(False, "通道不存在"), status_code=404)
    return JSONResponse(public.return_data(True, data=ch))


@router.delete("/card/channels/{channel_id}")
async def api_card_channel_delete(channel_id: str, request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    if not delete_channel(prefix, channel_id):
        return JSONResponse(public.returnMsg(False, "通道不存在"), status_code=404)
    return JSONResponse(public.return_data(True, data={"channel_id": channel_id}))


@router.post("/card/channels/{channel_id}/start")
async def api_card_channel_start(channel_id: str, request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    ok, msg = await start_channel(prefix, channel_id)
    if not ok:
        return JSONResponse(public.returnMsg(False, f"启动失败: {msg}"), status_code=500)
    return JSONResponse(public.return_data(True, data={"channel_id": channel_id, "running": True}))


# ── 凭据校验辅助 ──────────────────────────────────────────
def _probe_channel_config(kind: str, cfg: dict[str, Any]) -> str:
    """校验凭据能否被 SDK 接受，返回成功说明；失败抛异常。

    不启动真实连接，只做三件事：
      1. 必填字段完整性
      2. SDK 模块可导入
      3. 用归一化后的配置构造 SDK 的 Config 对象（字段类型/必填校验）
    """
    meta = gw.PLATFORM_KINDS.get(kind) or {}
    required = [f["name"] for f in meta.get("config_fields", []) if f.get("required")]
    missing = [n for n in required if not str(cfg.get(n) or "").strip()]
    if missing:
        raise ValueError(f"缺少必填字段: {', '.join(missing)}")

    if not gw._sdk_available(kind):
        raise ValueError(f"平台 {kind} 的通道 SDK 不可用，请先安装依赖")

    norm = gw._norm_cfg(kind, cfg)
    mod = __import__(f"octop_gateway.channels.{kind}", fromlist=["*"])

    import dataclasses as _dc

    # 收集模块内所有 dataclass Config 类，按「最贴合 norm 键集合」打分选最优
    candidates = []
    for name in dir(mod):
        if name.startswith("_"):
            continue
        cls = getattr(mod, name)
        if not (_dc.is_dataclass(cls) and hasattr(cls, "__dataclass_fields__")):
            continue
        field_names = {f.name for f in _dc.fields(cls)}
        score = len(field_names & set(norm.keys()))
        candidates.append((score, name, cls, field_names))

    if not candidates:
        return "凭据格式正确（该通道无 Config 校验）"

    candidates.sort(key=lambda x: -x[0])
    score, cls_name, config_cls, field_names = candidates[0]

    # 顶层 Config 类（如 WeixinConfig 的 accounts / FeishuConfig 的 app_id）
    # 优先于内部嵌套类（WeixinAccountConfig）；同分时取字段更少的（更基础）
    safe_cfg = {k: v for k, v in norm.items() if k in field_names}
    try:
        config_cls(**safe_cfg)
    except TypeError:
        # 顶层 Config 需要嵌套结构时（如 weixin accounts），退一步校验嵌套项
        if kind == "weixin" and isinstance(norm.get("accounts"), list):
            accounts = norm["accounts"]
            if not accounts:
                raise ValueError("微信通道需要 token + account_id 才能建立账号")
            acc_cls = getattr(mod, "WeixinAccountConfig", None)
            if acc_cls is not None:
                acc_cls(**accounts[0])
        else:
            raise
    return f"凭据格式正确，SDK 可用（{cls_name}）"


@router.post("/card/channels/test")
async def api_card_channel_test_fields(request: Request):
    """不依赖已有通道，直接测试一组凭据是否有效（创建表单用）。"""
    prefix, err = _require_card(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = str(body.get("kind") or "").strip()
    cfg = body.get("config") or {}
    if kind not in gw.PLATFORM_KINDS:
        return JSONResponse(public.returnMsg(False, f"不支持的通道类型: {kind}"), status_code=400)
    try:
        msg = _probe_channel_config(kind, cfg)
        return JSONResponse(public.return_data(True, data={"kind": kind, "message": msg}))
    except ValueError as e:
        return JSONResponse(public.returnMsg(False, str(e)), status_code=400)
    except Exception as e:
        logger.warning("[card-gw] 测试凭据 %s 失败: %s", kind, e, exc_info=True)
        return JSONResponse(public.returnMsg(False, f"测试失败: {e}"), status_code=500)


@router.post("/card/channels/{channel_id}/test")
async def api_card_channel_test(channel_id: str, request: Request):
    """测试通道凭据是否可用（不启动实例，仅校验字段和 SDK 连通性）。"""
    prefix, err = _require_card(request)
    if err:
        return err
    rec = _user_record(prefix)
    ch = next((c for c in rec.get("channels", []) if c.get("channel_id") == channel_id), None)
    if not ch:
        return JSONResponse(public.returnMsg(False, "通道不存在"), status_code=404)
    kind = ch["kind"]
    if kind not in gw.PLATFORM_KINDS:
        return JSONResponse(public.returnMsg(False, f"不支持的通道类型: {kind}"), status_code=400)
    cfg = ch.get("config", {})
    try:
        msg = _probe_channel_config(kind, cfg)
        return JSONResponse(public.return_data(True, data={"kind": kind, "message": msg}))
    except ValueError as e:
        return JSONResponse(public.returnMsg(False, str(e)), status_code=400)
    except Exception as e:
        logger.warning("[card-gw] 测试通道 %s/%s 失败: %s", prefix, channel_id, e, exc_info=True)
        return JSONResponse(public.returnMsg(False, f"测试失败: {e}"), status_code=500)


@router.post("/card/channels/{channel_id}/stop")
async def api_card_channel_stop(channel_id: str, request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    if not await stop_channel(prefix, channel_id):
        return JSONResponse(public.returnMsg(False, "通道不存在"), status_code=404)
    return JSONResponse(public.return_data(True, data={"channel_id": channel_id, "running": False}))


@router.post("/card/channels/{kind}/qrcode/generate")
async def api_card_qrcode_generate(kind: str, request: Request):
    """生成扫码二维码（仅支持的扫码平台）。扫码后凭据自动写入指定实例。"""
    prefix, err = _require_card(request)
    if err:
        return err
    from platform_ext import qr_login
    if kind not in qr_login.supported_qr_kinds():
        return JSONResponse(public.returnMsg(False, f"平台 {kind} 不支持扫码登录。支持的扫码平台: {', '.join(qr_login.supported_qr_kinds())}"), status_code=400)
    try:
        body = await request.json() if request else {}
    except Exception:
        body = {}
    session_id = f"card_{prefix}_{str(body.get('instance_id', '')).strip() or 'default'}"
    instance_id = str(body.get("instance_id") or "").strip() or None
    try:
        data = await qr_login.generate(kind, session_id=session_id)
        if instance_id:
            data["instance_id"] = instance_id
        # 生成二维码图片（data URI，前端可直接 <img src> 显示）
        qr_content = (
            data.get("qrcode_url")
            or data.get("auth_url")
            or data.get("verification_uri_complete")
            or ""
        )
        if qr_content:
            data["qrcode_image"] = qr_login._qr_png_data_uri(str(qr_content))
        return JSONResponse(public.return_data(True, data=data))
    except Exception as e:
        logger.warning("[card-gw] %s 生成二维码失败", kind, exc_info=True)
        return JSONResponse(public.returnMsg(False, f"生成二维码失败: {e}"), status_code=500)


@router.post("/card/channels/{kind}/qrcode/poll")
async def api_card_qrcode_poll(kind: str, request: Request):
    """轮询扫码结果。body: {session_id, instance_id?}。"""
    prefix, err = _require_card(request)
    if err:
        return err
    from platform_ext import qr_login
    if kind not in qr_login.supported_qr_kinds():
        return JSONResponse(public.returnMsg(False, f"平台 {kind} 不支持扫码登录"), status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not body.get("token"):
        return JSONResponse(public.returnMsg(False, "缺少 token"), status_code=400)
    token = str(body["token"])
    session_id = str(body.get("session_id") or f"card_{prefix}_default")
    instance_id = str(body.get("instance_id") or "").strip() or None
    try:
        data = await qr_login.poll(kind, token, session_id=session_id, instance_id=instance_id)
        if data.get("status") == "success":
            data["configured"] = True
            if instance_id:
                data["instance_id"] = instance_id
        return JSONResponse(public.return_data(True, data=data))
    except Exception as e:
        logger.warning("[card-gw] %s 轮询失败", kind, exc_info=True)
        return JSONResponse(public.returnMsg(False, f"轮询失败: {e}"), status_code=500)
