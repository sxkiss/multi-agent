"""平台扩展层（Platform Ext）—— 自包含命名空间。


- teams    ：专家团队实体（持久化 roster + 模板），复用 BT 既有 AgentRegistry/RunCrew
- channels ：可插拔通道抽象（BaseChannel + 注册表 + WebSocket 通道）
- storage  ：可插拔工作区后端（本地/文件系统/Docker），默认本地
- cli      ：薄 CLI 入口（复用现有 HTTP API）
- usage    ：用量统计（JSONL ledger）
- gateway  ：多平台 IM 通道桥（封装 octop-gateway 官方包）
- memory   ：增强分层记忆（封装 octop-memory 官方包）

vendor 包（octop-gateway / octop-memory）随仓库携带在 vendor/ 下，
这里把 vendor 加入 sys.path 供懒加载。

设计约束（不破坏现有 API）：
- 所有新增 HTTP 路由走 /api/v2/* 前缀；
- 配置存 BT config.json 的 platform_ext 段（不引入数据库、不引入多用户）；
- 鉴权复用 BT auth.py 的 JWT verify_token。
"""

from __future__ import annotations

import os
import sys

__all__ = ["teams", "channels", "storage", "cli", "api", "usage", "gateway", "memory_octop"]

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 把随仓库携带的 vendor 包加入 sys.path（octop-gateway / octop-memory 官方包）
_VENDOR_DIR = os.path.join(PROJECT_ROOT, "vendor")
# 优先把 vendor 根目录加入 sys.path，使自动安装到 vendor/ 的第三方依赖
# （如 paho-mqtt、discord.py、lark-oapi 等）也能被 import。
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)
for _v in ("octop-gateway", "octop-memory"):
    _p = os.path.join(_VENDOR_DIR, _v)
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
