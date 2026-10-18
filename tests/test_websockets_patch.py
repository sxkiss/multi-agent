"""websockets 版本兼容补丁测试：确保 platform_ext 加载后 websockets.asyncio 可用。"""
import importlib
import os
import sys


def test_websockets_asyncio_available_after_platform_ext():
    """模拟 web_server 启动：fastapi 先加载 websockets 12，platform_ext 补丁升级到 vendor 版。"""
    # 前置：确保 fastapi 已加载（会拉入 .venv 的 websockets）
    import fastapi  # noqa: F401

    # 触发补丁
    import platform_ext  # noqa: F401

    import websockets
    assert hasattr(websockets, "__version__")

    # 关键：websockets.asyncio 必须可用（补丁后）
    spec = importlib.util.find_spec("websockets.asyncio")
    assert spec is not None, "websockets.asyncio 应可用（补丁后）"

    # 且 websockets 版本应 >=13（来自 vendor）
    from packaging import version

    assert version.parse(websockets.__version__) >= version.parse("13.0")


def test_wecom_sdk_importable():
    """wecom_aibot_sdk 应能正常 import（依赖 websockets.asyncio）。"""
    import fastapi  # noqa: F401
    import platform_ext  # noqa: F401

    from wecom_aibot_sdk.ws import WsConnectionManager  # noqa: F401