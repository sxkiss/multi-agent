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

import logging
import os
import uuid
from typing import Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

import public
from platform_ext import PROJECT_ROOT, config_files, qr_login

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
        # 每通道追加可选"对话模式"字段（默认单 Agent）
        if not any(f.get("name") == "mode" for f in meta["config_fields"]):
            meta["config_fields"] = list(meta["config_fields"]) + [{
                "name": "mode",
                "type": "select",
                "required": False,
                "placeholder": "单 Agent（默认）",
                "options": [
                    {"value": "", "label": "单 Agent（默认）"},
                    {"value": "group", "label": "集团模式"},
                    {"value": "claude", "label": "Claude CLI"},
                    {"value": "opencode", "label": "opencode"},
                ],
            }]
        # 每通道追加可选"默认工作目录"字段（发起对话时传给 chat_start）
        if not any(f.get("name") == "workspace" for f in meta["config_fields"]):
            meta["config_fields"] = list(meta["config_fields"]) + [{
                "name": "workspace",
                "type": "string",
                "required": False,
                "placeholder": "Agent 默认工作目录（绝对路径），留空用全局默认",
            }]
        kinds[kind] = meta
    return kinds


PLATFORM_KINDS: dict[str, dict[str, Any]] = _build_platform_kinds()


def _config_path() -> str:
    return os.path.join(PROJECT_ROOT, "config.json")


def _gateway_cfg() -> dict[str, Any]:
    return config_files.load_section("gateway", _config_path())


def _set_gateway_cfg(cfg: dict[str, Any]) -> None:
    config_files.save_section("gateway", cfg, _config_path())


# ── 同通道多实例管理 ─────────────────────────────────────────
# 每个平台可配置多个实例（如两个飞书机器人、两个 Telegram 机器人）。
# 实例统一存于 config 的 platform_ext.gateway.instances 列表：
#   {
#     "instance_id": "feishu_main",
#     "kind": "feishu",
#     "name": "飞书主机器人",
#     "configured": true,
#     "enabled": true,          # 是否随服务重启自动恢复
#     "template": "",           # 提示词模板
#     "mode": "",               # 对话模式 single/group/claude
#     "workspace": "",          # 默认工作目录
#     ...其余为平台配置字段（app_id/app_secret/token 等）
#   }
# 旧的按 kind 存储的配置（config[<kind>]）会被自动迁移为 legacy 实例，
# 其 instance_id = <kind>，从而保持 /gateway/channels/{kind}/* 老接口兼容。


def _instances() -> list[dict[str, Any]]:
    g = _gateway_cfg()
    if not isinstance(g.get("instances"), list):
        # 首次读取：从老式 kind 配置构建实例列表，再重新读取迁移后的结果
        _migrate_legacy_kind_configs()
        g = _gateway_cfg()
    return g.get("instances") or []


def _migrate_legacy_kind_configs() -> None:
    """把老式 config[<kind>] 扁平配置迁移为 instances 列表条目。

    老式配置形如 platform_ext.gateway.<kind> = {configured, app_id, ...}，
    迁移后 instance_id = <kind>（保留老接口按 kind 定位的能力）。
    若 instances 已存在则不再迁移（避免删除的 legacy 实例被反复重建）。
    """
    g = _gateway_cfg()
    if "instances" in g and isinstance(g["instances"], list):
        return
    instances: list[dict[str, Any]] = []
    for kind in PLATFORM_KINDS:
        saved = g.get(kind)
        if not isinstance(saved, dict) or not saved.get("configured"):
            continue
        inst = dict(saved)
        inst["kind"] = kind
        inst["instance_id"] = kind  # legacy 实例 id 复用 kind
        inst.setdefault("name", _PLATFORM_META.get(kind, {}).get("label", kind))
        inst.setdefault("enabled", True)
        inst.pop("configured", None)
        inst["configured"] = True
        instances.append(inst)
    g["instances"] = instances
    _set_gateway_cfg(g)


def _find_instance(instance_id: str) -> dict[str, Any] | None:
    for inst in _instances():
        if inst.get("instance_id") == instance_id:
            return inst
    return None


