"""Webhook IM 通道（作为 octop-gateway 的一个自定义 BaseChannel）。

把 webhook-api（一个独立的 IM 消息平台）接入 BT 的统一 gateway 通讯器体系，
从而和微信/飞书/Telegram 等一样：在「通讯器」前端面板配置 base_url / api_key /
name，并可启停、可自动启动、配置存 platform_ext.gateway.webhook。

数据流（与 SDK 其它 channel 完全一致的桥接路径）：
  收：start() 起 httpx 轮询任务 → GET /webhook/outbound?name=&direction=inbound&since=
      → parse_inbound() 归一化 → self.enqueue() → ChannelManager worker
      → gateway_processor → BT agent 对话
  发：agent 回复 → _send_text/_send_media → POST /webhook/outbound/{name}
      （媒体走 multipart 的 media 字段），供 webhook 订阅方消费。

不依赖任何平台 SDK（webhook 只是 HTTP），因此不需要 _RUNTIME_DEPS 安装步骤。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

from octop_gateway.channel import BaseChannel, ChannelConfig
from octop_gateway.constraints import ChannelConstraints
from octop_gateway.models import (
    ChannelSubject,
    FileContent,
    ImageContent,
    InboundMessage,
    TextContent,
)

logger = logging.getLogger("gateway-webhook")


@dataclass
class WebhookConfig(ChannelConfig):
    """webhook 通讯器配置（字段与前端 _PLATFORM_META.config_fields 对齐）。"""

    base_url: str = "http://127.0.0.1:8100"
    api_key: str = ""
    channel_name: str = "default"
    poll_interval: float = 2.0
    timeout: float = 10.0
    listen_direction: str = "inbound"

    required_credentials = ("channel_name",)

    @property
    def name(self) -> str:
        """webhook 侧的通道标识（别名，供通道内部使用）。"""
        return self.channel_name or "default"

    def __post_init__(self) -> None:
        # 前端 number 字段可能以字符串传入，统一转数值，避免 wait_for(timeout=str) 报错。
        for attr in ("poll_interval", "timeout"):
            try:
                setattr(self, attr, float(getattr(self, attr)))
            except (TypeError, ValueError):
                setattr(self, attr, 2.0 if attr == "poll_interval" else 10.0)


class WebhookChannel(BaseChannel):
    """把 webhook-api 当作一个 IM 平台接入统一 ChannelManager。"""

    channel_type = "webhook"

    def __init__(
        self,
        processor,
        *,
        config: WebhookConfig,
        channel_id: str | None = None,
        tenant_id: str | None = None,
        debounce_seconds: float = 0.0,
        constraints: ChannelConstraints | None = None,
    ) -> None:
        super().__init__(
            processor,
            channel_id=channel_id,
            tenant_id=tenant_id,
            debounce_seconds=debounce_seconds,
            constraints=constraints,
            config=config,
        )
        self._config = config
        self._task: asyncio.Task | None = None
        self._stop_evt: asyncio.Event | None = None
        self._client: Any = None  # httpx.AsyncClient
        self._cursor: float | None = None
        self._seen_ids: set[str] = set()

    def _default_constraints(self) -> ChannelConstraints:
        return ChannelConstraints(
            send_rate_limit=(60, 60.0),
            show_thinking=False,
            show_tool_hints=True,
        )

    # ---- HTTP 辅助 ----
    def _base(self) -> str:
        return (self._config.base_url or "http://127.0.0.1:8100").rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._config.api_key}"} if self._config.api_key else {}

    def _abs_url(self, url: str | None) -> str:
        """把 webhook 的相对 media_url (/uploads/xxx) 用配置的 base_url 补成绝对地址。

        base_url 即 adapters 的地址，下载、展示、发送统一用它补全。
        把它配成大家都能访问的地址（内网 IP 或反代域名）即可。
        """
        if not url:
            return ""
        if url.startswith(("http://", "https://")):
            return url
        return self._base() + (url if url.startswith("/") else "/" + url)

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(
                base_url=self._base(), headers=self._headers(), timeout=self._config.timeout
            )
        return self._client

    async def fetch_remote_media(self, url: str) -> tuple[bytes, str]:
        """下载 webhook 侧媒体：补全相对 url + 带 api_key 鉴权。

        webhook 的 /uploads/xxx 通常需要与读写端点相同的 Bearer 鉴权，
        且是相对路径；直接用 SDK 默认的 aiohttp 裸 GET 会失败。
        """
        client = await self._ensure_client()
        resp = await client.get(self._abs_url(url))
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "").split(";")[0].strip()
        if not ctype:
            import mimetypes
            ctype = mimetypes.guess_type(url)[0] or "application/octet-stream"
        return resp.content, ctype

    # ---- 生命周期 ----
    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        if not self._config.name:
            raise RuntimeError("WebhookChannel: name is required")
        # cursor 是内存态，进程重启会丢。若不在启动时初始化，第一轮轮询将不带 since，
        # 服务端 /webhook/outbound 会返回最近 50 条历史消息当成新消息全部回放（刷屏）。
        # 方案 A：只处理启动之后的新消息，不回放停机前的历史。
        if self._cursor is None:
            self._cursor = time.time()
        self._stop_evt = asyncio.Event()
        await self._ensure_client()
        self._task = asyncio.create_task(self._poll_loop(), name=f"webhook-channel-{self.channel_id}")
        logger.info(
            "WebhookChannel started: name=%s base=%s direction=%s",
            self._config.name, self._base(), self._config.listen_direction,
        )

    async def stop(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        logger.info("WebhookChannel stopped: name=%s", self._config.name)

    # ---- 收：轮询 webhook 入站消息 ----
    async def _poll_loop(self) -> None:
        assert self._stop_evt is not None
        while not self._stop_evt.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("WebhookChannel poll error: %s", self._config.name)
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=self._config.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        client = await self._ensure_client()
        params: dict[str, Any] = {
            "name": self._config.name,
            "direction": self._config.listen_direction,
            "limit": 50,
        }
        if self._cursor is not None:
            params["since"] = self._cursor
        resp = await client.get("/webhook/outbound", params=params)
        resp.raise_for_status()
        msgs = (resp.json() or {}).get("messages") or []
        for m in msgs:
            mid = str(m.get("id") or "")
            if not mid or mid in self._seen_ids:
                continue
            self._seen_ids.add(mid)
            ts = m.get("timestamp")
            if isinstance(ts, (int, float)):
                self._cursor = max(self._cursor or 0.0, float(ts)) + 0.000001
            self.enqueue(self.parse_inbound(m))
        if len(self._seen_ids) > 2000:
            self._seen_ids = set(list(self._seen_ids)[-1000:])

    def parse_inbound(self, raw_payload: Any) -> InboundMessage:
        # ChannelManager worker 在 handle_inbound 前已把 raw dict 解析过一次，
        # 传入的是 InboundMessage。若在此再次当 dict 解析会得到空消息，
        # 导致 processor 因「无文本/无图/无附件」直接 return，消息永远进不了 agent。
        # 与内置 mqtt/discord channel 保持一致：已解析的 InboundMessage 原样返回。
        if isinstance(raw_payload, InboundMessage):
            return raw_payload
        data = raw_payload if isinstance(raw_payload, dict) else {}
        meta = dict(data.get("metadata") or {})
        meta.setdefault("webhook_id", str(data.get("id") or ""))
        meta.setdefault("webhook_type", data.get("type"))
        # webhook 的 media_url 是 /uploads/xxx 相对路径，用配置的 base_url 补成绝对地址。
        raw_media_url = data.get("media_url")
        media_url = self._abs_url(raw_media_url) if raw_media_url else ""
        if media_url:
            meta["webhook_media_url"] = media_url
        user_key = str(meta.get("user_key") or data.get("name") or self._config.name)

        content: list[Any] = []
        text = str(data.get("content") or "")
        msg_type = str(data.get("type") or "text")
        if media_url:
            if msg_type == "image":
                content.append(ImageContent(url=media_url))
            else:
                # file/video/audio 统一用 FileContent（webhook 只存 url + 类型）
                content.append(FileContent(url=media_url, mime_type=msg_type))
        if text:
            content.append(TextContent(text=text))
        if not content:
            content.append(TextContent(text=""))

        return InboundMessage(
            channel_id=self.channel_id,
            channel_type=self.channel_type,
            tenant_id=self._tenant_id,
            channel_subject=ChannelSubject(
                subject_id=user_key,
                chat_type=str(meta.get("chat_type") or "direct"),
                metadata={**meta, "name": self._config.name},
            ),
            channel_session_id=self._config.name,
            content=content,
            metadata=meta,
        )

    # ---- 发：agent 回复写回 webhook ----
    async def _send_text(self, subject: ChannelSubject, text: str) -> None:
        if not text.strip():
            return
        client = await self._ensure_client()
        payload = {
            "type": "text",
            "content": text,
            "metadata": {
                "user_key": subject.subject_id,
                "name": self._config.name,
                "channel_id": self.channel_id,
            },
        }
        await client.post(f"/webhook/outbound/{self._config.name}", json=payload)

    async def _send_media(self, subject: ChannelSubject, media: Any) -> None:
        client = await self._ensure_client()
        name = self._config.name
        meta = {
            "user_key": subject.subject_id,
            "name": name,
            "channel_id": self.channel_id,
        }
        # 优先用 url（webhook 的 media_url），避免重复上传字节。
        url = getattr(media, "url", None)
        mime = str(getattr(media, "mime_type", "") or "").lower()
        mtype_val = str(getattr(media, "type", "") or "")
        # 类型判定：显式 type > mime_type 前缀 > 类名。FileContent 也可能装图片
        # （如 base64 图片），必须靠 mime 嗅探，否则会被当成 file。
        if mtype_val in ("image", "video", "audio", "file"):
            mtype = mtype_val
            if mtype == "file" and mime:
                if mime.startswith("image/"):
                    mtype = "image"
                elif mime.startswith("video/"):
                    mtype = "video"
                elif mime.startswith("audio/"):
                    mtype = "audio"
        elif isinstance(media, ImageContent):
            mtype = "image"
        elif mime.startswith("image/"):
            mtype = "image"
        elif mime.startswith("video/"):
            mtype = "video"
        elif mime.startswith("audio/"):
            mtype = "audio"
        else:
            mtype = "file"
        if url:
            # 相对 /uploads/xxx 补成绝对地址，下游订阅方才能下载。
            from urllib.parse import urlparse
            full_url = self._abs_url(str(url))
            fname = getattr(media, "filename", None) or os.path.basename(urlparse(full_url).path) \
                or f"media-{uuid.uuid4().hex[:8]}"
            payload = {"type": mtype, "media_url": full_url,
                       "caption": fname, "metadata": {**meta, "filename": fname}}
            await client.post(f"/webhook/outbound/{name}", json=payload)
            return
        # 无直链：读字节走 multipart media 字段上传。
        try:
            raw, ctype = await self.load_media_bytes(media)
        except Exception:  # noqa: BLE001
            logger.exception("WebhookChannel load_media_bytes failed")
            return
        filename = getattr(media, "filename", None) or f"media-{uuid.uuid4().hex[:8]}"
        files = {"media": (filename, raw, ctype or mime or "application/octet-stream")}
        data = {"type": mtype, "caption": filename,
                "metadata": json.dumps(meta, ensure_ascii=False)}
        await client.post(f"/webhook/outbound/{name}", data=data, files=files)

    async def _send_content(self, subject: ChannelSubject, parts: list[Any]) -> None:
        for part in parts:
            if isinstance(part, TextContent):
                await self._send_text(subject, part.text)
            else:
                await self._send_media(subject, part)


def build_webhook_channel(processor, *, config: dict[str, Any], channel_id: str | None = None):
    """工厂：由前端保存的扁平配置 dict 构造 WebhookChannel 实例。"""
    cfg = WebhookConfig.from_dict(config)
    return WebhookChannel(processor, config=cfg, channel_id=channel_id)
