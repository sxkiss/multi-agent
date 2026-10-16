"""增强分层记忆（对齐 Octop octop-memory 官方包）。

封装 `octop_memory.Memory`（sqlite backend），为 BT 提供超越 mem0 的
分层长期记忆（leaf/branch/root 组织 + recall + 可迁移导出/导入）。

与现有 mem0 工具并存：mem0 负责快速语义搜索，本模块作为**增强分层层**，
支持结构化主题树与可迁移备份。

设计（不破坏现有 API）：
- 所有路由走 /api/v2/memory/* 前缀；
- 存储用 sqlite（memory_octop.sqlite，已 gitignore），不引入外部服务；
- 懒加载 octop-memory，避免启动开销。

API：
  GET  /api/v2/memory/status       记忆库状态（节点数、主题数、命名空间）
  POST /api/v2/memory/store        存储一条记忆（content + topic + level）
  GET  /api/v2/memory/recall       按 query 召回
  GET  /api/v2/memory/list         列出节点（按 topic / limit）
  GET  /api/v2/memory/get/{id}     取单个节点
  DELETE /api/v2/memory/delete/{id}  删除节点
  GET  /api/v2/memory/export       导出全部记忆（可迁移 JSON）
  POST /api/v2/memory/import       导入记忆（迁移）
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import public
from platform_ext import PROJECT_ROOT

logger = logging.getLogger("platform-ext-memory")

router = APIRouter()

SQLITE_PATH = os.path.join(PROJECT_ROOT, "memory_octop.sqlite")
NAMESPACE = "bt"

_memory = None


def _get_memory():
    """懒加载 octop_memory.Memory（sqlite backend）。"""
    global _memory
    if _memory is not None:
        return _memory
    from octop_memory import Memory
    _memory = Memory(namespace=NAMESPACE, backend="sqlite", backend_config={"path": SQLITE_PATH})
    return _memory


# ── HTTP 路由 ─────────────────────────────────────────────
def _ok(data: Any = None, msg: str = "") -> JSONResponse:
    return JSONResponse(public.return_data(True, data=data, msg=msg))


def _err(msg: str) -> JSONResponse:
    return JSONResponse(public.returnMsg(False, msg))


def _node_to_dict(node) -> dict[str, Any]:
    return {
        "id": node.id,
        "parent_id": node.parent_id,
        "level": getattr(node, "level", "leaf"),
        "content": node.content,
        "topic": getattr(node, "topic", None),
        "conversation_id": getattr(node, "conversation_id", None),
        "created_at": str(node.created_at) if node.created_at else None,
        "updated_at": str(node.updated_at) if node.updated_at else None,
        "metadata": getattr(node, "metadata", {}) or {},
    }


@router.get("/memory/status")
async def memory_status() -> JSONResponse:
    try:
        m = _get_memory()
        nodes = m.backend.get_tree() if hasattr(m.backend, "get_tree") else []
        topics = {}
        for n in nodes:
            t = getattr(n, "topic", None) or "untagged"
            topics[t] = topics.get(t, 0) + 1
        stats = {}
        if hasattr(m.backend, "count_stats"):
            stats = m.backend.count_stats()
        return _ok({
            "namespace": m.namespace,
            "backend": "sqlite",
            "total_nodes": len(nodes),
            "topics": topics,
            "stats": stats,
        })
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] status 失败")
        return _err(f"记忆状态获取失败: {e}")


@router.post("/memory/store")
async def memory_store(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict) or not body.get("content"):
        return _err("缺少 content")
    try:
        m = _get_memory()
        node = m.store(
            content=str(body["content"]),
            topic=body.get("topic") or None,
            level=body.get("level") or "leaf",
            conversation_id=body.get("conversation_id") or None,
            metadata=body.get("metadata") or {},
        )
        return _ok({"node": _node_to_dict(node)})
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] store 失败")
        return _err(f"存储失败: {e}")


@router.get("/memory/recall")
async def memory_recall(query: str, limit: int = 5) -> JSONResponse:
    if not query:
        return _err("缺少 query")
    try:
        m = _get_memory()
        nodes = m.recall(query, limit=max(1, min(limit, 50)))
        return _ok({"query": query, "nodes": [_node_to_dict(n) for n in nodes]})
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] recall 失败")
        return _err(f"召回失败: {e}")


@router.get("/memory/list")
async def memory_list(topic: str = "", limit: int = 100) -> JSONResponse:
    try:
        m = _get_memory()
        nodes = m.backend.get_tree() if hasattr(m.backend, "get_tree") else []
        if topic:
            nodes = [n for n in nodes if (getattr(n, "topic", None) or "") == topic]
        nodes = nodes[:max(1, min(limit, 1000))]
        return _ok({"nodes": [_node_to_dict(n) for n in nodes]})
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] list 失败")
        return _err(f"列出失败: {e}")


@router.get("/memory/get/{node_id}")
async def memory_get(node_id: str) -> JSONResponse:
    try:
        m = _get_memory()
        node = m.get(node_id)
        if node is None:
            return _err(f"节点不存在: {node_id}")
        return _ok({"node": _node_to_dict(node)})
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] get 失败")
        return _err(f"获取失败: {e}")


@router.delete("/memory/delete/{node_id}")
async def memory_delete(node_id: str) -> JSONResponse:
    try:
        m = _get_memory()
        ok = m.delete(node_id, cascade=True)
        return _ok({"deleted": ok, "node_id": node_id})
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] delete 失败")
        return _err(f"删除失败: {e}")


@router.get("/memory/export")
async def memory_export() -> JSONResponse:
    try:
        m = _get_memory()
        nodes = m.backend.get_tree() if hasattr(m.backend, "get_tree") else []
        data = {
            "format": "octop-memory-portable",
            "namespace": m.namespace,
            "exported_at": __import__("datetime").datetime.now().isoformat(),
            "nodes": [_node_to_dict(n) for n in nodes],
        }
        return _ok(data)
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] export 失败")
        return _err(f"导出失败: {e}")


@router.post("/memory/import")
async def memory_import(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _err("请求体必须是 JSON")
    nodes = (body.get("nodes") or []) if isinstance(body, dict) else []
    if not nodes:
        return _err("缺少 nodes")
    try:
        m = _get_memory()
        imported = 0
        for n in nodes:
            if not isinstance(n, dict) or not n.get("content"):
                continue
            m.store(
                content=str(n["content"]),
                topic=n.get("topic") or None,
                level=n.get("level") or "leaf",
                conversation_id=n.get("conversation_id") or None,
                metadata=n.get("metadata") or {},
            )
            imported += 1
        return _ok({"imported": imported})
    except Exception as e:  # noqa: BLE001
        logger.exception("[memory] import 失败")
        return _err(f"导入失败: {e}")