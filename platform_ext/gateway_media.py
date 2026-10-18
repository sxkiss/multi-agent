"""通道富媒体桥接：从 BT 工具结果中提取媒体文件，构造 octop-gateway ContentPart。

Octop 的做法（src/octop/infra/gateway/media/tool_media.py）：
- 只处理白名单工具（send_file / generate_image / desktop_screenshot / ...）的 tool_result；
- 从 tool_result 的文本/block 里提取媒体文件路径；
- 读文件转 base64，构造 ImageContent / VideoContent / AudioContent / FileContent；
- yield MessageEvent(type=MESSAGE, content=[part])，由 octop-gateway channel
  _deliver_event → reply_content → _send_media 真正发到微信/QQ/Telegram 等平台。

BT 的工具结果默认是 XML 文本（_xml_response），没有结构化媒体 block。因此这里
从文本里用正则识别媒体文件路径（markdown 图片、saved to / written to、outbound 目录）。

白名单工具名按「后缀」匹配（工具名如 BrowserScreenshot / generate_image / send_file 等）。
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from typing import Any

logger = logging.getLogger("platform-ext-gateway-media")

# 允许把工具结果作为媒体推送的工具名后缀（匹配 tool_result 的 tool 字段，忽略大小写）。
# 这些工具执行后通常会生成图片/文件，适合直接发给用户。
MEDIA_PUSH_TOOL_SUFFIXES: frozenset[str] = frozenset(
    {
        "send_file",
        "send_file_to_user",
        "desktop_screenshot",
        "mobile_screenshot",
        "screenshot",
        "generate_image",
        "generate_video",
        "generate_audio",
        "image",
        # BT 内置浏览器工具：screenshot action 返回的图片路径靠下方路径提取兜底，
        # 非截图 action（navigate/click/type）结果无图片路径，自动不产出事件。
        "browser_use",
    }
)

# markdown 图片/媒体语法：![alt](path) 或 [alt](path)
_MD_MEDIA_RE = re.compile(
    r"!?\[[^\]]*\]\(\s*([^)\s]+\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf|docx?|xlsx?|pptx?))\s*\)",
    re.IGNORECASE,
)

# 文本中的 "saved to /path/x.png" / "written to /path/x.png" / "file path: /path/x.png"
_SAVED_PATH_RE = re.compile(
    r"(?:saved to|written to|file(?:\s+path)?[:\s]+|output(?: file)?[:\s]+)\s*"
    r"([^\s<>\"']+\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf|docx?|xlsx?|pptx?))",
    re.IGNORECASE,
)

# 任意 outbound 目录下的媒体文件绝对/相对路径
_OUTBOUND_MEDIA_RE = re.compile(
    r"(?<![^\s\"'()])([^\s\"'()]+[/\\]outbound[/\\][^\s\"'()]+\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf|docx?|xlsx?|pptx?))",
    re.IGNORECASE,
)

# 通用绝对路径媒体文件（兜底，避免误抓文本里的普通单词）
_ABS_MEDIA_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:/[A-Za-z0-9_./@~-]+)+\.(?:png|jpe?g|gif|webp|bmp|svg|mp4|webm|mov|mp3|wav|ogg|m4a|pdf|docx?|xlsx?|pptx?))",
    re.IGNORECASE,
)

# 工具名规范化：去掉前缀（如 "BrowserScreenshot" -> "browser_screenshot"？）
def tool_name_base(name: str | None) -> str:
    """去掉工具名可能的前缀路径（如 'mcp__foo' / 'MCP__server__tool'），取最后一段。"""
    if not name:
        return ""
    trimmed = str(name).strip()
    slash = max(trimmed.rfind("/"), trimmed.rfind("."))
    if slash >= 0:
        trimmed = trimmed[slash + 1 :]
    # 去掉可能的模块前缀：xx.xx.xxx 只保留最后一段（如 MCP 工具名）
    return trimmed


def is_media_push_tool(tool_name: str | None) -> bool:
    """工具名是否命中媒体推送白名单（后缀匹配，大小写不敏感）。"""
    base = tool_name_base(tool_name).lower()
    return any(base.endswith(suffix.lower()) for suffix in MEDIA_PUSH_TOOL_SUFFIXES)


def _kind_for_path(path: str, mime: str | None) -> str:
    """根据扩展名/ mime 推断媒体类型：image/video/audio/file。"""
    mime = (mime or "").lower()
    if mime.startswith("image/") or re.search(r"\.(png|jpe?g|gif|webp|bmp|svg)$", path, re.I):
        return "image"
    if mime.startswith("video/") or re.search(r"\.(mp4|webm|mov|m4v)$", path, re.I):
        return "video"
    if mime.startswith("audio/") or re.search(r"\.(mp3|wav|ogg|m4a|flac|aac)$", path, re.I):
        return "audio"
    return "file"


def _extract_paths(text: str) -> list[str]:
    """从工具结果文本中提取候选媒体文件路径（去重，保持顺序）。"""
    if not text:
        return []
    seen: list[str] = []
    seen_set: set[str] = set()

    def _add(p: str):
        p = p.strip().strip("\"'()")
        if not p or p in seen_set:
            return
        seen.append(p)
        seen_set.add(p)

    # 1) markdown 媒体语法
    for m in _MD_MEDIA_RE.finditer(text):
        _add(m.group(1))
    # 2) saved to / written to / file path:
    for m in _SAVED_PATH_RE.finditer(text):
        _add(m.group(1))
    # 3) outbound 目录路径
    for m in _OUTBOUND_MEDIA_RE.finditer(text):
        _add(m.group(1))
    # 4) 通用绝对路径
    for m in _ABS_MEDIA_RE.finditer(text):
        _add(m.group(1))
    return seen


def _resolve_abs_path(path: str, workspace: str | None = None) -> str | None:
    """把候选路径解析为绝对路径；相对路径按 workspace（或当前目录）尝试。"""
    p = os.path.expanduser(path)
    if os.path.isabs(p):
        return p if os.path.isfile(p) else None
    candidates = []
    if workspace:
        candidates.append(os.path.join(workspace, p))
    candidates.append(os.path.abspath(p))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def content_part_for_path(path: str, workspace: str | None = None) -> Any | None:
    """读取文件并构造 octop-gateway 的 ContentPart（ImageContent 等）。

    data 是 base64（load_media_bytes 的最高优先级来源），平台直接读取发送。
    """
    abs_path = _resolve_abs_path(path, workspace)
    if not abs_path:
        return None
    try:
        with open(abs_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        logger.warning("[gw-media] 读取媒体文件失败 %s: %s", abs_path, e)
        return None
    mime = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    kind = _kind_for_path(abs_path, mime)
    data_b64 = base64.b64encode(raw).decode("ascii")
    filename = os.path.basename(abs_path)

    # 延迟导入 octop_gateway.models，避免本模块在未安装 gateway 时 import 失败
    try:
        from octop_gateway.models import (
            AudioContent,
            FileContent,
            ImageContent,
            VideoContent,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[gw-media] 无法导入 octop_gateway.models: %s", e)
        return None

    common = {"data": data_b64, "mime_type": mime, "local_path": None}
    if kind == "image":
        return ImageContent(url="", alt_text=filename, **common)
    if kind == "video":
        return VideoContent(url="", **common)
    if kind == "audio":
        return AudioContent(url="", **common)
    return FileContent(url="", filename=filename, **common)


def media_events_from_tool_result(
    tool_name: str | None,
    result_text: str,
    workspace: str | None = None,
) -> list[Any]:
    """从单个 tool_result 提取媒体并构造 MessageEvent(MESSAGE) 列表。

    仅白名单工具的结果会被处理。返回空列表表示没有可推送的媒体。
    """
    if not is_media_push_tool(tool_name):
        return []
    paths = _extract_paths(result_text or "")
    if not paths:
        return []
    events: list[Any] = []
    for path in paths:
        part = content_part_for_path(path, workspace)
        if part is None:
            continue
        try:
            from octop_gateway.models import MessageEvent, MessageEventType

            events.append(
                MessageEvent(type=MessageEventType.MESSAGE, content=[part])
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[gw-media] 构造 MessageEvent 失败: %s", e)
            break
    return events