def _instance_meta(instance_id: str) -> dict[str, Any] | None:
    return _find_instance(instance_id)


def _instance_kind(instance_id: str) -> str:
    inst = _find_instance(instance_id)
    return str(inst.get("kind")) if inst else ""


def _save_instance(instance_id: str, inst: dict[str, Any]) -> None:
    g = _gateway_cfg()
    instances = g.get("instances") or []
    replaced = False
    for i, it in enumerate(instances):
        if it.get("instance_id") == instance_id:
            instances[i] = inst
            replaced = True
            break
    if not replaced:
        instances.append(inst)
    g["instances"] = instances
    _set_gateway_cfg(g)


def _remove_instance(instance_id: str) -> bool:
    g = _gateway_cfg()
    instances = g.get("instances") or []
    new = [it for it in instances if it.get("instance_id") != instance_id]
    if len(new) == len(instances):
        return False
    g["instances"] = new
    _set_gateway_cfg(g)
    return True


def _compute_configured(kind: str, cfg: dict[str, Any]) -> bool:
    """按平台 required 字段判断实例是否已完成配置。"""
    required = [
        f["name"]
        for f in (PLATFORM_KINDS.get(kind, {}) or {}).get("config_fields", [])
        if f.get("required")
    ]
    if not required:
        return bool(cfg)
    return all(str(cfg.get(name) or "").strip() for name in required)


def _config_fields_for(kind: str, saved: dict[str, Any] | None) -> list[dict[str, Any]]:
    """构造带 saved/saved_value 的字段定义（敏感字段不回显明文）。"""
    saved = saved or {}
    fields: list[dict[str, Any]] = []
    for f in (PLATFORM_KINDS.get(kind, {}) or {}).get("config_fields", []):
        name = f.get("name")
        item = dict(f)
        if name in saved:
            is_sensitive = name in _SENSITIVE_FIELDS or f.get("type") == "password"
            if is_sensitive:
                item["saved"] = bool(saved.get(name))
                item["saved_value"] = ""
            else:
                item["saved"] = True
                item["saved_value"] = str(saved.get(name) or "")
        else:
            item["saved"] = False
            item["saved_value"] = ""
        fields.append(item)
    return fields


def _instance_public(inst: dict[str, Any]) -> dict[str, Any]:
    """实例对外视图：附带运行状态、字段定义（敏感字段脱敏）。"""
    kind = str(inst.get("kind") or "")
    meta = PLATFORM_KINDS.get(kind, {})
    instance_id = str(inst.get("instance_id") or "")
    return {
        "instance_id": instance_id,
        "kind": kind,
        "name": inst.get("name") or meta.get("label", kind),
        "label": meta.get("label", kind),
        "note": meta.get("note", ""),
        "configured": bool(inst.get("configured")),
        "enabled": bool(inst.get("enabled", True)),
        "running": instance_id in _running_instance_ids(),
        "sdk_installed": _sdk_available(kind),
        "supported_qr": kind in qr_login.supported_qr_kinds(),
        "config_fields": _config_fields_for(kind, inst),
    }


def _new_instance_id(kind: str) -> str:
    while True:
        candidate = f"{kind}_{uuid.uuid4().hex[:8]}"
        if _find_instance(candidate) is None:
            return candidate


# ── 自动启动状态持久化（重启后恢复手动启动过的通道/实例）────────
# 手动启动实例后，把 instance_id 记入配置的 _auto_start 列表；服务重启时
# 自动重新启动这些实例。手动停止则从列表移除，之后不会自动恢复。
def _known_instance_ids(g: dict[str, Any]) -> set[str]:
    """从已读取的 gateway 段收集合法实例 id（不触发迁移写入）。"""
    known = set(PLATFORM_KINDS)  # legacy kind 本身即实例 id
    insts = g.get("instances")
    if isinstance(insts, list):
        known |= {str(i.get("instance_id")) for i in insts if i.get("instance_id")}
    return known


def _auto_start_list() -> list[str]:
    g = _gateway_cfg()
    raw = g.get("_auto_start", [])
    if not isinstance(raw, list):
        return []
    known = _known_instance_ids(g)
    return [str(k) for k in raw if str(k) in known]


