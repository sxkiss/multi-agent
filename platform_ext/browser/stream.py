"""浏览器实时画面流（WebSocket screencast）—— 对齐 Octop api/routers/browser/stream.py。

客户端 → 服务端::

  {"type": "start", "url": "", "width": 1280, "height": 800, "session_id": "<profile>"}
  {"type": "navigate", "url": "https://..."}
  {"type": "click", "x": 100, "y": 200}
  {"type": "mousedown"/"mouseup"/"mousemove", "x":.., "y":.., "button":"left", "buttons":1}
  {"type": "scroll", "deltaY": 300, "x":.., "y":..}
  {"type": "type", "text": "..."}
  {"type": "keydown", "key": "Enter"}
  {"type": "tab_switch"/"tab_close"/"tab_new", "tab_id": "..."}
  {"type": "goback"/"goforward"/"reload"}
  {"type": "resize", "width":.., "height":..}
  {"type": "stop"}

服务端 → 客户端::

  {"type": "status", "status": "streaming"}
  {"type": "frame", "data": "<base64 jpeg>"}
  {"type": "tabs", "tabs": [{id, url, title, active}]}
  {"type": "session_update", "session_id": "...", "current_url": "...", ...}
  {"type": "error", "message": "..."}

鉴权：?token=<JWT>（浏览器 WS 无法带 Authorization 头）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from platform_ext.browser.harness import (
    DEFAULT_PROFILE,
    control_owner_for,
    harness_list_tabs,
    harness_page_url,
    resolve_harness_session,
)

logger = logging.getLogger("platform-ext-browser-stream")

router = APIRouter()

_FRAME_INTERVAL_S = 0.25  # ~4 fps


def _normalize_nav_url(raw: str) -> str:
    t = (raw or "").strip()
    if not t:
        return ""
    if t.lower().startswith(("http://", "https://")):
        return t
    return f"https://{t}"


async def _capture_jpeg(sess: Any) -> str | None:
    try:
        if not getattr(sess._internal, "_connected", False):  # noqa: SLF001
            return None
        result = await asyncio.wait_for(
            sess._internal.client.send(  # noqa: SLF001
                "Page.captureScreenshot",
                {"format": "jpeg", "quality": 80},
            ),
            timeout=1.5,
        )
        data = result.get("data")
        return str(data) if data else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("capture jpeg failed: %s", exc)
        return None


async def _send_json(ws: WebSocket, payload: dict[str, Any]) -> None:
    if ws.application_state == WebSocketState.CONNECTED:
        await ws.send_text(json.dumps(payload))


async def _send_session_snapshot(send_json: Any, profile: str, *, sess: Any | None) -> None:
    if sess is None:
        await send_json({
            "type": "session_update", "session_id": profile, "conversation_id": profile,
            "channel_source": "dashboard", "state": "idle",
            "control_owner": control_owner_for(profile), "current_url": "",
        })
        await send_json({"type": "tabs", "tabs": []})
        return
    url = await harness_page_url(sess)
    await send_json({
        "type": "session_update", "session_id": profile, "conversation_id": profile,
        "channel_source": "dashboard", "state": "streaming" if url else "idle",
        "control_owner": control_owner_for(profile), "current_url": url,
    })
    await send_json({"type": "tabs", "tabs": await harness_list_tabs(sess)})


async def _stream_loop(send_json: Any, is_connected: Any, sess: Any, profile: str) -> None:
    await send_json({"type": "status", "status": "browser_started"})
    await send_json({"type": "status", "status": "streaming"})
    while is_connected():
        await _send_session_snapshot(send_json, profile, sess=sess)
        frame = await _capture_jpeg(sess)
        if frame:
            await send_json({"type": "frame", "data": frame})
        await asyncio.sleep(_FRAME_INTERVAL_S)


_CDP_BUTTON_MASK = {"left": 1, "right": 2, "middle": 4, "none": 0}


def _cdp_button(raw: object) -> str:
    name = str(raw or "left").lower()
    return name if name in _CDP_BUTTON_MASK else "left"


def _cdp_buttons(msg: dict[str, Any], *, button: str, event: str) -> int:
    if msg.get("buttons") is not None:
        try:
            return max(0, int(msg["buttons"]))
        except (TypeError, ValueError):
            pass
    if event == "mouseup" or button == "none":
        return 0
    return _CDP_BUTTON_MASK.get(button, 0)


async def _dispatch_mouse(sess: Any, *, event_type: str, x: int, y: int,
                          button: str = "none", buttons: int = 0, click_count: int = 0) -> None:
    params: dict[str, Any] = {"type": event_type, "x": x, "y": y, "button": button, "buttons": buttons}
    if click_count:
        params["clickCount"] = click_count
    await sess._internal.client.send("Input.dispatchMouseEvent", params)  # noqa: SLF001


def _cdp_click_count(msg: dict[str, Any]) -> int:
    try:
        return max(1, int(msg.get("clickCount", msg.get("click_count", 1))))
    except (TypeError, ValueError):
        return 1


async def _handle_client_event(sess: Any, msg: dict[str, Any]) -> None:
    t = msg.get("type")
    if t == "navigate":
        url = _normalize_nav_url(str(msg.get("url") or ""))
        if url:
            await sess.navigate(url)
    elif t == "goback":
        await sess.go_back()
    elif t == "goforward":
        await sess.go_forward()
    elif t == "reload":
        await sess.reload()
    elif t == "click":
        await sess.click(x=int(msg.get("x") or 0), y=int(msg.get("y") or 0))
    elif t == "dblclick":
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        await sess.click(x=x, y=y)
        await sess.click(x=x, y=y)
    elif t in ("mousedown", "mouseup", "mousemove"):
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        button = _cdp_button(msg.get("button"))
        buttons = _cdp_buttons(msg, button=button, event=str(t))
        cc = _cdp_click_count(msg)
        if t == "mousedown":
            await _dispatch_mouse(sess, event_type="mousePressed", x=x, y=y,
                                  button=button if button != "none" else "left",
                                  buttons=buttons or _CDP_BUTTON_MASK.get(button, 1), click_count=cc)
        elif t == "mouseup":
            await _dispatch_mouse(sess, event_type="mouseReleased", x=x, y=y,
                                  button=button if button != "none" else "left", buttons=0, click_count=cc)
        else:
            await _dispatch_mouse(sess, event_type="mouseMoved", x=x, y=y, button=button, buttons=buttons)
    elif t == "scroll":
        delta_x = float(msg.get("deltaX") or msg.get("delta_x") or 0)
        delta_y = float(msg.get("deltaY") or msg.get("delta_y") or 0)
        x, y = int(msg.get("x") or 0), int(msg.get("y") or 0)
        if abs(delta_x) > 0.5 or abs(delta_y) > 0.5:
            with contextlib.suppress(Exception):
                await _dispatch_mouse(sess, event_type="mouseMoved", x=x, y=y)
                await sess._internal.client.send(  # noqa: SLF001
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y},
                )
        else:
            direction = "down" if delta_y > 0 else "up"
            await sess.scroll(direction=direction, amount=max(int(abs(delta_y)), 50))
    elif t == "type":
        text = str(msg.get("text") or "")
        if text:
            await sess.type(text)
    elif t == "keydown":
        key = str(msg.get("key") or "")
        if key == "Enter":
            await sess.type("\n")
    elif t == "tab_switch":
        if msg.get("tab_id") is not None:
            await sess.switch_tab(str(msg["tab_id"]))
    elif t == "tab_close":
        if msg.get("tab_id") is not None:
            await sess.close_tab(str(msg["tab_id"]))
    elif t == "tab_new":
        await sess.new_tab()
    elif t == "resize":
        w, h = int(msg.get("width") or 0), int(msg.get("height") or 0)
        if w > 0 and h > 0:
            with contextlib.suppress(Exception):
                await sess._internal.client.send(  # noqa: SLF001
                    "Emulation.setDeviceMetricsOverride",
                    {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False},
                )


def _verify_ws_token(token: str) -> bool:
    """校验 WS 连接的 JWT（?token=）。"""
    try:
        import os
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        import auth  # noqa: PLC0415
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        return bool(auth.verify_token(base_dir, token))
    except Exception as exc:  # noqa: BLE001
        logger.debug("ws token verify failed: %s", exc)
        return False


@router.websocket("/browser-stream/ws")
async def browser_stream_ws(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    width: int = Query(default=1280),
    height: int = Query(default=800),
) -> None:
    # 若启用了鉴权则强制校验；未启用时放行（与 BT 全局中间件一致）
    if token and not _verify_ws_token(token):
        await websocket.close(code=4001, reason="invalid token")
        return
    await websocket.accept()

    async def send_json(payload: dict[str, Any]) -> None:
        await _send_json(websocket, payload)

    def is_connected() -> bool:
        return websocket.application_state == WebSocketState.CONNECTED

    sess: Any | None = None
    stream_task: asyncio.Task | None = None
    profile = DEFAULT_PROFILE
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)
        start_msg = json.loads(raw)
        if not isinstance(start_msg, dict) or start_msg.get("type") != "start":
            await send_json({"type": "error", "message": "expected start message"})
            return
        profile = str(start_msg.get("session_id") or DEFAULT_PROFILE)

        sess = await resolve_harness_session(profile)
        if sess is None:
            await send_json({"type": "error", "message": "无法创建浏览器会话"})
            return

        start_url = _normalize_nav_url(str(start_msg.get("url") or ""))
        if start_url and start_url != "about:blank":
            try:
                await sess.navigate(start_url)
            except Exception as exc:  # noqa: BLE001
                await send_json({"type": "error", "message": f"导航失败：{exc}"})

        vw, vh = int(start_msg.get("width") or width), int(start_msg.get("height") or height)
        if vw > 0 and vh > 0:
            with contextlib.suppress(Exception):
                await sess._internal.client.send(  # noqa: SLF001
                    "Emulation.setDeviceMetricsOverride",
                    {"width": vw, "height": vh, "deviceScaleFactor": 1, "mobile": False},
                )

        stream_task = asyncio.create_task(_stream_loop(send_json, is_connected, sess, profile))

        while is_connected():
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "stop":
                break
            with contextlib.suppress(Exception):
                await _handle_client_event(sess, msg)
    except TimeoutError:
        await send_json({"type": "error", "message": "等待 start 超时"})
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser stream failed")
        if is_connected():
            with contextlib.suppress(Exception):
                await send_json({"type": "error", "message": str(exc)})
    finally:
        if stream_task is not None:
            stream_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close()
