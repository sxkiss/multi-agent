"""多平台 IM 通道桥。

封装底层 IM 通道管理器，为 BT 提供统一的多平台通道注册/启停/状态管理，
并把收到的消息桥接到 BT 现有 agent 对话体系。

设计（不破坏现有 API）：
- 所有路由走 /api/v2/gateway/* 前缀；
- 配置存 config.json 的 platform_ext.gateway 段；
- 平台 SDK 按需安装（vendor/octop-gateway），未装 SDK 的平台 start 时返回清晰错误；
- BT 现有微信小程序/clawbot/869 通道逻辑原样保留，本模块只新增真实 IM 平台通道；
- 通道消息经 gateway_processor 进入 BT 对话（session 形如 gw_<channel>_<subject>）。

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

# 支持的平台（对齐底层通道 SDK 的 SUPPORTED_CHANNEL_KINDS 常用子集）
# config_fields 的 name 严格对齐各平台 ChannelConfig 的真实凭据字段，
# 确保手动配置、扫码绑定、SDK 启动三者字段一致。
_PLATFORM_META: dict[str, dict[str, Any]] = {
    "weixin": {
        "label": "微信 (iLink)",
        "config_fields": [{"name": "token", "type": "password", "required": True, "placeholder": "iLink Bot token"},
                          {"name": "account_id", "type": "string", "required": False, "placeholder": "账号 ID / bot_uin"},
                          {"name": "base_url", "type": "string", "required": False, "placeholder": "默认 https://ilinkai.weixin.qq.com"}],
        "note": "微信 iLink Bot",
    },
    "feishu": {
        "label": "飞书",
        "config_fields": [{"name": "app_id", "type": "string", "required": True},
                          {"name": "app_secret", "type": "password", "required": True}],
        "note": "需 lark-oapi SDK",
    },
    "telegram": {
        "label": "Telegram",
        "config_fields": [{"name": "bot_token", "type": "password", "required": True}],
        "note": "需 python-telegram-bot SDK",
    },
    "qq": {
        "label": "QQ",
        "config_fields": [{"name": "app_id", "type": "string", "required": True, "placeholder": "QQ 开放平台 AppID"},
                          {"name": "secret", "type": "password", "required": True, "placeholder": "AppSecret（用于签名校验）"},
                          {"name": "token", "type": "password", "required": False, "placeholder": "Bot token（可选）"}],
        "note": "QQ 机器人通道",
    },
    "dingtalk": {
        "label": "钉钉",
        "config_fields": [{"name": "app_key", "type": "string", "required": True},
                          {"name": "app_secret", "type": "password", "required": True}],
        "note": "需 dingtalk-stream SDK",
    },
    "wecom": {
        "label": "企业微信",
        "config_fields": [{"name": "bot_id", "type": "string", "required": True, "placeholder": "企业微信 AI Bot ID"},
                          {"name": "secret", "type": "password", "required": True}],
        "note": "需企业微信 AI Bot SDK",
    },
    "discord": {
        "label": "Discord",
        "config_fields": [{"name": "bot_token", "type": "password", "required": True}],
        "note": "需 discord-py SDK",
    },
    "yuanbao": {
        "label": "元宝",
        "config_fields": [{"name": "token", "type": "password", "required": True}],
        "note": "元宝开放平台",
    },
    "xiaoyi": {
        "label": "小一 (xiaoyi)",
        "config_fields": [{"name": "token", "type": "password", "required": True}],
        "note": "小一智能助手",
    },
    "mqtt": {
        "label": "MQTT",
        "config_fields": [{"name": "host", "type": "string", "required": True, "placeholder": "broker 主机名"},
                          {"name": "port", "type": "number", "required": False, "placeholder": "端口，默认 8883"},
                          {"name": "subscribe_topic", "type": "string", "required": True, "placeholder": "订阅主题（入站）"},
                          {"name": "publish_topic", "type": "string", "required": False, "placeholder": "发布主题模板（出站）"},
                          {"name": "username", "type": "string", "required": False},
                          {"name": "password", "type": "password", "required": False}],
        "note": "MQTT 协议通道，需 paho-mqtt SDK",
    },
}

# 对齐底层通道 SDK 的 SUPPORTED_CHANNEL_KINDS（动态枚举，自动同步新平台）
# 这样底层通道库升级新增平台时，本模块自动跟随。
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
        meta.setdefault("note", "SDK 通道，按需安装依赖")
        # 每通道追加可选"提示词模板"字段（连续对话时传给 chat_start）
        if not any(f.get("name") == "template" for f in meta["config_fields"]):
            meta["config_fields"] = list(meta["config_fields"]) + [{
                "name": "template",
                "type": "string",
                "required": False,
                "placeholder": "提示词模板名（default/gpt5.5/hermes/unrestricted_jeli），留空用全局默认",
            }]
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
    # 平台 SDK 模块路径：octop_gateway.channels.<kind>（对齐底层 _CHANNEL_MAP）
    try:
        __import__(f"octop_gateway.channels.{kind}", fromlist=["*"])
        return True
    except Exception:  # noqa: BLE001
        return False


def _norm_cfg(kind: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """把前端扁平配置（config_fields 字段名）归一化为底层 SDK 期望的结构。

    多数平台字段名已对齐（app_id/app_secret/bot_token/bot_id 等），直接透传；
    少数平台需要特殊转换：
      - weixin：扁平 {token, account_id, base_url} → accounts[] 列表
      - mqtt：port 字符串 → int
    template 是配置元数据（仅用于对话提示词），不传给 SDK，统一剔除。
    """
    cfg = {k: v for k, v in cfg.items() if k != "template"}
    if kind == "weixin":
        token = str(cfg.get("token") or "").strip()
        account_id = str(cfg.get("account_id") or "").strip()
        base_url = str(cfg.get("base_url") or "").strip() or "https://ilinkai.weixin.qq.com"
        if token and account_id:
            return {
                "accounts": [{
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                }]
            }
        return {"accounts": []}
    if kind == "mqtt":
        out = dict(cfg)
        if out.get("port"):
            try:
                out["port"] = int(out["port"])
            except (TypeError, ValueError):
                out["port"] = 8883
        return out
    # 其余平台字段已对齐，直接透传
    return dict(cfg)


# 运行中的 ChannelManager 单例（惰性）
_manager = None
_manager_kinds: set[str] = set()
_manager_kind_of: dict[str, str] = {}  # channel_id -> kind
_manager_running = False


def _get_manager():
    """懒加载底层 ChannelManager，并绑定对话桥接处理器。"""
    global _manager
    if _manager is not None:
        return _manager
    from octop_gateway import ChannelManager
    from platform_ext.gateway_processor import make_processor

    _manager = ChannelManager(
        workers_per_channel=2,
        queue_maxsize=200,
        processor=make_processor(),
    )
    return _manager


async def _start_channel(kind: str, cfg: dict[str, Any]) -> str:
    """启动一个平台通道，返回 channel_id。

    先把前端扁平配置归一化为底层 SDK 期望结构，再注册通道；
    底层 manager 自带对话桥接处理器，消息进入 BT 对话体系。
    """
    global _manager_running
    mgr = _get_manager()
    if not _manager_running:
        await mgr.start()
        _manager_running = True
    norm_cfg = _norm_cfg(kind, cfg)
    channel_id = await mgr.add_channel(channel_type=kind, config=norm_cfg)
    _manager_kinds.add(kind)
    _manager_kind_of[channel_id] = kind
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


@router.get("/gateway/templates")
async def gateway_templates() -> JSONResponse:
    """列出可用的提示词模板名，供每通道模板配置选择。"""
    templates = []
    try:
        from chat_client.opencode_templates import OPENCODE_TEMPLATES
        templates = sorted(OPENCODE_TEMPLATES.keys())
    except Exception:  # noqa: BLE001
        templates = []
    # 合并自定义模板
    try:
        from platform_ext import PROJECT_ROOT
        import json as _json
        with open(os.path.join(PROJECT_ROOT, "config.json"), "r", encoding="utf-8") as fh:
            cfg = _json.load(fh)
        for key in ("opencode", "claude", "single"):
            custom = ((cfg.get(key, {}) or {}).get("templates", {}) or {})
            for name in custom.keys():
                if name not in templates:
                    templates.append(name)
    except Exception:  # noqa: BLE001
        pass
    return _ok({"templates": templates})


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
    global _manager_running, _manager_kinds, _manager_kind_of
    if _manager is None:
        return _ok({"kind": kind, "running": False})
    try:
        # 精确找到该 kind 对应的 channel（channel_ids 是属性，非方法）
        target = [cid for cid in _manager.channel_ids if _manager_kind_of.get(cid) == kind]
        for cid in target:
            await _manager.remove_channel(cid)
            _manager_kind_of.pop(cid, None)
        if kind in _manager_kinds:
            _manager_kinds.discard(kind)
        # 若已无运行通道，停止 manager 释放资源
        if not _manager_kinds and _manager_running:
            await _manager.stop()
            _manager_running = False
        return _ok({"kind": kind, "running": False})
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 停止 %s 失败", kind)
        return _err(f"停止 {kind} 失败: {e}")
from platform_ext import qr_login  # noqa: E402


@router.post("/gateway/channels/{kind}/qrcode/generate")
async def gateway_qrcode_generate(kind: str, request: Request) -> JSONResponse:
    """生成二维码。body 可带 session_id（可选）。"""
    if kind not in qr_login.supported_qr_kinds():
        return _err(f"平台 {kind} 不支持扫码登录。支持的扫码平台: {', '.join(qr_login.supported_qr_kinds())}")
    try:
        body = await request.json() if request is not None else {}
    except Exception:
        body = {}
    session_id = str((body or {}).get("session_id") or "default")
    try:
        data = await qr_login.generate(kind, session_id=session_id)
        return _ok(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] %s 生成二维码失败", kind)
        return _err(f"生成二维码失败: {e}")


@router.post("/gateway/channels/{kind}/qrcode/poll")
async def gateway_qrcode_poll(kind: str, request: Request) -> JSONResponse:
    """轮询扫码结果。body: {token, session_id?}。成功后自动保存配置。"""
    if kind not in qr_login.supported_qr_kinds():
        return _err(f"平台 {kind} 不支持扫码登录")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or not body.get("token"):
        return _err("缺少 token")
    token = str(body["token"])
    session_id = str(body.get("session_id") or "default")
    try:
        data = await qr_login.poll(kind, token, session_id=session_id)
        if data.get("status") == "success":
            data["configured"] = True
        return _ok(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] %s 轮询失败", kind)
        return _err(f"轮询失败: {e}")