def _set_auto_start_list(ids: list[str]) -> None:
    g = _gateway_cfg()
    known = _known_instance_ids(g)
    g["_auto_start"] = [k for k in ids if str(k) in known]
    _set_gateway_cfg(g)


def mark_auto_start(instance_id: str) -> None:
    cur = _auto_start_list()
    if instance_id not in cur:
        cur.append(instance_id)
        _set_auto_start_list(cur)


def unmark_auto_start(instance_id: str) -> None:
    cur = _auto_start_list()
    if instance_id in cur:
        cur.remove(instance_id)
        _set_auto_start_list(cur)


async def restore_auto_start_channels() -> None:
    """服务重启后自动启动用户手动启动过、且未手动停止的通道实例。"""
    ids = _auto_start_list()
    if not ids:
        return
    logger.info("[gateway] 检测到 %d 个需自动恢复的通道实例: %s", len(ids), ids)
    for instance_id in ids:
        inst = _find_instance(instance_id)
        if inst is None:
            # legacy 兼容：kind 直接作为实例 id，用老式扁平配置启动
            if instance_id not in PLATFORM_KINDS:
                continue
            saved = _gateway_cfg().get(instance_id, {})
            if not isinstance(saved, dict) or not saved.get("configured"):
                continue
            inst = dict(saved)
            inst["kind"] = instance_id
            inst["instance_id"] = instance_id
            inst.setdefault("enabled", True)
        kind = inst.get("kind", "")
        if not inst.get("configured"):
            logger.warning("[gateway] 实例 %s 已标记自动启动但未完成配置，跳过", instance_id)
            continue
        if not _sdk_available(kind):
            logger.warning("[gateway] 实例 %s（%s）SDK 不可用，跳过自动恢复", instance_id, kind)
            continue
        ok, dep_msg = _ensure_deps(kind)
        if not ok:
            logger.warning("[gateway] 实例 %s 依赖未就绪，跳过自动恢复: %s", instance_id, dep_msg)
            continue
        try:
            channel_cfg = _instance_to_channel_cfg(inst)
            # legacy 实例（instance_id == kind，老式扁平配置）走旧通道接口，
            # 保持 /gateway/channels/{kind}/* 兼容；独立实例走实例级接口。
            if instance_id in PLATFORM_KINDS and _gateway_cfg().get(instance_id, {}).get("configured"):
                cid = await _start_channel(kind, channel_cfg)
            else:
                cid = await _start_instance(instance_id, kind, channel_cfg)
            logger.info("[gateway] 实例 %s 已自动恢复 (channel_id=%s)", instance_id, cid)
        except Exception as e:
            logger.warning("[gateway] 自动恢复实例 %s 失败: %s", instance_id, e)


