"""浏览器环境探测 + Chromium 安装（对齐 Octop api/routers/browser/env.py）。

  GET  /api/v2/browser/env-status  → {playwright, browsers_ok, chrome_path, …}
  POST /api/v2/browser/install     → SSE 安装进度（复用系统 Chrome 或下载 Chromium）
"""

from __future__ import annotations

import glob as _glob
import json
import os
import subprocess
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()


def probe_env() -> dict[str, Any]:
    """探测浏览器环境（同步、无副作用）。"""
    out: dict[str, Any] = {
        "playwright": False,
        "browsers_ok": False,
        "harness_browser": False,
        "playwright_chromium": False,
        "chrome_path": None,
        "chrome_source": None,   # "system" | "playwright" | None
        "error": None,
    }

    # 系统 Chrome / Playwright Chromium
    try:
        from octop_browser.cdp.launcher import find_chrome  # noqa: PLC0415
        out["harness_browser"] = True
        chrome = find_chrome()
        if chrome:
            out["browsers_ok"] = True
            out["chrome_path"] = chrome
            out["chrome_source"] = "playwright" if "ms-playwright" in chrome else "system"
        else:
            out["error"] = (
                "未找到 Chrome/Chromium。请安装 Google Chrome，"
                "或调用 POST /api/v2/browser/install 下载 Playwright Chromium。"
            )
    except ImportError as exc:  # noqa: BLE001
        out["error"] = f"octop_browser 未就绪: {exc}"

    # Playwright 本体
    try:
        import playwright  # noqa: F401, PLC0415
        out["playwright"] = True
    except ImportError as exc:  # noqa: BLE001
        if not out["harness_browser"]:
            out["error"] = f"playwright 未安装: {exc}"

    # Playwright 自管 Chromium 是否存在（卸载目标，不涉及系统浏览器）
    try:
        cache = os.path.expanduser("~/.cache/ms-playwright")
        out["playwright_chromium"] = bool(_glob.glob(os.path.join(cache, "chromium-*")))
    except Exception:  # noqa: BLE001
        pass

    if not out["browsers_ok"] and not out.get("error"):
        out["error"] = (
            "未找到 Chrome/Chromium。请安装 Google Chrome，"
            "或调用 POST /api/v2/browser/install 下载 Playwright Chromium。"
        )
    return out


def _verify_binary(exe: str) -> tuple[bool, str]:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return False, f"浏览器验证超时: {exe}"
    except OSError as exc:
        return False, f"浏览器验证失败: {exc}"
    if r.returncode == 0:
        return True, (r.stdout or r.stderr or "").strip() or exe
    return False, f"退出码 {r.returncode}: {(r.stderr or r.stdout).strip()}"


async def install_stream() -> AsyncGenerator[str, None]:
    """SSE：优先复用系统 Chrome，否则下载 Playwright Chromium。"""
    def _sse(obj: dict[str, Any]) -> str:
        return f"data: {json.dumps(obj)}\n\n"
    try:
        from octop_browser.cdp.launcher import find_chrome  # noqa: PLC0415
        chrome = find_chrome()
        if chrome:
            yield _sse({"log": f"发现浏览器: {chrome}"})
            yield _sse({"log": "正在验证安装 ..."})
            ok, msg = _verify_binary(chrome)
            if ok:
                yield _sse({"log": msg})
                yield _sse({"done": True, "success": True})
                return
            yield _sse({"log": f"系统浏览器不可用（{msg}）；回退到 Playwright Chromium 下载 ..."})

        from octop_browser.install import install_chromium_stream  # noqa: PLC0415
        async for event in install_chromium_stream():
            yield _sse(event)
    except Exception as exc:  # noqa: BLE001
        yield _sse({"done": True, "success": False, "error": str(exc)})


@router.get("/browser/env-status")
async def env_status() -> dict[str, Any]:
    return {"status": True, "data": probe_env(), "msg": ""}


@router.post("/browser/install")
async def install() -> StreamingResponse:
    return StreamingResponse(
        install_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
