"""
@input: asyncio, logging, mimetypes, os, re, threading
@output: SendMedia 工具 — 向当前 IM 会话发送媒体文件（图片/视频/语音/文件）
@position: Tools layer — 统一出站媒体发送（覆盖 clawbot / gateway 所有通道）
@auto-doc: Update header and folder INDEX.md when this file changes

定位：这是 BT 的**统一媒体发送工具**。模型生成/找到媒体文件后，显式调用本工具
将文件投递到当前对话所在的 IM 通道（微信/Telegram/QQ/飞书/钉钉/企微/元宝/小一/MQTT/Discord）。

为什么不是自动推：模型截图/生图后，内部产物（AI 自己分析用的截图）和
「要发给用户的图」语义不同。只有模型显式调用 `SendMedia`，才意味着
它要把这个文件推送给对话方。避免误发内部产物、避免白名单永远列不全。

工具命名不带平台前缀（不像 ClawbotSendMedia）：本工具是跨通道的，
模型无需关心当前走的是哪个平台——工具自动从会话上下文定位通道并投递。

链路：
    AG 调 SendMedia(path=...)
      → _resolve_channel 根据 session_id 找到 channel_type + subject
      → 读文件 → base64
      → channel.reply_media(ChannelSubject(subject_id), ImageContent(data=base64))
      → octop-gateway channel._send_media → 真正发到微信/QQ/Telegram...

路径解析：
- 绝对路径直接读
- 相对路径按 workspace 目录解析（与 BT 其他工具一致）

媒体类型推断：
- 按文件扩展名自动识别 image/video/audio/file
- 无法识别时模型应显式传 media_type

安全：
- 文件不存在/非文件 → 错误提示
- 定时任务无会话上下文 → 必须显式传 channel_type/subject_id
- 只发当前会话，不跨账号
"""
import asyncio
import re
import logging
import mimetypes
import os
import threading

from . import PROJECT_ROOT, register_tool, _current_session_id
from .base import _xml_response

logger = logging.getLogger(__name__)

# 扩展名 → 媒体类型（显式覆盖，避免依赖系统 mime 表漏掉常见格式如 png/svg）
_MEDIA_EXTS: dict[str, str] = {
    # image
    "png": "image", "jpg": "image", "jpeg": "image", "gif": "image",
    "webp": "image", "bmp": "image", "svg": "image", "ico": "image",
    "avif": "image", "tif": "image", "tiff": "image",
    # video
    "mp4": "video", "webm": "video", "mov": "video", "mkv": "video",
    "avi": "video", "m4v": "video", "mpg": "video", "mpeg": "video",
    # audio
    "mp3": "audio", "wav": "audio", "ogg": "audio", "m4a": "audio",
    "flac": "audio", "aac": "audio", "wma": "audio", "opus": "audio",
}

_SESSION_ID_RE = re.compile(r"^gw_(\w+)_(.+)$")


def _guess_media_type(path: str) -> str:
    """根据文件扩展名推断媒体类型：image / video / audio / file。"""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    return _MEDIA_EXTS.get(ext, "file")


def _resolve_channel_from_session(session_id: str) -> dict | None:
    """从 session_id 解析出 channel_type + subject_id，并定位 channel_id。

    session_id 形如 gw_<channel_type>_<subject_id>（两下划线分隔，subject 可能含下划线）。
    返回 {"channel_type": str, "subject_id": str, "channel_id": str | None}。
    """
    import platform_ext.gateway as gw_mod
    m = _SESSION_ID_RE.match(session_id)
    if not m:
        return None
    channel_type = m.group(1)
    subject_id = m.group(2)
    # 找当前 session 对应的 channel_id（根据 kind 在 manager 的 _manager_kinds 里匹配）
    mgr = gw_mod._get_manager() if hasattr(gw_mod, "_get_manager") else None
    channel_id = None
    if mgr is not None:
        # _manager_kinds / _manager_kind_of 是模块级全局，路由到 channel_id
        for cid, kind in getattr(gw_mod, "_manager_kind_of", {}).items():
            if kind == channel_type:
                channel_id = cid
                break
    return {"channel_type": channel_type, "subject_id": subject_id, "channel_id": channel_id}


