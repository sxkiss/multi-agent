import json
import logging
import os

from . import register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

KB_ADD_DESC = """- 把文档加入本地知识库（可长期沉淀、跨会话检索）
- 支持格式：pdf/docx/pptx/xlsx/html/md/txt/csv/json 及图片(png/jpg等，走OCR)
- 关键词密度高、结构化的资料优先入库
- 适用场景：用户说"把这个文件加入知识库"/"收录这份资料"/"建个知识库" """

KB_SEARCH_DESC = """- 从本地知识库做语义检索，返回带来源引用的相关片段
- 与对话历史检索(RAG)不同：这里检索的是你自己入库的文档
- 适用场景：回答需要引用私有资料时、用户问"知识库里有没有..." """

KB_LIST_DESC = """- 列出知识库中的全部文档（名称、块数、字数、入库时间）
- 适用场景：查看知识库有什么、确认某文档是否已入库"""

KB_DELETE_DESC = """- 从知识库删除指定文档（按 doc_id）
- doc_id 可通过 KnowledgeList 获取
- 不可撤销"""


def _kb_config() -> dict:
    """读取知识库配置（复用主 config.json 的 embedding 段）。"""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg: dict = {}
    for name in ("config.json",):
        p = os.path.join(base_dir, name)
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                logger.warning("读取 %s 失败", p, exc_info=True)
    emb = cfg.get("embedding", {}) if isinstance(cfg, dict) else {}
    return {
        "embedding_api_key": emb.get("embedding_api_key") or "",
        "embedding_base_url": emb.get("embedding_base_url") or "",
        "embedding_model_name": emb.get("embedding_model_name") or "text-embedding-3-small",
        "default_headers": cfg.get("default_headers") if isinstance(cfg, dict) else None,
    }


def _kb_storage_dir() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    d = os.path.join(base_dir, "knowledge")
    os.makedirs(d, exist_ok=True)
    return d


def _get_kb():
    from chat_client.knowledge_base import KnowledgeBase, build_embed_fn
    c = _kb_config()
    embed = build_embed_fn(
        c["embedding_api_key"], c["embedding_base_url"], c["embedding_model_name"],
        default_headers=c["default_headers"],
    )
    return KnowledgeBase(_kb_storage_dir(), embed)


@register_tool(category="知识", name_cn="知识库", risk_level="low")
def Knowledge(action: str = "search", query: str = "", top_k: int = 5, score: float = 0.2,
              path: str = "", text: str = "", name: str = "", doc_id: str = "") -> str:
    """
    知识库工具（支持入库/检索/列表/删除）。

    Args:
        action: 操作类型
            - add: 入库文档或文本（需 path 或 text）
            - search: 语义检索知识库（需 query）
            - list: 列出全部文档
            - delete: 删除文档（需 doc_id）
        query: 检索关键词（action=search 时必填）
        top_k: 返回条数，默认5（action=search 时可选）
        score: 最小相似度阈值，默认0.2（action=search 时可选）
        path: 文档路径（action=add 时可选，与 text 二选一）
        text: 直接入库的文本（action=add 时可选，与 path 二选一）
        name: 文档显示名（action=add 时可选）
        doc_id: 文档ID（action=list/delete 时使用）
    """
    act = (action or "search").strip().lower()
    if act == "add":
        return _knowledge_add(path, text, name)
    elif act == "search":
        return _knowledge_search(query, top_k, score)
    elif act == "list":
        return _knowledge_list()
    elif act == "delete":
        return _knowledge_delete(doc_id)
    else:
        return _xml_response("error", f"未知 action: {act}，可选: add/search/list/delete")


def _knowledge_add(path: str, text: str, name: str) -> str:
    """入库文档或文本。"""
    if not path and not text:
        return _xml_response("error", "action=add 须提供 path（文档路径）或 text（文本内容）之一")
    try:
        kb = _get_kb()
        result = kb.add_document(path=path or None, text=(text or None), name=name)
        if not result.get("status"):
            return _xml_response("error", json.dumps(result, ensure_ascii=False))
        return _xml_response("done", json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.warning("KnowledgeAdd 异常", exc_info=True)
        return _xml_response("error", f"知识库入库失败: {e}")


def _knowledge_search(query: str, top_k: int, score: float) -> str:
    """语义检索知识库。"""
    if not query or not query.strip():
        return _xml_response("error", "query 不能为空")
    try:
        kb = _get_kb()
        res = kb.search(query, top_k=int(top_k or 5), score=float(score or 0.2))
        if not res.get("status"):
            return _xml_response("error", json.dumps(res, ensure_ascii=False))
        hits = res.get("results", [])
        if not hits:
            return _xml_response("done", json.dumps({"query": query, "results": [],
                                                     "msg": res.get("msg", "未命中")}, ensure_ascii=False))
        lines = [f"{h['citation']} (score={h['score']})\n{h['text']}" for h in hits]
        return _xml_response("done", "\n\n".join(lines))
    except Exception as e:
        logger.warning("KnowledgeSearch 异常", exc_info=True)
        return _xml_response("error", f"知识库检索失败: {e}")


def _knowledge_list() -> str:
    """列出全部文档。"""
    try:
        kb = _get_kb()
        docs = kb.list_documents()
        stats = kb.stats()
        return _xml_response("done", json.dumps({
            "stats": stats,
            "documents": [
                {"doc_id": d["doc_id"], "name": d["name"], "chunks": d["chunks"],
                 "chars": d["chars"], "ocr_used": d.get("ocr_used", False),
                 "added_at": d.get("added_at", "")}
                for d in docs
            ],
        }, ensure_ascii=False))
    except Exception as e:
        logger.warning("KnowledgeList 异常", exc_info=True)
        return _xml_response("error", f"知识库列表失败: {e}")


def _knowledge_delete(doc_id: str) -> str:
    """删除文档。"""
    if not doc_id or not doc_id.strip():
        return _xml_response("error", "doc_id 不能为空")
    try:
        kb = _get_kb()
        result = kb.delete_document(doc_id.strip())
        if not result.get("status"):
            return _xml_response("error", json.dumps(result, ensure_ascii=False))
        return _xml_response("done", json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.warning("KnowledgeDelete 异常", exc_info=True)
        return _xml_response("error", f"知识库删除失败: {e}")


# 向后兼容别名
KnowledgeAdd = _knowledge_add
KnowledgeSearch = _knowledge_search
KnowledgeList = _knowledge_list
KnowledgeDelete = _knowledge_delete
