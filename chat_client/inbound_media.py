"""
@input: base64, hashlib, mimetypes, os, time, typing
@position: Gateway layer — 微信/渠道入站媒体（图片/文件）落盘 inbound/，供 agent 引用
@auto-doc: Update header and folder INDEX.md when this file changes

的轻量版。

bt 此前入站图片只转 base64 传上游，**未落盘**——agent 无法用 Read 等工具
二次处理（如 OCR、裁剪、存档）。本模块：

- save_inbound_media(...)：把入站媒体落 `workspace/inbound/<session>/`
- 尺寸/类型校验（防超大文件、防非法扩展名）
- 返回落盘路径 + 元数据，供 agent 在提示中引用
"""
import base64
import hashlib
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

INBOUND_DIRNAME = "inbound"
MAX_BYTES = 20 * 1024 * 1024  # 20MB
ALLOWED_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".json",
    ".mp3", ".wav", ".ogg", ".mp4", ".mov",
}


def _safe_session(session_id: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9_.\-]", "_", str(session_id or "default"))
    return s.strip("_") or "default"


def inbound_dir(base_dir: str, session_id: str = "") -> str:
    d = os.path.join(base_dir, "workspace", INBOUND_DIRNAME)
    if session_id:
        d = os.path.join(d, _safe_session(session_id))
    return d


def save_inbound_media(data: bytes | str, *, base_dir: str, session_id: str,
                       ext: str = "", filename: str = "") -> dict[str, Any]:
    """落盘入站媒体。

    Args:
        data: 字节，或 base64 字符串
        ext: 扩展名（含点，如 ".png"）；留空则从 filename 推断
        filename: 原始文件名（可选）

    Returns:
        {ok, path, bytes, ext, session} 或 {ok: False, error}
    """
    try:
        if isinstance(data, str):
            raw = base64.b64decode(data, validate=False)
        else:
            raw = bytes(data)
    except Exception as e:
        return {"ok": False, "error": f"解码失败: {e!s}"}

    if not raw:
        return {"ok": False, "error": "空内容"}
    if len(raw) > MAX_BYTES:
        return {"ok": False, "error": f"超过 {MAX_BYTES // (1024*1024)}MB 上限"}

    e = (ext or os.path.splitext(filename or "")[1] or "").lower()
    if e and e not in ALLOWED_EXT:
        return {"ok": False, "error": f"不允许的扩展名: {e}"}
    if not e:
        e = ".bin"

    d = inbound_dir(base_dir, session_id)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as err:
        return {"ok": False, "error": f"创建目录失败: {err}"}

    # 文件名：时间戳 + 内容哈希前 8 位（去重友好）
    h = hashlib.sha1(raw).hexdigest()[:8]
    name = f"{int(time.time())}_{h}{e}"
    path = os.path.join(d, name)
    try:
        with open(path, "wb") as f:
            f.write(raw)
    except OSError as err:
        return {"ok": False, "error": f"写入失败: {err}"}

    logger.info("[Inbound] 入站媒体落盘: %s (%d bytes)", path, len(raw))
    return {"ok": True, "path": path, "bytes": len(raw), "ext": e,
            "session": session_id, "rel": os.path.relpath(path, base_dir)}