def _instance_to_channel_cfg(inst: dict[str, Any]) -> dict[str, Any]:
    """从实例对象提取传给底层 SDK 的配置（去掉管理字段）。"""
    skip = {"instance_id", "kind", "name", "configured", "enabled",
            "template", "mode", "workspace"}
    return {k: v for k, v in inst.items() if k not in skip}


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
    template 是配置元数据（仅用于对话提示词），mode 是对话模式（single/group/claude），
    workspace 是对话默认工作目录，均不传给 SDK，统一剔除。
    """
    cfg = {k: v for k, v in cfg.items() if k not in ("template", "mode", "workspace")}
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


# 各通道运行时需要的第三方依赖（import 模块名 -> pip 包名）
# 这些库是延迟导入的：通道模块本身能 import（_sdk_available=True），
# 但真正启动连接时才需要它们。启动前自检，缺失则自动 pip 安装。
_RUNTIME_DEPS: dict[str, dict[str, str]] = {
    "dingtalk": {"dingtalk_stream": "dingtalk-stream"},
    "discord": {"discord": "discord.py"},
    "feishu": {"lark_oapi": "lark-oapi"},
    "mqtt": {"paho.mqtt": "paho-mqtt"},
    "wecom": {"wecom_aibot_sdk": "wecom-aibot-sdk"},
    "telegram": {"telegram": "python-telegram-bot"},
}


def _missing_deps(kind: str) -> list[str]:
    """返回该通道缺失的 pip 包名列表（按 _RUNTIME_DEPS 检测）。"""
    missing: list[str] = []
    for mod, pkg in (_RUNTIME_DEPS.get(kind) or {}).items():
        try:
            import importlib.util

            if importlib.util.find_spec(mod) is None:
                missing.append(pkg)
        except (ImportError, ValueError, AttributeError):
            missing.append(pkg)
    return missing


def _install_deps(kind: str, pkgs: list[str]) -> tuple[bool, str]:
    """自动安装指定通道缺失的第三方依赖（装到项目 vendor 目录）。

    返回 (成功与否, 错误信息或成功提示)。
    """
    import subprocess

    target = os.path.join(PROJECT_ROOT, "vendor")
    pip = os.path.join(PROJECT_ROOT, ".venv", "bin", "pip")
    if not os.path.exists(pip):
        pip = "pip"  # 回退到系统 pip
    cmd = [pip, "install", "--quiet", "--disable-pip-version-check", "--target", target] + pkgs
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout or "").strip()[-800:]
        return True, "已安装: " + ", ".join(pkgs)
    except subprocess.TimeoutExpired:
        return False, f"安装依赖超时: {', '.join(pkgs)}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _ensure_deps(kind: str) -> tuple[bool, str]:
    """确保通道运行依赖就绪：缺失则自动安装。返回 (是否就绪, 提示/错误)。"""
    missing = _missing_deps(kind)
    if not missing:
        return True, ""
    logger.info("[gateway] %s 缺失依赖 %s，尝试自动安装", kind, ", ".join(missing))
    ok, msg = _install_deps(kind, missing)
    if not ok:
        return False, f"自动安装 {kind} 依赖失败: {msg}"
    # 安装后再确认一次
    still = _missing_deps(kind)
    if still:
        return False, f"安装后仍缺失: {', '.join(still)}"
    return True, msg


# 运行中的 ChannelManager 单例（惰性）
_manager = None
_manager_kinds: set[str] = set()            # 兼容：kind 维度（老接口用）
_manager_kind_of: dict[str, str] = {}       # channel_id -> kind（兼容）
_manager_instances: dict[str, str] = {}     # channel_id -> instance_id
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


def _running_instance_ids() -> set[str]:
    """当前所有运行中的实例 id。"""
    return set(_manager_instances.values())


async def _start_instance(instance_id: str, kind: str, cfg: dict[str, Any]) -> str:
    """启动一个实例，返回 channel_id。实例级防重：同一 instance_id 不会重复注册。"""
    if instance_id in _running_instance_ids():
        raise RuntimeError(f"实例 {instance_id} 已在运行中")
    global _manager_running
    mgr = _get_manager()
    if not _manager_running:
        await mgr.start()
        _manager_running = True
    norm_cfg = _norm_cfg(kind, cfg)
    channel_id = await mgr.add_channel(channel_type=kind, config=norm_cfg)
    _manager_instances[channel_id] = instance_id
    _manager_kind_of[channel_id] = kind
    _manager_kinds.add(kind)
    return channel_id


async def _start_channel(kind: str, cfg: dict[str, Any]) -> str:
    """（兼容）按 kind 启动通道，等价于启动 legacy 实例（instance_id == kind）。"""
    return await _start_instance(kind, kind, cfg)


async def _stop_instance(instance_id: str) -> None:
    """停止指定实例对应的所有 channel。"""
    global _manager_running, _manager_kinds
    if _manager is None:
        return
    target = [cid for cid in _manager.channel_ids if _manager_instances.get(cid) == instance_id]
    for cid in target:
        await _manager.remove_channel(cid)
        _manager_instances.pop(cid, None)
        kind = _manager_kind_of.pop(cid, None)
        if kind and not any(v == kind for v in _manager_instances.values()):
            _manager_kinds.discard(kind)
    if not _manager_instances and _manager_running:
        await _manager.stop()
        _manager_running = False


def _instance_create_body(body: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化创建/更新实例的请求体。"""
    kind = str((body.get("kind") or "").strip())
    if kind not in PLATFORM_KINDS:
        raise ValueError(f"未知平台: {kind}")
    name = str((body.get("name") or "").strip())
    if not name:
        name = PLATFORM_KINDS[kind].get("label", kind)
    inst = {
        "instance_id": str((body.get("instance_id") or "").strip()) or _new_instance_id(kind),
        "kind": kind,
        "name": name,
        "configured": bool(body.get("configured", False)),
        "enabled": bool(body.get("enabled", True)),
        "template": str(body.get("template") or "").strip(),
        "mode": str(body.get("mode") or "").strip(),
        "workspace": str(body.get("workspace") or "").strip(),
    }
    for f in PLATFORM_KINDS[kind].get("config_fields", []):
        fname = f.get("name")
        if fname in body:
            inst[fname] = body[fname]
    # 按平台 required 字段判断是否已配置
    inst["configured"] = _compute_configured(kind, inst)
    return inst


