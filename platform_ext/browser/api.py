"""浏览器基础设施 API —— /api/v2/browser/*

合并 env / harness / record_replay / uninstall / stream 五组路由。
所有路由挂在新前缀 /api/v2/browser/* 下，与现有 /api/* 完全隔离，
由 BT 全局 auth 中间件自动保护（WS 端点用 ?token= 校验）。

FastAPI 0.141 的嵌套 include_router 不再自动叠加 prefix，因此这里
统一为每个子 router 显式设置 prefix="/api/v2"，再导出 subrouters。
"""

from __future__ import annotations

from fastapi import APIRouter

from platform_ext.browser import env as _env
from platform_ext.browser import harness as _harness
from platform_ext.browser import proxy_config as _proxy
from platform_ext.browser import record_replay as _rr
from platform_ext.browser import stream as _stream
from platform_ext.browser import uninstall as _uninstall

# 显式设置前缀（FastAPI 0.141 嵌套不叠加 prefix）
for _r in (_env.router, _harness.router, _proxy.router, _rr.router, _uninstall.router, _stream.router):
    _r.prefix = "/api/v2"

# 供 web_server 逐个挂载
subrouters: list[APIRouter] = [
    _env.router,
    _harness.router,
    _proxy.router,
    _rr.router,
    _uninstall.router,
    _stream.router,
]

# 兼容：顶层 router（可被 include_router，但路径可能不展平；优先用 subrouters）
router = APIRouter(prefix="/api/v2")
for r in subrouters:
    router.include_router(r)