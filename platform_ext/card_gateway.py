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


@router.post("/card/channels/{channel_id}/stop")
async def api_card_channel_stop(channel_id: str, request: Request):
    prefix, err = _require_card(request)
    if err:
        return err
    if not await stop_channel(prefix, channel_id):
        return JSONResponse(public.returnMsg(False, "通道不存在"), status_code=404)
    return JSONResponse(public.return_data(True, data={"channel_id": channel_id, "running": False}))
