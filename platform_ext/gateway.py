"""多平台 IM 通道桥（对齐 Octop octop-gateway 官方包）。

封装 `octop_gateway.manager.ChannelManager`，为 BT 提供统一的多平台
IM 通道注册/启停/状态管理。

设计（不破坏现有 API）：
- 所有路由走 /api/v2/gateway/* 前缀；
- 配置存 config.json 的 platform_ext.gateway 段；
- 平台 SDK 按需安装（vendor/octop-gateway），未装 SDK 的平台 start 时返回清晰错误；
- BT 现有微信小程序/clawbot/869 通道逻辑原样保留，本模块只新增真实 IM 平台通道。

API：
  GET  /api/v2/gateway/channels  列出支持的平台通道与激活状态
  GET  /api/v2/gateway/status  全局状态（运行中通道数、队列长度）
  POST /api/v2/gateway/channels/{kind}/start   启动某平台通道
  POST /api/v2/gateway/channels/{kind}/stop    停止某平台通道
  POST /api/v2/gateway/channels/{kind}/config 保存平台配置（token/webhook 等）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import public
from platform_ext import PROJECT_ROOT

logger = logging.getLogger("platform-ext-gateway")

router = APIRouter()

# 支持的平台（对齐 octop_gateway.channels.SUPPORTED_CHANNEL_KINDS 的常用子集）
# 平台标签与配置说明（用于增强 SUPPORTED_CHANNEL_KINDS 的展示）
_PLATFORM_META: dict[str, dict[str, Any]] = {
    "weixin": {
        "label": "微信 (iLink)",
        "config_fields": [{"name": "token", "type": "string", "required": True}],
        "note": "复用 BT 869 微信能力，需配置 webhook/token",
    },
    "feishu": {
        "label": "飞书",
        "config_fields": [{"name": "app_id", "type": "string", "required": True},
                          {"name": "app_secret", "type": "string", "required": True}],
        "note": "需 lark-oapi SDK",
    },
    "telegram": {
        "label": "Telegram",
        "config_fields": [{"name": "bot_token", "type": "string", "required": True}],
        "note": "需 python-telegram-bot SDK",
    },
    "qq": {
        "label": "QQ",
        "config_fields": [{"name": "account", "type": "string", "required": True}],
        "note": "QQ 协议通道",
    },
    "dingtalk": {
        "label": "钉钉",
        "config_fields": [{"name": "app_key", "type": "string", "required": True},
                          {"name": "app_secret", "type": "string", "required": True}],
        "note": "需 dingtalk-stream SDK",
    },
    "wecom": {
        "label": "企业微信",
        "config_fields": [{"name": "corp_id", "type": "string", "required": True},
                          {"name": "secret", "type": "string", "required": True}],
        "note": "需 wecom-aibot-sdk",
    },
    "discord": {
        "label": "Discord",
        "config_fields": [{"name": "bot_token", "type": "string", "required": True}],
        "note": "需 discord-py SDK",
    },
    "yuanbao": {
        "label": "元宝",
        "config_fields": [{"name": "token", "type": "string", "required": True}],
        "note": "元宝开放平台",
    },
    "xiaoyi": {
        "label": "小一 (xiaoyi)",
        "config_fields": [{"name": "token", "type": "string", "required": True}],
        "note": "小一智能助手",
    },
    "mqtt": {
        "label": "MQTT",
        "config_fields": [{"name": "broker", "type": "string", "required": True},
                          {"name": "topic", "type": "string", "required": True}],
        "note": "MQTT 协议通道，需 paho-mqtt SDK",
    },
}

# 对齐 octop-gateway 官方 SUPPORTED_CHANNEL_KINDS（动态枚举，自动同步新平台）
# 这样 octop-gateway 升级新增平台时，本模块自动跟随。
def _build_platform_kinds() -> dict[str, dict[str, Any]]:
    kinds = {}
    try:
        from octop_gateway import SUPPORTED_CHANNEL_KINDS
        supported = sorted(SUPPORTED_CHANNEL_KINDS)
    except Exception:  # noqa: BLE001
        supported = list(_PLATFORM_META.keys())
    for kind in supported:
        meta = dict(_PLATFORM_META.get(kind, {}))
        meta.setdefault("label", kind.title())
        meta.setdefault("config_fields", [])
        meta.setdefault("note", "Octop 官方通道（SDK 按需安装）")
        kinds[kind] = meta
    return kinds


PLATFORM_KINDS: dict[str, dict[str, Any]] = _build_platform_kinds()


def _config_path() -> str:
    return os.path.join(PROJECT_ROOT, "config.json")


def _load_config() -> dict[str, Any]:
    try:
        with open(_config_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _save_config(cfg: dict[str, Any]) -> None:
    with open(_config_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def _gateway_cfg() -> dict[str, Any]:
    return _load_config().get("platform_ext", {}).get("gateway", {})


def _set_gateway_cfg(cfg: dict[str, Any]) -> None:
    full = _load_config()
    full.setdefault("platform_ext", {})["gateway"] = cfg
    _save_config(full)


def _sdk_available(kind: str) -> bool:
    if kind not in PLATFORM_KINDS:
        return False
    # 平台 SDK 模块路径：octop_gateway.channels.<kind>（对齐官方 _CHANNEL_MAP）
    try:
        __import__(f"octop_gateway.channels.{kind}", fromlist=["*"])
        return True
    except Exception:  # noqa: BLE001
        return False


# 运行中的 ChannelManager 单例（惰性）
_manager = None
_manager_kinds: set[str] = set()
_manager_running = False


def _get_manager():
    """懒加载 octop_gateway.ChannelManager。"""
    global _manager
    if _manager is not None:
        return _manager
    from octop_gateway import ChannelManager
    _manager = ChannelManager(workers_per_channel=2, queue_maxsize=200)
    return _manager


async def _start_channel(kind: str, cfg: dict[str, Any]) -> str:
    """启动一个平台通道，返回 channel_id。"""
    global _manager_running
    mgr = _get_manager()
    if not _manager_running:
        await mgr.start()
        _manager_running = True
    # 通过 add_channel 统一注册（便捷 add_* 方法依赖具体 SDK 类型）
    channel_id = await mgr.add_channel(channel_type=kind, config=cfg)
    _manager_kinds.add(kind)
    return channel_id


# ── HTTP 路由 ─────────────────────────────────────────────
def _ok(data: Any = None, msg: str = "") -> JSONResponse:
    return JSONResponse(public.return_data(True, data=data, msg=msg))


def _err(msg: str) -> JSONResponse:
    return JSONResponse(public.returnMsg(False, msg))


@router.get("/gateway/channels")
async def gateway_list_channels() -> JSONResponse:
    """列出支持的平台通道与激活状态。"""
    cfg = _gateway_cfg()
    out = []
    for kind, meta in PLATFORM_KINDS.items():
        saved = cfg.get(kind, {})
        sdk_ok = _sdk_available(kind)
        out.append({
            "kind": kind,
            "label": meta["label"],
            "note": meta["note"],
            "configured": bool(saved.get("configured")),
            "sdk_installed": sdk_ok,
            "running": kind in _manager_kinds,
            "config_fields": meta["config_fields"],
        })
    return _ok({"channels": out})


@router.get("/gateway/status")
async def gateway_status() -> JSONResponse:
    """全局状态。"""
    running = list(sorted(_manager_kinds))
    queue_len = 0
    if _manager is not None:
        try:
            queue_len = sum(q.qsize() for q in _manager._queues.values())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            queue_len = -1
    return _ok({
        "manager_running": _manager_running,
        "running_channels": running,
        "queue_len": queue_len,
        "sdk_missing": [k for k in PLATFORM_KINDS if not _sdk_available(k)],
    })


@router.post("/gateway/channels/{kind}/config")
async def gateway_save_config(kind: str, request: Request) -> JSONResponse:
    """保存某平台的配置（token/webhook 等）。"""
    if kind not in PLATFORM_KINDS:
        return _err(f"未知平台: {kind}")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        return _err("配置必须是 JSON 对象")
    cfg = _gateway_cfg()
    # 合并配置并标记已配置
    saved = dict(cfg.get(kind, {}))
    for k, v in body.items():
        if k == "configured":
            continue
        saved[k] = v
    saved["configured"] = True
    cfg[kind] = saved
    _set_gateway_cfg(cfg)
    return _ok({"kind": kind, "configured": True})


@router.post("/gateway/channels/{kind}/start")
async def gateway_start(kind: str) -> JSONResponse:
    """启动某平台通道。SDK 缺失时返回清晰错误。"""
    if kind not in PLATFORM_KINDS:
        return _err(f"未知平台: {kind}")
    cfg = _gateway_cfg().get(kind, {})
    if not cfg.get("configured"):
        return _err(f"平台 {kind} 未配置，请先 POST /api/v2/gateway/channels/{kind}/config")
    if not _sdk_available(kind):
        return _err(
            f"平台 {kind} 的 SDK 未安装。请在 BT 虚拟环境安装 vendor/octop-gateway 的对应依赖：\n"
            f"  .venv/bin/pip install --target vendor/octop-gateway <sdk>\n"
            f"（详见 docs/plan-octop-gateway-memory.md）"
        )
    try:
        # 去掉配置管理字段，只传平台所需字段
        channel_cfg = {k: v for k, v in cfg.items() if k != "configured"}
        cid = await _start_channel(kind, channel_cfg)
        return _ok({"kind": kind, "channel_id": cid, "running": True})
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 启动 %s 失败", kind)
        return _err(f"启动 {kind} 失败: {e}")


@router.post("/gateway/channels/{kind}/stop")
async def gateway_stop(kind: str) -> JSONResponse:
    """停止某平台通道。"""
    global _manager_running
    if _manager is None:
        return _ok({"kind": kind, "running": False})
    try:
        # 找到该 kind 对应的 channel 并移除
        cids = [cid for cid in _manager.channel_ids() if cid in _manager_kinds]
        for cid in cids:
            if kind in _manager_kinds:
                await _manager.remove_channel(cid)
                _manager_kinds.discard(kind)
        if not _manager_kinds and _manager_running:
            await _manager.stop()
            _manager_running = False
        return _ok({"kind": kind, "running": False})
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 停止 %s 失败", kind)
        return _err(f"停止 {kind} 失败: {e}")