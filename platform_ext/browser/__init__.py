"""浏览器基础设施（对齐 Octop infra/browser + api/routers/browser）。

Octop 的浏览器是**内置基础设施**（自带 CDP 引擎 + 实时画面流 + 录制回放 +
会话接管 + 一键安装），不是「技能」。本包复现该能力。

实现方式：vendor 官方 `octop-browser` 包（PyPI 公开，45 文件 CDP 引擎），
打一个 websockets 版本兼容补丁（BT 环境 websockets==12，f2 库要求 <13），
在其上移植 Octop 的路由层（env/stream/harness/record_replay/uninstall）。

不破坏现有 API：所有路由走 /api/v2/browser/*。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(_HERE, "_vendor")


def ensure_vendor_on_path() -> None:
    """把 vendored octop_browser 加入 sys.path（幂等）。"""
    if _VENDOR not in sys.path:
        sys.path.insert(0, _VENDOR)


# 导入本包即确保 vendor 可用
ensure_vendor_on_path()

__all__ = ["env", "stream", "harness", "record_replay", "uninstall", "tool", "api"]
