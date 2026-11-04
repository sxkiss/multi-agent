"""知识库 API（文档管线）。

优-1 第二阶段：从 web_server.py 按域拆出的知识库路由。
"""
from __future__ import annotations

import json
import logging
import os
import time

from fastapi import File, Request, UploadFile
from fastapi.responses import JSONResponse

import public

logger = logging.getLogger(__name__)

_BASE_DIR = ""


async def _params(request: Request) -> dict:
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("知识库请求参数解析失败，已忽略 body", exc_info=True)
    return params


def _kb_instance():
    """构造知识库实例（复用 config.json 的 embedding 段）。"""
    from chat_client.knowledge_base import KnowledgeBase, build_embed_fn
    cfg = {}
    cfg_path = os.path.join(_BASE_DIR, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            logger.warning("读取 config.json 失败", exc_info=True)
    emb = (cfg.get("embedding") or {}) if isinstance(cfg, dict) else {}
    embed = build_embed_fn(
        emb.get("embedding_api_key") or "",
        emb.get("embedding_base_url") or "",
        emb.get("embedding_model_name") or "text-embedding-3-small",
        default_headers=cfg.get("default_headers"),
    )
    kb_dir = os.path.join(_BASE_DIR, "knowledge")
    os.makedirs(kb_dir, exist_ok=True)
    return KnowledgeBase(kb_dir, embed)


def register_knowledge_routes(app, base_dir: str) -> None:
    global _BASE_DIR
    _BASE_DIR = base_dir

    @app.get("/api/knowledge/list")
    async def api_knowledge_list():
        """列出知识库全部文档 + 统计。"""
        try:
            kb = _kb_instance()
            return JSONResponse(public.return_data(True, data={
                "stats": kb.stats(),
                "documents": kb.list_documents(),
            }))
        except Exception:
            logger.warning("知识库列表失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "知识库列表失败"))

    @app.post("/api/knowledge/upload")
    async def api_knowledge_upload(file: UploadFile = File(...), ocr: bool = True, name: str = ""):
        """上传文档并入库（pdf/docx/pptx/xlsx/html/md/txt/图片，图片走 OCR）。"""
        max_kb_size = int(os.environ.get("BT_MAX_KB_SIZE_MB", "100")) * 1024 * 1024
        tmp_dir = os.path.join(_BASE_DIR, "uploads")
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except Exception:
            logger.warning("创建知识库上传目录失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "无法创建上传目录"))
        fname = os.path.basename(file.filename or "upload.bin")
        dest = os.path.join(tmp_dir, f"kb_{int(time.time())}_{fname}")
        try:
            size = 0
            with open(dest, "wb") as out:
                while True:
                    chunk = await file.read(8192)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_kb_size:
                        os.remove(dest)
                        return JSONResponse(public.returnMsg(
                            False,
                            f"文件超过限制（{max_kb_size // 1024 // 1024}MB）",
                        ))
                    out.write(chunk)
            kb = _kb_instance()
            result = kb.add_document(path=dest, name=name or fname, ocr=bool(ocr))
            if not result.get("status"):
                return JSONResponse(public.returnMsg(False, result.get("msg", "入库失败")))
            return JSONResponse(public.return_data(True, data=result))
        except Exception:
            logger.warning("知识库上传入库失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "知识库入库失败"))

    @app.post("/api/knowledge/add_text")
    async def api_knowledge_add_text(request: Request):
        """直接入库一段文本。body: {text, name?}"""
        try:
            params = await _params(request)
            text = (params.get("text") or "").strip()
            if not text:
                return JSONResponse(public.returnMsg(False, "text 不能为空"))
            kb = _kb_instance()
            result = kb.add_document(text=text, name=params.get("name") or "")
            if not result.get("status"):
                return JSONResponse(public.returnMsg(False, result.get("msg", "入库失败")))
            return JSONResponse(public.return_data(True, data=result))
        except Exception:
            logger.warning("知识库文本入库失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "文本入库失败"))

    @app.post("/api/knowledge/search")
    async def api_knowledge_search(request: Request):
        """语义检索知识库。body: {query, top_k?, score?}"""
        try:
            params = await _params(request)
            query = (params.get("query") or "").strip()
            if not query:
                return JSONResponse(public.returnMsg(False, "query 不能为空"))
            kb = _kb_instance()
            res = kb.search(query, top_k=int(params.get("top_k") or 5),
                            score=float(params.get("score") or 0.2))
            if not res.get("status"):
                return JSONResponse(public.returnMsg(False, res.get("msg", "检索失败")))
            return JSONResponse(public.return_data(True, data={
                "query": query,
                "results": res.get("results", []),
            }))
        except Exception:
            logger.warning("知识库检索失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "知识库检索失败"))

    @app.post("/api/knowledge/delete")
    async def api_knowledge_delete(request: Request):
        """删除知识库文档。body: {doc_id}"""
        try:
            params = await _params(request)
            doc_id = (params.get("doc_id") or "").strip()
            if not doc_id:
                return JSONResponse(public.returnMsg(False, "doc_id 不能为空"))
            kb = _kb_instance()
            result = kb.delete_document(doc_id)
            if not result.get("status"):
                return JSONResponse(public.returnMsg(False, result.get("msg", "删除失败")))
            return JSONResponse(public.return_data(True, data=result))
        except Exception:
            logger.warning("知识库删除失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "知识库删除失败"))

    @app.post("/api/knowledge/add_mem0")
    async def api_knowledge_add_mem0():
        """从 mem0 导入全部记忆到知识库（幂等，已入库的跳过）。"""
        try:
            import requests as req
            from chat_client.knowledge_base import KnowledgeBase, build_embed_fn
            cfg = {}
            cfg_path = os.path.join(_BASE_DIR, "config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, encoding="utf-8") as f:
                    cfg = json.load(f)
            emb = cfg.get("embedding") or {}
            embed = build_embed_fn(
                emb.get("embedding_api_key") or "",
                emb.get("embedding_base_url") or "",
                emb.get("embedding_model_name") or "text-embedding-3-small",
            )
            kb = KnowledgeBase(os.path.join(_BASE_DIR, "knowledge"), embed)
            r = req.get("http://127.0.0.1:8000/memories", params={"limit": 1000}, timeout=15)
            memos = r.json().get("results") or []
            existing = {d["name"] for d in kb.list_documents()}
            imported = 0
            for m in memos:
                text = str(m.get("memory") or "").strip()
                if not text:
                    continue
                mid = str(m.get("id") or "")
                agent = str(m.get("agent_id") or m.get("user_id") or "unknown")
                name = f"mem0-{agent}-{mid[:8]}"
                if name in existing:
                    continue
                res = kb.add_document(text=text, name=name)
                if res.get("status"):
                    imported += 1
                    existing.add(name)
            return JSONResponse(public.return_data(True, data={
                "imported": imported,
                "total": len(memos),
            }))
        except Exception:
            logger.warning("mem0→知识库同步失败", exc_info=True)
            return JSONResponse(public.returnMsg(False, "同步失败"))
