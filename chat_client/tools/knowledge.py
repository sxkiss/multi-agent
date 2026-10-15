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


@register_tool(category="知识", name_cn="知识库入库", risk_level="low")
def KnowledgeAdd(path: str = "", text: str = "", name: str = "") -> str:
    """
    把文档或文本加入本地知识库（解析→切块→向量化→可检索）。
    Args:
        path: 文档路径（本地文件，支持 pdf/docx/pptx/xlsx/html/md/txt/图片）
        text: 直接入库的文本（与 path 二选一）
        name: 可选，文档显示名
    """
    if not path and not text:
        return _xml_response("error", "须提供 path（文档路径）或 text（文本内容）之一")
    try:
        kb = _get_kb()
        target = path if path else None
        result = kb.add_document(path=target, text=(text or None), name=name)
        if not result.get("status"):
            return _xml_response("error", json.dumps(result, ensure_ascii=False))
        return _xml_response("done", json.dumps(result, ensure_ascii=False))
    except Exception as e:
        logger.warning("KnowledgeAdd 异常", exc_info=True)
        return _xml_response("error", f"知识库入库失败: {e}")


@register_tool(category="知识", name_cn="知识库检索", risk_level="low")
def KnowledgeSearch(query: str, top_k: int = 5, score: float = 0.2) -> str:
    """
    从本地知识库做语义检索，返回带来源引用的相关片段。
    Args:
        query: 检索问题/关键词
        top_k: 返回条数，默认5
        score: 最小相似度阈值，默认0.2
    """
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
        # 拼接带引用的上下文，便于模型直接引用
        lines = [f"{h['citation']} (score={h['score']})\n{h['text']}" for h in hits]
        return _xml_response("done", "\n\n".join(lines))
    except Exception as e:
        logger.warning("KnowledgeSearch 异常", exc_info=True)
        return _xml_response("error", f"知识库检索失败: {e}")


@register_tool(category="知识", name_cn="知识库列表", risk_level="low")
def KnowledgeList() -> str:
    """列出知识库中的全部文档（名称、doc_id、块数、字数、入库时间）。"""
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


@register_tool(category="知识", name_cn="知识库删除", risk_level="medium")
def KnowledgeDelete(doc_id: str) -> str:
    """
    从知识库删除指定文档（不可撤销）。
    Args:
        doc_id: 文档 ID（通过 KnowledgeList 获取）
    """
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
