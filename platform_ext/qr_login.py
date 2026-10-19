"""多平台扫码登录。

支持：weixin(iLink Bot)/qq/wecom/dingtalk 四平台扫码绑定，
生成二维码 -> 轮询结果 -> 成功自动保存配置到 config.json。

- weixin/qq   复用底层通道库的 login_qr（WeixinQRLogin / QQBotQRLogin）
- wecom       直连企业微信公开接口完成扫码授权
- dingtalk    走钉钉开放平台的 Device Flow 注册流程
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from platform_ext import PROJECT_ROOT, config_files

# 登录会话状态（内存，按 session_id 区分多并发扫码）
_sessions: dict[str, dict[str, Any]] = {}


# ── 配置读写（写入 config.json 的 platform_ext.gateway[<kind>]）──
def _config_path() -> str:
    """返回 gateway 独立配置文件路径（兼容测试 monkeypatch）。"""
    return config_files.section_file("gateway")


def _gateway_cfg() -> dict[str, Any]:
    return config_files.load_section("gateway", _config_path())


def _set_gateway_cfg(cfg: dict[str, Any]) -> None:
    config_files.save_section("gateway", cfg, _config_path())


def save_channel_config(kind: str, config: dict[str, Any]) -> None:
    """把扫码拿到的凭据写入配置（自动标记 configured，并同步 legacy 实例）。"""
    g = _gateway_cfg()
    existing = dict(g.get(kind, {}))
    existing.update({k: v for k, v in config.items() if v})
    existing["configured"] = True
    g[kind] = existing
    # 同步 instance_id == kind 的 legacy 实例，避免实例面板与扫码结果脱节。
    # instances 尚未生成时交由 gateway 的首次读取迁移处理（会从 g[kind] 建实例）。
    instances = g.get("instances")
    if isinstance(instances, list):
        target = next((it for it in instances if it.get("instance_id") == kind), None)
        if target is None:
            target = {"instance_id": kind, "kind": kind, "enabled": True}
            instances.append(target)
        target.update({k: v for k, v in config.items() if v})
        target["instance_id"] = kind
        target["kind"] = kind
        target["configured"] = True
        g["instances"] = instances
    _set_gateway_cfg(g)


# ── 微信 iLink Bot ────────────────────────────────────────────
async def weixin_generate() -> dict[str, Any]:
    from octop_gateway.channels.weixin.login_qr import WeixinQRLogin
    login = WeixinQRLogin()
    result = await login.fetch_qr_code()
    return {"qrcode_token": str(result.qrcode), "qrcode_url": str(result.qrcode_img_content)}


async def weixin_poll(qrcode_token: str) -> dict[str, Any]:
    from octop_gateway.channels.weixin.login_qr import WeixinQRLogin
    login = WeixinQRLogin()
    try:
        result = await asyncio.wait_for(login.wait_for_login(qrcode_token), timeout=60.0)
    except asyncio.TimeoutError:
        return {"status": "wait"}
    if not result.connected:
        return {"status": "error", "message": result.message}
    return {
        "status": "success",
        "account_id": result.account_id,
        "token": result.bot_token,
        "base_url": result.base_url,
    }


# ── QQ ─────────────────────────────────────────────────────────
async def qq_generate() -> dict[str, Any]:
    from octop_gateway.channels.qq.login_qr import QQBotQRLogin
    login = QQBotQRLogin(source="bt")
    result = await login.fetch_qr_code()
    return {"qrcode_token": str(result.task_id), "qrcode_url": str(result.qrcode_url)}


async def qq_poll(task_id: str) -> dict[str, Any]:
    from octop_gateway.channels.qq.login_qr import QQBotQRLogin
    login = QQBotQRLogin(source="bt")
    result = await login.poll(task_id)
    if result.status == "pending":
        return {"status": "pending"}
    if result.status == "expired":
        return {"status": "expired", "message": "QQ 二维码已过期"}
    if not result.credentials:
        return {"status": "error", "message": "QQ 扫码绑定未返回凭据"}
    cred = result.credentials[0]
    return {
        "status": "success",
        "app_id": cred.app_id,
        "secret": cred.app_secret,
        "user_openid": cred.user_openid,
    }


# ── 企业微信 WeCom ──────────────────────────────────────────
async def wecom_generate() -> dict[str, Any]:
    url = "https://work.weixin.qq.com/ai/qc/generate?source=bt"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.json()
    inner = data.get("data") or {}
    scode = inner.get("scode")
    auth_url = inner.get("auth_url")
    if not scode or not auth_url:
        raise RuntimeError("unexpected WeCom QR response")
    return {"scode": str(scode), "auth_url": str(auth_url)}


async def wecom_poll(scode: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://work.weixin.qq.com/ai/qc/query_result",
            params={"scode": scode},
        )
        resp.raise_for_status()
        data = resp.json()
    inner = data.get("data") or {}
    status = inner.get("status", "pending")
    if status == "success":
        bot_info = inner.get("bot_info") or {}
        bot_id = bot_info.get("botid", "")
        secret = bot_info.get("secret", "")
        if not bot_id or not secret:
            return {"status": "error", "reason": "扫码成功但缺少机器人信息"}
        return {"status": "success", "bot_id": bot_id, "secret": secret}
    return {"status": status}


# ── 钉钉 DingTalk Device Flow ────────────────────────────────
_DINGTALK_BASE = "https://oapi.dingtalk.com"
_DINGTALK_TIMEOUT = 15.0


def _dt_require_success(data: Any, operation: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError(f"DingTalk {operation} returned invalid response")
    if data.get("errcode") != 0:
        raise RuntimeError(f"DingTalk {operation} failed: {data.get('errmsg') or 'unknown'}")
    return data


async def dingtalk_generate() -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=_DINGTALK_BASE, timeout=_DINGTALK_TIMEOUT) as client:
        init_resp = await client.post("/app/registration/init", json={})
        init_resp.raise_for_status()
        init = _dt_require_success(init_resp.json(), "init")
        nonce = init.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise RuntimeError("DingTalk registration init missing nonce")
        begin_resp = await client.post("/app/registration/begin", json={"nonce": nonce})
        begin_resp.raise_for_status()
        started = _dt_require_success(begin_resp.json(), "begin")
    for key in ("device_code", "user_code", "verification_uri_complete"):
        if not isinstance(started.get(key), str) or not started[key]:
            raise RuntimeError("DingTalk registration begin incomplete")
    return started


async def dingtalk_poll(device_code: str) -> dict[str, Any]:
    async with httpx.AsyncClient(base_url=_DINGTALK_BASE, timeout=_DINGTALK_TIMEOUT) as client:
        resp = await client.post("/app/registration/poll", json={"device_code": device_code})
        resp.raise_for_status()
    return _dt_require_success(resp.json(), "poll")


# ── 统一入口：按平台分发 generate / poll ─────────────────────
_GENERATORS = {
    "weixin": weixin_generate,
    "qq": qq_generate,
    "wecom": wecom_generate,
    "dingtalk": dingtalk_generate,
}

# poll 函数的参数名映射：weixin/wecom 用 qrcode_token/scode，qq 用 task_id，dingtalk 用 device_code
_POLL_ARGS = {
    "weixin": "qrcode_token",
    "qq": "task_id",
    "wecom": "scode",
    "dingtalk": "device_code",
}

QR_KINDS = frozenset(_GENERATORS)


def supported_qr_kinds() -> list[str]:
    return sorted(QR_KINDS)


async def generate(kind: str, session_id: str = "") -> dict[str, Any]:
    """生成二维码。返回的元数据连同 session_id 存入内存，供 poll 使用。"""
    gen = _GENERATORS.get(kind)
    if gen is None:
        raise KeyError(f"unsupported QR kind: {kind}")
    result = await gen()
    result["kind"] = kind
    result["session_id"] = session_id
    result["created_at"] = int(time.time())
    # 保存会话（键 = session_id:kind，便于多会话并发）
    if session_id:
        _sessions[f"{session_id}:{kind}"] = result
    return result


async def poll(kind: str, token: str, session_id: str = "") -> dict[str, Any]:
    """轮询扫码结果；成功后自动把凭据写入 config.json。"""
    if kind == "weixin":
        result = await weixin_poll(token)
    elif kind == "qq":
        result = await qq_poll(token)
    elif kind == "wecom":
        result = await wecom_poll(token)
    elif kind == "dingtalk":
        result = await dingtalk_poll(token)
    else:
        raise KeyError(f"unsupported QR kind: {kind}")

    if result.get("status") == "success":
        # 自动保存凭据到配置（各平台字段不同）
        if kind == "weixin":
            save_channel_config("weixin", {
                "account_id": result.get("account_id", ""),
                "token": result.get("token", ""),
                "base_url": result.get("base_url") or "https://ilinkai.weixin.qq.com",
            })
        elif kind == "qq":
            save_channel_config("qq", {
                "app_id": result.get("app_id", ""),
                "secret": result.get("secret", ""),
                "user_openid": result.get("user_openid", ""),
            })
        elif kind == "wecom":
            save_channel_config("wecom", {
                "bot_id": result.get("bot_id", ""),
                "secret": result.get("secret", ""),
            })
        elif kind == "dingtalk":
            save_channel_config("dingtalk", {
                "device_code": token,
            })
    # 清理已结束的会话
    if session_id:
        _sessions.pop(f"{session_id}:{kind}", None)
    return result
