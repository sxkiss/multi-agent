"""可插拔通道抽象（对齐 Octop gateway.channels）。

BT 现有通道（微信小程序、clawbot、HTTP/SSE、OpenAI 兼容）是散落的。
本模块引入一个轻量统一抽象：
- BaseChannel：通道接口（channel_type / handle_inbound / send_text / send_media / start / stop）
- ChannelRegistry：按 channel_id 注册/查找通道
- 内置 HTTP 通道：接受 POST 入站 JSON，回调 agent 处理
- 内置 WebSocket 通道：转发 Dashboard 聊天（复用 BT 现有 SSE 会话）

不改变 BT 现有通道行为，仅提供可插拔注册表 + 新通道类型。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger("platform-ext-channels")

# 通道类型常量
CHANNEL_HTTP = "http"
CHANNEL_WEBSOCKET = "websocket"
CHANNEL_MINIPROGRAM = "miniprogram"   # 注册表保留，行为仍由 BT 既有代码处理
CHANNEL_CLAWBOT = "clawbot"           # 注册表保留


# ── 消息模型 ───────────────────────────────────────────────
@dataclass
class InboundMessage:
    channel_id: str
    user_key: str            # 会话归属（如 wx_xxx / card_xxx）
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    payload: Any = None      # 原始入站负载


@dataclass
class ContentPart:
    kind: str              # text / image / file / audio / video
    data: str = ""         # 文本内容或 base64
    mime_type: str = ""
    filename: str = ""
    alt_text: str = ""


# ── 通道接口 ───────────────────────────────────────────────
@runtime_checkable
class Channel(Protocol):
    channel_type: str
    channel_id: str

    async def handle_inbound(self, message: InboundMessage) -> None: ...
    async def send_text(self, user_key: str, text: str, metadata: dict[str, Any] | None = None) -> None: ...
    async def send_media(self, user_key: str, parts: list[ContentPart], metadata: dict[str, Any] | None = None) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


# ── 入站处理器：把用户消息交给 BT agent ─────────────────────
class _BTAgentBridge:
    """把 InboundMessage 转成 BT 现有 agent 可处理的格式，并回发输出。

    BT 的 SSE/HTTP 会话已处理微信小程序/clawbot；这里为通用通道提供
    一个「文本进 → agent SSE 出 → 文本回」的最小桥，供自定义通道复用。
    """

    def __init__(self, *, process_text: Callable[[str, dict[str, Any]], str] | None = None):
        self._process_text = process_text

    async def process(self, text: str, metadata: dict[str, Any]) -> str:
        if self._process_text is None:
            return f"(echo) {text}"
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._process_text, text, metadata)


# ── 内置 HTTP 通道 ─────────────────────────────────────────
class HttpChannel:
    channel_type = CHANNEL_HTTP

    def __init__(self, channel_id: str, *, processor: _BTAgentBridge | None = None,
                 reply: Callable[[str, str], None] | None = None) -> None:
        self.channel_id = channel_id
        self._processor = processor or _BTAgentBridge()
        self._reply = reply

    async def handle_inbound(self, message: InboundMessage) -> None:
        out = await self._processor.process(message.text, message.metadata)
        if self._reply:
            self._reply(message.user_key, out)

    async def send_text(self, user_key: str, text: str, metadata=None) -> None:
        logger.info("[channel:%s] -> %s: %s", self.channel_id, user_key, text[:200])

    async def send_media(self, user_key: str, parts, metadata=None) -> None:
        logger.info("[channel:%s] -> %s: %d media part(s)", self.channel_id, user_key, len(parts))

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return


# ── 内置 WebSocket 通道 ────────────────────────────────────
class WebSocketChannel:
    channel_type = CHANNEL_WEBSOCKET

    def __init__(self, channel_id: str, *, processor: _BTAgentBridge | None = None) -> None:
        self.channel_id = channel_id
        self._processor = processor or _BTAgentBridge()

    async def handle_inbound(self, message: InboundMessage) -> None:
        # WebSocket 入站同样走 agent 桥
        await self._processor.process(message.text, message.metadata)

    async def send_text(self, user_key: str, text: str, metadata=None) -> None:
        # 实际推送由 BT 的 SSE/WS hub 负责；此处仅记录
        logger.info("[ws:%s] -> %s: %s", self.channel_id, user_key, text[:200])

    async def send_media(self, user_key: str, parts, metadata=None) -> None:
        logger.info("[ws:%s] -> %s: %d media part(s)", self.channel_id, user_key, len(parts))

    async def start(self) -> None:
        return

    async def stop(self) -> None:
        return


# ── 注册表 ─────────────────────────────────────────────────
class ChannelRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, Channel] = {}
        self._builtin_handlers: dict[str, Callable] = {}

    def register(self, channel: Channel) -> None:
        self._channels[channel.channel_id] = channel
        logger.info("[channels] 注册通道: %s (%s)", channel.channel_id, getattr(channel, "channel_type", "?"))

    def register_builtin(self, channel_type: str, handler: Callable) -> None:
        """注册 BT 既有内置通道（miniprogram/clawbot）的处理器。"""
        self._builtin_handlers[channel_type] = handler

    def get(self, channel_id: str) -> Channel | None:
        return self._channels.get(channel_id)

    def get_by_type(self, channel_type: str) -> list[Channel]:
        return [c for c in self._channels.values() if getattr(c, "channel_type", "") == channel_type]

    def list(self) -> list[dict[str, Any]]:
        out = []
        for cid, ch in self._channels.items():
            out.append({"channel_id": cid, "channel_type": getattr(ch, "channel_type", "?")})
        for ctype in self._builtin_handlers:
            if ctype not in {getattr(c, "channel_type", "") for c in self._channels.values()}:
                out.append({"channel_id": f"builtin:{ctype}", "channel_type": ctype, "builtin": True})
        return out

    def list_types(self) -> list[str]:
        types = {getattr(c, "channel_type", "") for c in self._channels.values()}
        types.update(self._builtin_handlers.keys())
        return sorted(types)

    async def dispatch(self, channel_id: str, message: InboundMessage) -> None:
        ch = self._channels.get(channel_id)
        if ch is None:
            raise KeyError(f"unknown channel: {channel_id}")
        await ch.handle_inbound(message)

    async def dispatch_text(self, channel_id: str, user_key: str, text: str, metadata=None) -> None:
        await self.dispatch(channel_id, InboundMessage(channel_id=channel_id, user_key=user_key, text=text, metadata=metadata or {}))


_registry = ChannelRegistry()


def get_registry() -> ChannelRegistry:
    return _registry