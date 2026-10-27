"""平台扩展层（Platform Ext）—— 自包含命名空间。


- teams    ：专家团队实体（持久化 roster + 模板），复用 BT 既有 AgentRegistry/RunCrew
- channels ：可插拔通道抽象（BaseChannel + 注册表 + WebSocket 通道）
- storage  ：可插拔工作区后端（本地/文件系统/Docker），默认本地
- cli      ：薄 CLI 入口（复用现有 HTTP API）
- usage    ：用量统计（JSONL ledger）
- gateway  ：多平台 IM 通道桥（封装 octop-gateway 官方包）

vendor 包（octop-gateway）随仓库携带在 vendor/ 下，
这里把 vendor 加入 sys.path 供懒加载。

设计约束（不破坏现有 API）：
- 所有新增 HTTP 路由走 /api/v2/* 前缀；
- 配置存 BT config.json 的 platform_ext 段（不引入数据库、不引入多用户）；
- 鉴权复用 BT auth.py 的 JWT verify_token。
"""

from __future__ import annotations

import os
import sys

__all__ = ["teams", "channels", "storage", "cli", "api", "usage", "gateway", ]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 把随仓库携带的 vendor 包加入 sys.path（octop-gateway 官方包）
_VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor")
# 优先把 vendor 根目录加入 sys.path，使自动安装到 vendor/ 的第三方依赖
# （如 paho-mqtt、discord.py、lark-oapi 等）也能被 import。
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)
for _v in ("octop-gateway",):
    _p = os.path.join(_VENDOR_DIR, _v)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# ── websockets 版本兼容补丁 ─────────────────────────────────────
# 主环境 .venv 的 websockets 被锁在 12.x（f2 库要求 <13），但该版本缺少
# websockets.asyncio 子模块（13.0 才引入）。wecom 通道的 wecom-aibot-sdk
# 依赖 websockets.asyncio.client，启动会 ModuleNotFoundError。
# vendor 目录已携带 websockets 17.2（完整支持 asyncio），且 octop-browser
# 已做 12/13+ 双兼容。因此这里若检测到当前 websockets 缺 asyncio，
# 就从 vendor 重新加载高版本替换 sys.modules，仅本进程生效，不改 .venv。
def _ensure_websockets_asyncio() -> None:
    try:
        import importlib.util

        if importlib.util.find_spec("websockets.asyncio") is not None:
            return
    except Exception:
        return  # websockets 未安装或不可用，交由后续依赖自检处理

    # 当前 websockets 不支持 asyncio（<13）。vendor 已携带完整版（17.2），
    # 但 sys.modules 里已缓存旧版，仅改 sys.path 不会重新加载。
    # 这里先剔除缓存的 websockets 及其子模块，再从 vendor 强制重新加载。
    try:
        import websockets as _old
        _old_path = getattr(_old, "__path__", None)
        _old_dir = getattr(_old, "__file__", None)
    except Exception:
        _old_path = None
        _old_dir = None

    vendor_ws = os.path.join(_VENDOR_DIR, "websockets")
    if os.path.isdir(vendor_ws) and vendor_ws not in sys.path:
        sys.path.insert(0, vendor_ws)

    # 仅当旧版确实来自 .venv（而非 vendor 自身）时才需要替换，避免误伤
    _replace = True
    if _old_dir and os.path.abspath(os.path.dirname(_old_dir)) == os.path.abspath(vendor_ws):
        _replace = False
    if not _replace:
        return

    try:
        # 移除缓存的 websockets 顶层模块及其子模块
        for _k in list(sys.modules):
            if _k == "websockets" or _k.startswith("websockets."):
                sys.modules.pop(_k, None)
        # 重新 import：此时 vendor 在 sys.path 最前，会命中 17.2
        import websockets as _ws
        import websockets.asyncio  # noqa: PLC0415
        sys.modules["websockets"] = _ws
        sys.modules["websockets.asyncio"] = websockets.asyncio
        if logger is not None:
            try:
                _old_ver = getattr(_old, "__version__", "?")
            except Exception:
                _old_ver = "?"
            logger.info(
                "websockets 已从 %s 升级为 %s（支持 asyncio），供 wecom 通道使用",
                _old_ver,
                getattr(_ws, "__version__", "?"),
            )
    except Exception:
        pass


try:
    import logging as _logging

    logger = _logging.getLogger("platform-ext")
except Exception:
    logger = None
if logger is not None:
    _ensure_websockets_asyncio()
