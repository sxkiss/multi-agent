"""浏览器会话接管助手（对齐 Octop api/routers/browser/harness.py）。

BT 是单用户，profile 固定为 "default"（可配置）。负责：
- 解析/复用活跃 BrowserSession（带健康检查）
- 会话状态（url / tabs）
- 控制权交接（agent ↔ user）
- 关闭浏览器会话

  GET  /api/v2/browser/harness-sessions
  POST /api/v2/browser/sessions/{session_id}/handoff
  POST /api/v2/browser/close
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger("platform-ext-browser-harness")

router = APIRouter()

# 单用户默认 profile（可被 BROWSER_PROFILE 环境变量覆盖）
DEFAULT_PROFILE = os.environ.get("BROWSER_PROFILE", "default")

# 控制权归属（session_id -> "agent" | "user"）
_CONTROL_OWNERS: dict[str, str] = {}


def control_owner_for(session_id: str) -> str:
    return _CONTROL_OWNERS.get(session_id, "agent")


def set_control_owner(session_id: str, owner: str) -> None:
    _CONTROL_OWNERS[session_id] = owner


class HandoffBody(BaseModel):
    target: str  # "agent" | "user"
    reason: str = ""


async def _is_session_alive(sess: Any, *, timeout: float = 2.0) -> bool:
    """用一次轻量 CDP 往返探测会话是否仍存活。"""
    try:
        client = sess._internal.client  # noqa: SLF001
        send = getattr(client, "send_passive", client.send)
        await asyncio.wait_for(
            send("Runtime.evaluate", {"expression": "1", "returnByValue": True}),
            timeout=timeout,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("cached harness session failed health check: %s", exc)
        return False


async def resolve_harness_session(profile_hint: str | None, *, create: bool = True) -> Any | None:
    """返回 profile 对应的活跃 BrowserSession（复用前做健康检查）。"""
    from octop_browser.tool_interface import _registry, _get_or_create_session, _drop_session  # noqa: PLC0415

    profile = (profile_hint or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    sess = _registry.get(profile)
    if sess is not None and not await _is_session_alive(sess):
        await _drop_session(profile)
        sess = None
    if sess is None:
        if not create:
            return None
        sess = await _get_or_create_session(profile)
    return sess


async def harness_page_url(sess: Any) -> str:
    if sess is None:
        return ""
    try:
        return await sess._current_page_url() or ""  # noqa: SLF001
    except Exception:  # noqa: BLE001
        return ""


async def harness_list_tabs(sess: Any) -> list[dict[str, Any]]:
    if sess is None:
        return []
    try:
        result = await sess.list_tabs()
        import json as _json  # noqa: PLC0415
        content = getattr(result, "content", "") or ""
        if content.strip().startswith(("{", "[")):
            data = _json.loads(content)
            if isinstance(data, dict):
                return data.get("tabs", [])
            if isinstance(data, list):
                return data
        return []
    except Exception:  # noqa: BLE001
        return []


def _session_payload(profile: str, sess: Any | None, url: str, tabs: list[dict[str, Any]]) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "session_id": profile,
        "profile_name": profile,
        "conversation_id": profile,
        "channel_source": "dashboard",
        "state": "streaming" if url else "idle",
        "control_owner": control_owner_for(profile),
        "current_url": url,
        "tabs": tabs,
        "created_at": now,
        "last_activity_at": now,
    }


@router.get("/browser/harness-sessions")
async def list_harness_sessions() -> dict[str, Any]:
    from octop_browser.tool_interface import _registry  # noqa: PLC0415
    sessions = []
    for profile in list(_registry.keys()):
        sess = await resolve_harness_session(profile, create=False)
        url = await harness_page_url(sess) if sess else ""
        tabs = await harness_list_tabs(sess) if sess else []
        sessions.append(_session_payload(profile, sess, url, tabs))
    return {"status": True, "data": {"sessions": sessions, "default_profile": DEFAULT_PROFILE}, "msg": ""}


@router.post("/browser/sessions/{session_id}/handoff")
async def handoff(session_id: str, body: HandoffBody) -> dict[str, Any]:
    target = body.target if body.target in ("agent", "user") else "agent"
    set_control_owner(session_id, target)
    sess = await resolve_harness_session(session_id, create=False)
    url = await harness_page_url(sess) if sess else ""
    tabs = await harness_list_tabs(sess) if sess else []
    return {"status": True, "data": {"ok": True, "session": _session_payload(session_id, sess, url, tabs)}, "msg": ""}


@router.post("/browser/close")
async def close_browser_session() -> dict[str, Any]:
    """结束当前用户的浏览器会话（登录态 cookies 保留在磁盘 profile）。"""
    try:
        from octop_browser.tool_interface import browser_tool  # noqa: PLC0415
    except ImportError as exc:
        return {"status": False, "data": None, "msg": f"octop-browser 未就绪: {exc}"}
    result = await browser_tool(action="close_session", profile=DEFAULT_PROFILE, kill=True)
    _CONTROL_OWNERS.pop(DEFAULT_PROFILE, None)
    if not result.success:
        return {"status": False, "data": None, "msg": result.error or "关闭失败"}
    return {"status": True, "data": {"ok": True, "profile": DEFAULT_PROFILE}, "msg": ""}