def _send_media_sync(path: str, caption: str, media_type: str,
                     channel_type: str, subject_id: str) -> str:
    """同步包装：把异步 reply_media 提交到当前事件循环（或创建新循环）。"""
    import platform_ext.gateway as gw_mod
    mgr = gw_mod._get_manager()
    if mgr is None:
        return _xml_response("error", "网关未初始化，请先在面板启动对应通道")
    # 定位 channel
    channel_id = None
    for cid, kind in getattr(gw_mod, "_manager_kind_of", {}).items():
        if kind == channel_type:
            channel_id = cid
            break
    if not channel_id:
        return _xml_response(
            "error",
            f"未找到 {channel_type} 通道实例（通道可能未启动）。"
            "请先在面板「📡 通道」启动 {channel_type}，再重试。"
        )
    channel = mgr.get_channel(channel_id)
    if channel is None:
        return _xml_response("error", f"通道实例获取失败: channel_id={channel_id}")
    # 构造 subject
    from octop_gateway.models import ChannelSubject, ImageContent, VideoContent, AudioContent, FileContent
    subject = ChannelSubject(
        subject_id=subject_id,
        chat_type="direct" if "@" not in subject_id else "group",
    )
    # 读文件转 base64
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    kind = media_type or _guess_media_type(path)
    try:
        with open(path, "rb") as f:
            data_b64 = __import__("base64").b64encode(f.read()).decode("ascii")
    except OSError as e:
        return _xml_response("error", f"读取文件失败: {e}")
    # 构造 ContentPart（按类型）
    common = {"data": data_b64, "mime_type": mime, "local_path": None,
              "url": "", "filename": os.path.basename(path)}
    if kind == "image":
        part = ImageContent(alt_text=caption or os.path.basename(path), **common)
    elif kind == "video":
        part = VideoContent(**common)
    elif kind == "audio":
        part = AudioContent(**common)
    else:
        part = FileContent(filename=os.path.basename(path), **common)
    # 异步发送（跨线程）
    loop = getattr(mgr, "_loop", None)
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(channel.reply_media(subject, part), loop)
        try:
            future.result(timeout=60)
        except Exception as e:  # noqa: BLE001
            return _xml_response("error", f"发送失败: {e}")
    else:
        # 无事件循环（channel 未启动）→ 直接同步调（channel 内部用 asyncio.run）
        try:
            asyncio.run(channel.reply_media(subject, part))
        except Exception as e:  # noqa: BLE001
            return _xml_response("error", f"发送失败: {e}")
    return _xml_response(
        "success",
        f"已发送到 {channel_type} 会话 {subject_id}：{os.path.basename(path)}",
        {"channel_type": channel_type, "subject_id": subject_id, "filename": os.path.basename(path)},
    )


@register_tool(category="媒体", name_cn="发送媒体", risk_level="medium")
def SendMedia(path: str, caption: str | None = None,
              media_type: str | None = None) -> str:
    """
    - 向当前 IM 会话发送媒体文件（图片 / 视频 / 音频 / 文件）
    - 适用：AG 生成图片/视频/音频或找到文件后，要推送给对话方

    Args:
        path: 待发送文件的**绝对路径**（相对路径按 workspace 解析）。
        caption: 附带说明文字（发图片/视频/文件时额外发文本）。
        media_type: 媒体类型，留空按扩展名自动推断。可选 image / video / audio / file。
            无法识别扩展名时必须显式指定（如 .mp3 之外格式）。

    Returns:
        发送结果：成功含文件名与目标通道；失败给出明确原因（通道未启动/文件不存在等）。
    """
    p = (path or "").strip()
    if not p:
        return _xml_response("error", "缺少参数 path")
    # 相对路径解析
    if not os.path.isabs(p):
        try:
            from ...config import Config
            base = str(getattr(Config, "workspace", "") or PROJECT_ROOT)
        except Exception:
            base = PROJECT_ROOT
        p = os.path.join(base, p)
    if not os.path.exists(p):
        return _xml_response("error", f"文件不存在: {p}")
    if not os.path.isfile(p):
        return _xml_response("error", f"不是文件（可能是目录）: {p}")
    # 定位通道（从当前会话上下文）
    sid = (_current_session_id() or "").strip()
    if not sid:
        return _xml_response(
            "error",
            "无法定位目标通道：非对话环境（如定时任务）请显式传入 channel_type 和 subject_id。"
        )
    info = _resolve_channel_from_session(sid)
    if info is None or not info["channel_id"]:
        return _xml_response(
            "error",
            f"会话 {sid} 不属于任何已启动的通道。"
            "请先在面板「📡 通道」启动对应平台，再重试。"
        )
    # 分派到同步发送（在工具线程内完成，避免阻塞事件循环）
    return _send_media_sync(
        path=p,
        caption=(caption or "").strip(),
        media_type=(media_type or "").strip().lower(),
        channel_type=info["channel_type"],
        subject_id=info["subject_id"],
    )
