"""文件浏览 / 上传 / 下载路由。

从 web_server.py 抽出的第一个按域拆分模块（优-1 第一阶段）。只负责
``/api/files/*``，避免继续把文件能力、聊天、备份等职责堆在单文件里。
"""
from __future__ import annotations

import logging
import os
import time
import urllib.parse

from fastapi import File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import public

logger = logging.getLogger(__name__)

_BASE_DIR = ""
_FILE_BROWSE_ROOTS: list[str] = []
_SERVE_ROOTS: list[str] = []


def get_serve_roots() -> list[str]:
    """供其他模块（如多模态图片校验）复用的文件白名单。"""
    return list(_SERVE_ROOTS)


def _safe_resolve(requested: str):
    if not requested:
        return _FILE_BROWSE_ROOTS[0]
    req = os.path.realpath(os.path.expanduser(requested))
    for root in _FILE_BROWSE_ROOTS:
        r = os.path.realpath(root)
        if req == r or req.startswith(r + os.sep):
            return req
    return None


def _safe_resolve_serve(requested: str):
    if not requested:
        return None
    req = os.path.realpath(os.path.expanduser(requested))
    for root in _SERVE_ROOTS:
        r = os.path.realpath(root)
        if req == r or req.startswith(r + os.sep):
            return req
    return None


def register_file_routes(app, base_dir: str) -> None:
    global _BASE_DIR, _FILE_BROWSE_ROOTS, _SERVE_ROOTS
    _BASE_DIR = base_dir
    _FILE_BROWSE_ROOTS = [
        os.path.abspath(os.path.join(base_dir, "workspace")),
        os.path.abspath(os.path.expanduser("~")),
        "/tmp",
    ]
    _SERVE_ROOTS = _FILE_BROWSE_ROOTS + [
        os.path.abspath(os.path.join(base_dir, "uploads"))
    ]

    @app.get("/api/files/raw")
    async def api_files_raw(path: str = ""):
        """安全提供文件内容（图片/视频/音频/文档预览下载）。"""
        if not path:
            return JSONResponse(public.returnMsg(False, "缺少 path"))
        cand = path if os.path.isabs(path) else os.path.abspath(os.path.join(_BASE_DIR, path))
        real = _safe_resolve_serve(cand)
        if real is None or not os.path.isfile(real):
            return JSONResponse(public.returnMsg(False, "文件不存在或不在允许访问的范围内"))
        try:
            return FileResponse(real)
        except Exception:
            logger.warning("读取文件失败（路径不返回客户端）", exc_info=True)
            return JSONResponse(public.returnMsg(False, "读取文件失败"))

    @app.get("/api/files/browse")
    async def api_files_browse(path: str = ""):
        target = _safe_resolve(path)
        if target is None:
            return JSONResponse(public.returnMsg(False, "路径不在允许浏览的范围内"))
        if not os.path.isdir(target):
            parent = os.path.dirname(target)
            preselect = os.path.basename(target) if os.path.isfile(target) else ""
            target = parent if os.path.isdir(parent) else _FILE_BROWSE_ROOTS[0]
        else:
            preselect = ""
        try:
            entries = []
            for name in sorted(os.listdir(target)):
                if name.startswith("."):
                    continue
                full = os.path.join(target, name)
                try:
                    st = os.stat(full)
                except Exception:
                    logger.warning("读取目录项失败，已跳过", exc_info=True)
                    continue
                is_dir = os.path.isdir(full)
                entries.append({
                    "name": name,
                    "type": "dir" if is_dir else "file",
                    "size": 0 if is_dir else st.st_size,
                    "mtime": int(st.st_mtime),
                })
            entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
            parent = os.path.dirname(target)
            return JSONResponse(public.return_data(True, data={
                "path": target,
                "parent": parent if parent != target else "",
                "preselect": preselect,
                "items": entries,
            }))
        except Exception:
            logger.warning("浏览目录失败（详情不返回客户端）", exc_info=True)
            return JSONResponse(public.returnMsg(False, "浏览失败"))

    @app.post("/api/files/upload")
    async def api_files_upload(file: UploadFile = File(...)):
        max_upload_size = int(os.environ.get("BT_MAX_UPLOAD_SIZE_MB", "100")) * 1024 * 1024
        upload_dir = os.path.join(_BASE_DIR, "uploads")
        try:
            os.makedirs(upload_dir, exist_ok=True)
        except Exception:
            logger.warning("创建上传目录失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "无法创建上传目录"))
        fname = os.path.basename(file.filename or "upload.bin")
        dest = os.path.join(upload_dir, fname)
        if os.path.exists(dest):
            base, ext = os.path.splitext(fname)
            dest = os.path.join(upload_dir, f"{base}_{int(time.time())}{ext}")
        content_length = file.size
        if content_length is not None and content_length > max_upload_size:
            return JSONResponse(public.returnMsg(
                False, f"文件大小超过限制（{max_upload_size // 1024 // 1024}MB）"
            ))
        try:
            size = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = await file.read(8192)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_upload_size:
                        os.remove(dest)
                        return JSONResponse(public.returnMsg(
                            False,
                            f"文件大小超过限制（{max_upload_size // 1024 // 1024}MB）",
                        ))
                    out.write(chunk)
            rel = os.path.relpath(dest, _BASE_DIR)
            return JSONResponse(public.return_data(True, data={
                "name": fname,
                "path": rel,
                "url": "/api/files/raw?path=" + urllib.parse.quote(rel),
                "size": size,
            }))
        except Exception:
            logger.warning("上传文件失败（详情不返回客户端）", exc_info=True)
            return JSONResponse(public.returnMsg(False, "上传失败"))
