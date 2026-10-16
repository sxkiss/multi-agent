"""浏览器卸载（对齐 Octop api/routers/browser/uninstall.py）。

  POST /api/v2/browser/uninstall  → SSE 卸载进度（仅移除 Playwright 自管 Chromium，
  不动系统 Chrome）。
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter()


def _playwright_chromium_dirs() -> list[str]:
    cache = os.path.expanduser("~/.cache/ms-playwright")
    return sorted(glob.glob(os.path.join(cache, "chromium-*")))


async def uninstall_stream() -> AsyncGenerator[str, None]:
    def _sse(obj: dict[str, Any]) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    dirs = _playwright_chromium_dirs()
    if not dirs:
        yield _sse({"log": "未发现 Playwright Chromium 安装（无需卸载）。"})
        yield _sse({"done": True, "success": True})
        return

    removed_any = False
    for cdir in dirs:
        yield _sse({"log": f"移除 Playwright Chromium: {cdir} ..."})
        try:
            await asyncio.to_thread(shutil.rmtree, cdir)
            removed_any = True
            yield _sse({"log": f"已移除 {os.path.basename(cdir)}。"})
        except OSError as exc:
            yield _sse({"done": True, "success": False, "error": str(exc), "log": str(exc)})
            return

    if removed_any:
        yield _sse({"log": "已移除 Playwright Chromium；系统 Chrome/Chromium 未受影响。"})
    yield _sse({"done": True, "success": True})


@router.post("/browser/uninstall")
async def uninstall() -> StreamingResponse:
    return StreamingResponse(
        uninstall_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
