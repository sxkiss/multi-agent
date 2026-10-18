"""多平台 IM 通道 → BT 对话桥接（MessageProcessor）。

把通道收到的 InboundMessage 桥接到 BT 现有 agent 对话体系：

- 用 channel_subject.subject_id 生成稳定 session_id（gw_<channel_type>_<subject>）；
- 调用 agent_main.chat_start 启动后台对话任务；
- 异步迭代 agent_main.chat_events 读取流式回复；
- 把文本增量 yield 成 MessageEvent.delta，结束时 completed()。

这样通道消息能真正进入 BT 的对话/记忆/工具体系，实现"对话绑定"。
"""

from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator

logger = logging.getLogger("platform-ext-gateway-processor")

# 合法 session 字符（与 web_server._validate_session_id 对齐）
_SESSION_SAFE = re.compile(r"[^A-Za-z0-9_.:@-]")


def _safe_session_component(value: str) -> str:
    """清洗 subject_id 等，得到仅含安全字符的会话分量。"""
    cleaned = _SESSION_SAFE.sub("_", str(value or "anon"))
    cleaned = cleaned.strip("._")
    return cleaned[:64] or "anon"


def session_id_for(channel_type: str, subject_id: str) -> str:
    """生成稳定的 BT 会话 ID：gw_<channel_type>_<subject>。"""
    return f"gw_{_safe_session_component(channel_type)}_{_safe_session_component(subject_id)}"


def _extract_text(message) -> str:
    """从 InboundMessage 提取纯文本。"""
    try:
        return message.text or ""
    except Exception:  # noqa: BLE001
        try:
            parts = getattr(message, "content", []) or []
            return "\n".join(
                getattr(c, "text", "") for c in parts if getattr(c, "text", "")
            )
        except Exception:  # noqa: BLE001
            return ""


def _parse_sse_data(data_payload: str) -> dict[str, Any]:
    """解析 chat_events SSE 的 data 行（JSON 字符串）。"""
    if not data_payload:
        return {}
    try:
        import json

        return json.loads(data_payload)
    except Exception:  # noqa: BLE001
        return {}


def make_processor(agent_main=None):
    """构造 MessageProcessor（async generator）。

    agent_main 可显式注入（便于测试）；默认懒加载 web_server.agent_main。
    """

    def _get_agent_main():
        if agent_main is not None:
            return agent_main
        try:
            import web_server

            return web_server.agent_main
        except Exception:  # noqa: BLE001
            return None

    async def processor(message) -> AsyncIterator[Any]:
        from octop_gateway.models import MessageEvent

        text = _extract_text(message)
        if not text.strip():
            return

        channel_type = str(getattr(message, "channel_type", "") or "unknown")
        subject = getattr(message, "channel_subject", None)
        subject_id = getattr(subject, "subject_id", None) or "anon"
        sid = session_id_for(channel_type, subject_id)

        ag = _get_agent_main()
        if ag is None:
            yield MessageEvent.error_event("BT agent_main 不可用")
            return

        # 启动后台对话任务（复用 BT 现有 agent 体系）
        try:
            start_res = ag.chat_start({
                "session_id": sid,
                "message": text,
                "mode": "claude",
            })
        except Exception as e:  # noqa: BLE001
            logger.exception("[gw-proc] chat_start 失败")
            yield MessageEvent.error_event(f"启动对话失败: {e}")
            return

        # 校验启动是否成功
        if isinstance(start_res, dict) and not start_res.get("status", True):
            yield MessageEvent.error_event(str(start_res.get("msg") or "启动对话失败"))
            return

        # 订阅事件流，把文本增量转发为 MessageEvent.delta
        yield MessageEvent.typing()
        try:
            async for sse in ag.chat_events({"session_id": sid}):
                data_payload = _extract_sse_data(sse)
                event = data_payload.get("event")
                data = data_payload.get("data")
                if event == "message" and isinstance(data, dict):
                    content = str(data.get("content") or data.get("text") or "")
                    if content:
                        yield MessageEvent.delta(content)
                elif event == "message_end":
                    yield MessageEvent.completed()
                    return
        except Exception as e:  # noqa: BLE001
            logger.exception("[gw-proc] chat_events 流中断")
            yield MessageEvent.error_event(f"对话流中断: {e}")

    return processor


def _extract_sse_data(sse: str) -> dict[str, Any]:
    """从 SSE 文本中提取 data 字段并解析 JSON。"""
    data_line = ""
    if isinstance(sse, str):
        for line in sse.splitlines():
            if line.startswith("data:"):
                data_line = line[5:].strip()
                break
    elif isinstance(sse, dict):
        # 某些封装直接返回 dict
        return sse
    return _parse_sse_data(data_line)