# ── HTTP 路由 ─────────────────────────────────────────────
def _ok(data: Any = None, msg: str = "") -> JSONResponse:
    return JSONResponse(public.return_data(True, data=data, msg=msg))


def _err(msg: str) -> JSONResponse:
    return JSONResponse(public.returnMsg(False, msg))


# 敏感字段（密码/密钥类），回显时不返回明文，只标记是否已设置
_SENSITIVE_FIELDS = {
    "token", "secret", "password", "app_secret", "bot_token",
    "client_secret", "encrypt_key",
}


@router.get("/gateway/channels")
async def gateway_list_channels() -> JSONResponse:
    """列出支持的平台通道与激活状态。

    对已配置的通道，config_fields 附带 saved_value（非敏感字段回填真实值，
    敏感字段仅标记 saved=True，不回显明文），供面板回显与编辑。
    """
    cfg = _gateway_cfg()
    out = []
    for kind, meta in PLATFORM_KINDS.items():
        saved = cfg.get(kind, {})
        sdk_ok = _sdk_available(kind)
        configured = bool(saved.get("configured"))
        fields = []
        for f in meta.get("config_fields", []):
            name = f.get("name")
            item = dict(f)
            if configured and name in saved:
                is_sensitive = name in _SENSITIVE_FIELDS or f.get("type") == "password"
                if is_sensitive:
                    item["saved"] = bool(saved.get(name))
                    item["saved_value"] = ""
                else:
                    item["saved"] = True
                    item["saved_value"] = str(saved.get(name) or "")
            else:
                item["saved"] = False
                item["saved_value"] = ""
            fields.append(item)
        out.append({
            "kind": kind,
            "label": meta["label"],
            "note": meta["note"],
            "configured": configured,
            "sdk_installed": sdk_ok,
            "running": kind in _manager_kinds,
            "config_fields": fields,
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
    """保存某平台配置（老接口）：委托到 instance_id == kind 的 legacy 实例。"""
    if kind not in PLATFORM_KINDS:
        return _err(f"未知平台: {kind}")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        return _err("配置必须是 JSON 对象")
    inst = _find_instance(kind)
    if inst is None:
        inst = {
            "instance_id": kind,
            "kind": kind,
            "name": PLATFORM_KINDS[kind].get("label", kind),
            "enabled": True,
            "configured": False,
        }
    for k, v in body.items():
        if k == "configured":
            continue
        fdef = next((f for f in PLATFORM_KINDS[kind].get("config_fields", []) if f.get("name") == k), None)
        is_sensitive = k in _SENSITIVE_FIELDS or (fdef or {}).get("type") == "password"
        if is_sensitive and (v is None or str(v).strip() == ""):
            continue  # 敏感字段留空表示不修改
        inst[k] = v
    inst["configured"] = True  # 老接口语义：保存即视为已配置
    _save_instance(kind, inst)
    # 同步老式扁平键，保证 /gateway/channels 仍能读到（兼容一个版本周期）
    g = _gateway_cfg()
    g[kind] = {k: v for k, v in inst.items()
               if k not in ("instance_id", "kind", "name", "enabled")}
    g[kind]["configured"] = True
    _set_gateway_cfg(g)
    return _ok({"kind": kind, "configured": True})


@router.post("/gateway/channels/{kind}/start")
async def gateway_start(kind: str) -> JSONResponse:
    """启动某平台通道（老接口）：委托到 instance_id == kind 的 legacy 实例。"""
    if kind not in PLATFORM_KINDS:
        return _err(f"未知平台: {kind}")
    if kind in _manager_kinds:
        return _ok({"kind": kind, "running": True, "msg": "通道已在运行中"})
    inst = _find_instance(kind)
    if inst is None or not inst.get("configured"):
        return _err(f"平台 {kind} 未配置，请先 POST /api/v2/gateway/channels/{kind}/config")
    if not _sdk_available(kind):
        return _err(
            f"平台 {kind} 的通道 SDK 不可用（底层库缺失）。请检查 vendor/octop-gateway 是否完整。"
        )
    ok, dep_msg = _ensure_deps(kind)
    if not ok:
        return _err(dep_msg)
    try:
        cid = await _start_instance(kind, kind, _instance_to_channel_cfg(inst))
        mark_auto_start(kind)
        return _ok({"kind": kind, "channel_id": cid, "running": True, "deps": dep_msg})
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 启动 %s 失败", kind)
        return _err(f"启动 {kind} 失败: {e}")


@router.post("/gateway/channels/{kind}/stop")
async def gateway_stop(kind: str) -> JSONResponse:
    """停止某平台通道（老接口）：委托到 instance_id == kind 的 legacy 实例。"""
    if _find_instance(kind) is None and kind not in PLATFORM_KINDS:
        return _err(f"未知平台: {kind}")
    try:
        await _stop_instance(kind)
        unmark_auto_start(kind)
        return _ok({"kind": kind, "running": False})
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 停止 %s 失败", kind)
        return _err(f"停止 {kind} 失败: {e}")


@router.post("/gateway/channels/{kind}/qrcode/generate")
async def gateway_qrcode_generate(kind: str, request: Request) -> JSONResponse:
    """生成二维码。body 可带 session_id / instance_id（可选）。"""
    if kind not in qr_login.supported_qr_kinds():
        return _err(f"平台 {kind} 不支持扫码登录。支持的扫码平台: {', '.join(qr_login.supported_qr_kinds())}")
    try:
        body = await request.json() if request is not None else {}
    except Exception:
        body = {}
    session_id = str((body or {}).get("session_id") or "default")
    instance_id = str((body or {}).get("instance_id") or "").strip() or None
    try:
        data = await qr_login.generate(kind, session_id=session_id)
        if instance_id:
            data["instance_id"] = instance_id
        return _ok(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] %s 生成二维码失败", kind)
        return _err(f"生成二维码失败: {e}")


@router.post("/gateway/channels/{kind}/qrcode/poll")
async def gateway_qrcode_poll(kind: str, request: Request) -> JSONResponse:
    """轮询扫码结果。body: {token, session_id?, instance_id?}。

    若带 instance_id，扫码成功后把凭据写入该指定实例，并回传 instance_id。
    """
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
    instance_id = str(body.get("instance_id") or "").strip() or None
    try:
        data = await qr_login.poll(kind, token, session_id=session_id, instance_id=instance_id)
        if data.get("status") == "success":
            data["configured"] = True
            if instance_id:
                data["instance_id"] = instance_id
        return _ok(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] %s 轮询失败", kind)
        return _err(f"轮询失败: {e}")


# ── 同通道多实例 CRUD + 启停 ───────────────────────────────
@router.get("/gateway/instances")
async def gateway_list_instances() -> JSONResponse:
    """列出全部实例（含 running 状态、kind、name）。"""
    try:
        items = [_instance_public(inst) for inst in _instances()]
        items.sort(key=lambda x: (x.get("kind") or "", x.get("name") or ""))
        return _ok({"instances": items})
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 列出实例失败")
        return _err(f"列出实例失败: {e}")


@router.post("/gateway/instances")
async def gateway_create_instance(request: Request) -> JSONResponse:
    """创建实例。body: {kind, name, enabled, ...config_fields}。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        return _err("请求体必须是 JSON 对象")
    try:
        inst = _instance_create_body(body)
    except ValueError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 创建实例失败")
        return _err(f"创建实例失败: {e}")
    if _find_instance(inst["instance_id"]) is not None:
        return _err(f"实例已存在: {inst['instance_id']}")
    _save_instance(inst["instance_id"], inst)
    return _ok({"instance": _instance_public(inst)}, "实例已创建")


@router.patch("/gateway/instances/{instance_id}")
async def gateway_update_instance(instance_id: str, request: Request) -> JSONResponse:
    """更新实例配置。body 可含 name/kind/enabled 及各平台字段。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        return _err("请求体必须是 JSON 对象")
    inst = _find_instance(instance_id)
    if inst is None:
        return _err(f"实例不存在: {instance_id}")
    # 禁止通过 PATCH 改 instance_id / kind（kind 变更视为删除重建）
    body.pop("instance_id", None)
    body.pop("kind", None)
    # 敏感字段留空表示“不修改”，避免面板回显空值把旧凭据清空
    sensitive = {
        f.get("name")
        for f in (PLATFORM_KINDS.get(inst["kind"], {}) or {}).get("config_fields", [])
        if f.get("name") in _SENSITIVE_FIELDS or f.get("type") == "password"
    }
    for fname in list(body):
        if fname in sensitive and not str(body.get(fname) or "").strip():
            body.pop(fname, None)
    try:
        updated = _instance_create_body({**inst, **body, "kind": inst["kind"]})
    except ValueError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 更新实例失败")
        return _err(f"更新实例失败: {e}")
    updated["instance_id"] = instance_id
    _save_instance(instance_id, updated)
    return _ok({"instance": _instance_public(updated)}, "实例已更新")


@router.delete("/gateway/instances/{instance_id}")
async def gateway_delete_instance(instance_id: str) -> JSONResponse:
    """删除实例（运行中的先停止）。"""
    inst = _find_instance(instance_id)
    if inst is None:
        return _err(f"实例不存在: {instance_id}")
    try:
        await _stop_instance(instance_id)
        unmark_auto_start(instance_id)
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 删除实例前停止失败: %s", instance_id)
        return _err(f"停止实例失败: {e}")
    _remove_instance(instance_id)
    return _ok({"instance_id": instance_id}, "实例已删除")


@router.post("/gateway/instances/{instance_id}/start")
async def gateway_start_instance(instance_id: str) -> JSONResponse:
    """启动实例（调 _start_instance，实例级防重）。"""
    inst = _find_instance(instance_id)
    if inst is None:
        return _err(f"实例不存在: {instance_id}")
    kind = str(inst.get("kind") or "")
    if not inst.get("configured"):
        return _err(f"实例 {instance_id} 未配置，请先编辑并填写必填字段")
    if not _sdk_available(kind):
        return _err(f"平台 {kind} 的通道 SDK 不可用（底层库缺失）。请检查 vendor/octop-gateway 是否完整。")
    ok, dep_msg = _ensure_deps(kind)
    if not ok:
        return _err(dep_msg)
    try:
        channel_cfg = _instance_to_channel_cfg(inst)
        cid = await _start_instance(instance_id, kind, channel_cfg)
        mark_auto_start(instance_id)
        return _ok({
            "instance_id": instance_id,
            "channel_id": cid,
            "running": True,
            "deps": dep_msg,
        }, "实例已启动")
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 启动实例 %s 失败", instance_id)
        return _err(f"启动实例 {instance_id} 失败: {e}")


@router.post("/gateway/instances/{instance_id}/stop")
async def gateway_stop_instance(instance_id: str) -> JSONResponse:
    """停止实例。"""
    if _find_instance(instance_id) is None:
        return _err(f"实例不存在: {instance_id}")
    try:
        await _stop_instance(instance_id)
        unmark_auto_start(instance_id)
        return _ok({"instance_id": instance_id, "running": False}, "实例已停止")
    except Exception as e:  # noqa: BLE001
        logger.exception("[gateway] 停止实例 %s 失败", instance_id)
        return _err(f"停止实例 {instance_id} 失败: {e}")
