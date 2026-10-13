"""
@input: os, json, time, uuid, logging, threading; numpy, openai; chat_client.document_parser
@output: KnowledgeBase, build_embed_fn
@position: Knowledge layer — 文档知识库（解析→切块→向量化→检索→引用）
@auto-doc: Update header and folder INDEX.md when this file changes

设计参考 TencentCloud/Octop 的 knowledge/service.py + index.py +
retrieve.py + citations.py，按 BT 现有 SimpleVectorDB 的轻量 JSON 存储风格落地。

存储布局（storage_dir 下）：
  manifest.json   文档清单（doc_id -> 元信息）
  chunks.json     切块 + 向量（[{id, doc_id, text, embedding, idx}]）

与现有会话 RAG 的区别：RAGService 只索引「对话对」，本模块索引「文档」，
可长期沉淀、跨会话检索。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import numpy as np

from chat_client.document_parser import (
    SUPPORTED_EXTS,
    chunk_text,
    is_supported,
    parse_document,
)

logger = logging.getLogger(__name__)


def build_embed_fn(embedding_api_key: str, embedding_base_url: str,
                   embedding_model: str, default_headers: dict | None = None,
                   timeout: int = 30) -> Callable[[list[str]], list[list[float]]]:
    """构造 embedding 函数（可用 OpenAI 兼容端点，如本地 mem0 /v1/embeddings）。

    返回 fn(texts: list[str]) -> list[list[float]]，失败返回空列表。
    """
    import openai

    client = openai.OpenAI(
        api_key=embedding_api_key or "EMPTY",
        base_url=embedding_base_url or "https://api.openai.com/v1",
        default_headers=default_headers,
        timeout=timeout,
        max_retries=2,
    )

    def _embed(texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not embedding_api_key or not embedding_base_url:
            logger.warning("[Knowledge] embedding 未配置，跳过向量化")
            return []
        try:
            resp = client.embeddings.create(
                input=[t.replace("\n", " ")[:4000] for t in texts],
                model=embedding_model or "text-embedding-3-small",
            )
            return [d.embedding for d in resp.data]
        except Exception as e:
            logger.warning("[Knowledge] embedding 调用失败: %s", e)
            return []

    return _embed


class KnowledgeBase:
    """文档知识库：支持多文档入库、检索、删除，JSON 持久化 + 余弦检索。"""

    def __init__(self, storage_dir: str, embed_fn: Callable[[list[str]], list[list[float]]]):
        self.storage_dir = storage_dir
        self.embed_fn = embed_fn
        os.makedirs(storage_dir, exist_ok=True)
        self.manifest_file = os.path.join(storage_dir, "manifest.json")
        self.chunks_file = os.path.join(storage_dir, "chunks.json")
        self._lock = threading.Lock()
        self.manifest: dict[str, dict] = {}
        self.chunks: list[dict] = []
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            if os.path.exists(self.manifest_file):
                with open(self.manifest_file, encoding="utf-8") as f:
                    self.manifest = json.load(f)
            if os.path.exists(self.chunks_file):
                with open(self.chunks_file, encoding="utf-8") as f:
                    self.chunks = json.load(f)
        except Exception:
            logger.warning("[Knowledge] 知识库文件损坏，重置为空库", exc_info=True)
            self.manifest, self.chunks = {}, []

    def _save(self):
        self._atomic_write(self.manifest_file, self.manifest)
        self._atomic_write(self.chunks_file, self.chunks)

    @staticmethod
    def _atomic_write(path: str, data: Any):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            logger.warning("[Knowledge] 写入失败 %s", path, exc_info=True)

    # ---------- 入库 ----------
    def add_document(self, path: str | None = None, text: str | None = None,
                     name: str = "", metadata: dict | None = None,
                     ocr: bool = True) -> dict:
        """入库一个文档（文件路径 或 直接文本）。返回结果字典。"""
        metadata = metadata or {}
        warnings: list[str] = []
        ocr_used = False

        if text is None:
            if not path or not os.path.exists(path):
                return {"status": False, "msg": f"文件不存在: {path}"}
            if not is_supported(path):
                return {"status": False,
                        "msg": f"不支持的格式: {Path(path).suffix}，支持: {', '.join(sorted(SUPPORTED_EXTS))}"}
            parsed = parse_document(path, ocr=ocr)
            text = parsed["text"]
            ocr_used = parsed["ocr_used"]
            warnings = parsed["warnings"]
            name = name or os.path.basename(path)
        else:
            name = name or f"text-{int(time.time())}"

        if not (text or "").strip():
            return {"status": False, "msg": "文档解析结果为空", "warnings": warnings}

        chunks = chunk_text(text, size=800, overlap=120)
        if not chunks:
            return {"status": False, "msg": "切块结果为空"}

        # 批量向量化（分批，避免单次 payload 过大）
        embeddings: list[list[float]] = []
        BATCH = 16
        for i in range(0, len(chunks), BATCH):
            batch = chunks[i:i + BATCH]
            vecs = self.embed_fn(batch)
            if not vecs or len(vecs) != len(batch):
                return {"status": False,
                        "msg": f"向量化失败（embedding 未配置或上游不可用），已处理 {i}/{len(chunks)} 块"}
            embeddings.extend(vecs)

        doc_id = str(uuid.uuid4())[:8]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            for idx, (chunk, vec) in enumerate(zip(chunks, embeddings)):
                self.chunks.append({
                    "id": f"{doc_id}:{idx}",
                    "doc_id": doc_id,
                    "idx": idx,
                    "text": chunk,
                    "embedding": vec,
                })
            self.manifest[doc_id] = {
                "doc_id": doc_id,
                "name": name,
                "path": path or "",
                "ext": Path(path).suffix.lower() if path else ".txt",
                "chars": len(text),
                "chunks": len(chunks),
                "ocr_used": ocr_used,
                "added_at": now,
                "metadata": metadata,
            }
            self._save()

        return {"status": True, "doc_id": doc_id, "name": name,
                "chunks": len(chunks), "chars": len(text),
                "ocr_used": ocr_used, "warnings": warnings}

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = 5, score: float = 0.2,
               doc_ids: list[str] | None = None) -> dict:
        """语义检索，返回带引用的命中列表。

        Returns:
            {"status": bool, "results": [{text, doc_id, name, idx, score, citation}]}
        """
        if not query or not query.strip():
            return {"status": False, "msg": "查询为空", "results": []}
        with self._lock:
            candidates = list(self.chunks)
        if doc_ids:
            allowed = set(doc_ids)
            candidates = [c for c in candidates if c["doc_id"] in allowed]
        if not candidates:
            return {"status": True, "results": [], "msg": "知识库为空"}

        qvecs = self.embed_fn([query])
        if not qvecs:
            return {"status": False, "msg": "查询向量化失败（embedding 不可用）", "results": []}
        qvec = np.array(qvecs[0], dtype=float)
        qnorm = np.linalg.norm(qvec) or 1e-9

        scored = []
        for c in candidates:
            v = np.array(c["embedding"], dtype=float)
            vnorm = np.linalg.norm(v) or 1e-9
            sim = float(np.dot(v, qvec) / (vnorm * qnorm))
            if sim >= score:
                scored.append((sim, c))
        scored.sort(key=lambda t: t[0], reverse=True)
        scored = scored[:top_k]

        results = []
        for sim, c in scored:
            doc = self.manifest.get(c["doc_id"], {})
            dname = doc.get("name", c["doc_id"])
            citation = f"[来源: {dname}#块{c['idx']}]"
            results.append({
                "text": c["text"],
                "doc_id": c["doc_id"],
                "name": dname,
                "idx": c["idx"],
                "score": round(sim, 4),
                "citation": citation,
            })
        return {"status": True, "results": results}

    # ---------- 管理 ----------
    def list_documents(self) -> list[dict]:
        with self._lock:
            return sorted(self.manifest.values(), key=lambda d: d.get("added_at", ""), reverse=True)

    def delete_document(self, doc_id: str) -> dict:
        with self._lock:
            if doc_id not in self.manifest:
                return {"status": False, "msg": f"文档不存在: {doc_id}"}
            name = self.manifest[doc_id].get("name", doc_id)
            before = len(self.chunks)
            self.chunks = [c for c in self.chunks if c["doc_id"] != doc_id]
            del self.manifest[doc_id]
            self._save()
            return {"status": True, "msg": f"已删除 {name}", "removed_chunks": before - len(self.chunks)}

    def stats(self) -> dict:
        with self._lock:
            return {"documents": len(self.manifest), "chunks": len(self.chunks),
                    "chars": sum(d.get("chars", 0) for d in self.manifest.values())}

    def clear(self) -> dict:
        with self._lock:
            n = len(self.manifest)
            self.manifest, self.chunks = {}, []
            self._save()
            return {"status": True, "msg": f"已清空 {n} 个文档"}
