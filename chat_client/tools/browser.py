"""browser_use —— 内置浏览器自动化工具（对齐 Octop 的内置 browser 工具）。

底层走 vendored octop_browser 的 CDP 引擎（真实 Chrome），支持导航、
DOM 快照、点击、输入、截图、标签页等。BT 工具是同步的，这里用专用后台
事件循环跑 async browser_tool，避免与 FastAPI/agent 的 loop 冲突。

配合 /browser 可视化面板（实时画面 + 鼠标键盘控制）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

from . import register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

# 专用后台事件循环
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True, name="browser-use-loop")
            t.start()
            _loop = loop
        return _loop


def _run(coro, timeout: float = 120.0):
    return asyncio.run_coroutine_threadsafe(coro, _get_loop()).result(timeout=timeout)


BROWSER_USE_DESC = """
- 内置浏览器自动化（真实 Chrome，基于 CDP）。可打开网页、抓取 DOM、点击、
  输入、截图、管理标签页。适合需要真实渲染/登录态/JS 交互的网页任务。
- 需要「实时看画面 + 手动操作」时，用 Web 面板 /browser（不经过本工具）。
- 会话按 profile 复用，登录态（cookies）持久化在磁盘 profile 中。

可用 action：
  navigate      打开 URL（参数 url）
  dom_tree      获取页面可交互元素树（参数 level: interactive|full）
  screenshot    截图（返回 base64 图片说明）
  click         按坐标点击（参数 x, y）
  type          输入文本（参数 text；可选 ref 指定元素）
  fill          填充表单元素（参数 ref, text）
  press         按键（参数 key，如 Enter）
  select        下拉选择（参数 ref, text=值）
  scroll        滚动（参数 direction: up|down, amount）
  hover         悬停（参数 x, y 或 ref）
  eval_js       执行 JS（参数 expression）
  wait          等待（参数 key=CSS 选择器）
  go_back / go_forward / reload
  new_tab / switch_tab（tab_id）/ close_tab（tab_id）/ list_tabs
  close_session 关闭浏览器会话
"""


@register_tool(category="浏览器", name_cn="浏览器自动化", risk_level="medium", timeout=300)
def browser_use(action: str, url: str = "", x: int = 0, y: int = 0,
                text: str = "", ref: str = "", key: str = "",
                direction: str = "down", amount: int = 300,
                expression: str = "", tab_id: str = "",
                level: str = "interactive", profile: str = "default",
                **kwargs) -> str:
    """
    内置浏览器自动化（真实 Chrome / CDP）。打开网页、抓取 DOM、点击、输入、
    截图、管理标签页。适合需要真实渲染 / 登录态 / JS 交互的网页任务。

    Args:
        action: 操作类型（navigate/dom_tree/screenshot/click/type/fill/press/
            select/scroll/hover/eval_js/wait/go_back/go_forward/reload/
            new_tab/switch_tab/close_tab/list_tabs/close_session）
        url: navigate 时的目标 URL
        x: 点击/悬停的 X 坐标
        y: 点击/悬停的 Y 坐标
        text: type/fill/select 的文本
        ref: 元素引用（dom_tree 返回的 ref）
        key: press 的按键 或 wait 的 CSS 选择器
        direction: scroll 方向（up/down）
        amount: scroll 像素数
        expression: eval_js 的 JS 表达式
        tab_id: 标签页 ID
        level: dom_tree 详细度（interactive/full）
        profile: 浏览器 profile 名（默认 default，登录态按 profile 持久化）
    """
    try:
        from platform_ext.browser import ensure_vendor_on_path  # noqa: PLC0415
        ensure_vendor_on_path()
        from octop_browser.tool_interface import browser_tool  # noqa: PLC0415
    except ImportError as exc:  # noqa: BLE001
        return _xml_response("error", f"浏览器引擎未就绪: {exc}")

    params: dict[str, Any] = {}
    if action == "navigate":
        if not url:
            return _xml_response("error", "navigate 需要 url 参数")
        params["url"] = url
    elif action == "click":
        params["x"] = x
        params["y"] = y
        if ref:
            params["ref"] = ref
    elif action == "hover":
        params["x"] = x
        params["y"] = y
        if ref:
            params["ref"] = ref
    elif action in ("type", "fill"):
        params["text"] = text
        if ref:
            params["ref"] = ref
    elif action == "press":
        params["key"] = key
    elif action == "select":
        params["ref"] = ref
        params["value"] = text
    elif action == "scroll":
        params["direction"] = direction
        params["amount"] = amount
    elif action == "eval_js":
        if not expression:
            return _xml_response("error", "eval_js 需要 expression 参数")
        params["expression"] = expression
    elif action == "wait":
        if key:
            params["for_selector"] = key
    elif action in ("switch_tab", "close_tab"):
        if tab_id:
            params["tab_id"] = tab_id
    elif action == "dom_tree":
        params["level"] = level

    try:
        result = _run(browser_tool(action=action, profile=profile, **params))
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser_use failed: action=%s", action)
        return _xml_response("error", f"浏览器操作失败（{action}）: {exc}")

    if not getattr(result, "success", False):
        return _xml_response("error", getattr(result, "error", None) or f"{action} 执行失败")
    content = getattr(result, "content", "") or ""
    # dom_tree / screenshot 返回结构化 JSON，直接透传
    if content.strip().startswith(("{", "[")):
        return content
    return _xml_response("success", content)
