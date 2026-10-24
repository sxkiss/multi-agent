"""
@input: base64, io, json, logging, mimetypes, os
@output: ViewImage 工具 — 读取本地图片，返回元信息 + base64 data URL
@position: Tools layer — 图片/多模态，供模型直接查看图片内容
@auto-doc: Update header and folder INDEX.md when this file changes

设计说明：
- 模型需要查看本地图片时（截图、设计稿、测试报告图等），调用本工具。
- 返回图片元信息（格式/尺寸/大小）+ base64 data URL，多模态模型可直接读取。
- 单张图片大小限制 10MB，避免 base64 撑爆上下文。
- 纯文本模型也可用：返回的元信息 + 可选 base64（后续可扩展 OCR）。
"""

import base64
import logging
import mimetypes
import os

from PIL import Image

from . import PROJECT_ROOT, register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

# 单张图片大小上限：10MB（base64 后约 13.3MB，已足够绝大多数场景）
MAX_IMAGE_SIZE = 10 * 1024 * 1024

# 支持的图片扩展名
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico", ".tiff", ".tif",
}


def _safe_path(path: str) -> str:
    """解析图片路径：绝对路径直接用，相对路径按项目根目录解析。"""
    if not path:
        return ""
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p):
        p = os.path.join(PROJECT_ROOT, p)
    return os.path.realpath(os.path.abspath(p))


@register_tool(category="媒体", name_cn="查看图片", risk_level="low")
def ViewImage(path: str, return_base64: bool = False) -> str:
    """
    View a local image file and return its metadata plus an optional base64 data URL.

    Use this tool when you need to inspect an image on disk: screenshots, design
    mockups, test report images, photos, diagrams, etc. The model can directly
    "see" the image when the data URL is included (multimodal models).

    Args:
        path: Absolute path to the image file. Relative paths are resolved against
            the project root directory.
        return_base64: Whether to embed the base64 data URL directly in the text
            result. Default False: the tool returns compact metadata plus a
            Markdown image link, and the agent layer converts it into a real
            image_url content block for the vision model (so the model still
            "sees" the image without paying 260KB+ of base64 in the transcript).
            Set True only when you explicitly need the raw data URL inline.

    Returns:
        Image metadata (format, size, dimensions, mode) and, when requested,
        a data:image/...;base64,... URL for direct viewing.

    Limitations:
        - Maximum image size is 10MB.
        - Only common raster image formats are supported (png/jpg/jpeg/gif/bmp/webp/ico/tiff).
        - Sensitive files (e.g. SSH keys, /etc/shadow) are never readable.
    """
    real = _safe_path(path)
    if not real or not os.path.exists(real):
        return _xml_response("error", f"File not found: {path}")
    if not os.path.isfile(real):
        return _xml_response("error", f"Not a file: {path}")

    ext = os.path.splitext(real)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return _xml_response(
            "error",
            f"Unsupported image extension: {ext or '(none)'}. "
            f"Supported: {', '.join(sorted(IMAGE_EXTENSIONS))}",
        )

    # 敏感路径黑名单（与 Read 工具一致）
    SENSITIVE_PATHS = {"/etc/shadow", "/etc/sudoers", "/etc/gshadow", "/root/.ssh", "/etc/ssh"}
    for sp in SENSITIVE_PATHS:
        if real == sp or real.startswith(sp + os.sep):
            return _xml_response("error", f"Access denied: 敏感文件不允许读取 ({path})")

    try:
        file_size = os.path.getsize(real)
        if file_size > MAX_IMAGE_SIZE:
            return _xml_response(
                "error",
                f"Image too large: {file_size / 1024 / 1024:.1f}MB exceeds 10MB limit: {path}",
            )

        with Image.open(real) as img:
            fmt = img.format or ext.lstrip(".").upper()
            width, height = img.size
            mode = img.mode
            # 读取第一帧（gif 等多帧图只取概览）
            img.seek(0)

        mime = mimetypes.guess_type(real)[0] or f"image/{ext.lstrip('.').lower()}"
        meta = {
            "path": real,
            "format": fmt,
            "width": str(width),
            "height": str(height),
            "mode": mode,
            "size_bytes": str(file_size),
            "mime": mime,
        }

        if not return_base64:
            body = (
                f"<path>{real}</path>\n"
                f"<format>{fmt}</format>\n"
                f"<size>{width}x{height}</size>\n"
                f"<mode>{mode}</mode>\n"
                f"<file_size>{file_size} bytes</file_size>\n"
                f"![图片]({real})\n"
                "Image attached (rendered in UI). The model receives the image itself."
            )
            return _xml_response("done", body, metadata=meta)

        with open(real, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
        body = (
            f"<path>{real}</path>\n"
            f"<format>{fmt}</format>\n"
            f"<size>{width}x{height}</size>\n"
            f"<mode>{mode}</mode>\n"
            f"<file_size>{file_size} bytes</file_size>\n"
            f"![图片]({data_url})\n"
            "Image data URL is embedded above. You can see the image directly."
        )
        return _xml_response("done", body, metadata=meta)
    except Exception as e:
        logger.warning("ViewImage 读取图片失败: %s", e, exc_info=True)
        return _xml_response("error", f"读取图片失败: {e}")
