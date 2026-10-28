import json
import logging

from . import register_tool
from .base import _xml_response
from chat_client.retrieval import Mem0Service

logger = logging.getLogger(__name__)

MEM0_SEARCH_DESC = """- 从 mem0 记忆系统查询相关记忆
- 当需要回忆用户偏好、历史决策、已完成任务或之前踩过的坑时使用
- query: 搜索关键词（中文优先）
- limit: 返回条数，默认5
- 适用场景：用户提到"之前说过"/"我记得"/"查一下记录"，或任务涉及历史上下文时主动调用"""

MEM0_SEARCH_ALL_DESC = """- 跨域搜索所有 agent 的记忆（不限于当前 agent）
- 当需要查询其他工具（OpenCode、Claude Code、Hermes 等）的记忆时使用
- query: 搜索关键词（中文优先）
- limit: 返回条数，默认10
- 适用场景：用户问"其他工具说过什么"/"跨工具搜索"/"所有记忆"时调用"""

MEM0_ADD_DESC = """- 向 mem0 记忆系统写入重要信息
- 当用户表达了稳定偏好、协作规则、技术决策、踩坑经验时主动记录
- text: 要记住的内容（自然语言描述，建议清晰完整）
- 适用场景：用户明确说"记住"、"以后都这样"、"别忘"，或完成重要任务后记录教训"""

MEM0_LIST_DESC = """- 列出 mem0 记忆系统中的所有记忆
- 支持按 agent_id 过滤，不指定则列出所有 agent 的记忆
- limit: 最多返回条数，默认50
- 适用场景：查看记忆库全貌、整理过期记忆"""

MEM0_DELETE_DESC = """- 删除指定 ID 的记忆
- id: 记忆的 memory_id（可通过 mem0_list 获取）
- 不可撤销，谨慎使用"""

MEM0_IMPORT_DESC = """- 批量导入记忆（每行一条，用 \\n 分隔）
- texts: 多行文本，每行一条记忆
- 适用场景：从文件/文档一次性导入大量知识点"""


@register_tool(category="知识", name_cn="记忆", risk_level="low")
def mem0(action: str = "search", query: str = "", limit: int = 5, id: str = "",
         text: str = "", metadata: dict | None = None, agent_id: str | None = None,
         texts: str = "") -> str:
    """
    记忆系统工具（支持搜索/添加/列表/删除/导入）。

    Args:
        action: 操作类型
            - search: 查询当前agent记忆（需 query）
            - search_all: 跨域搜索所有agent记忆（需 query）
            - add: 写入记忆（需 text）
            - list: 列出记忆（可按 agent_id 过滤）
            - delete: 删除记忆（需 id）
            - import: 批量导入记忆（需 texts，每行一条）
        query: 搜索关键词（action=search/search_all 时必填）
        limit: 返回条数，默认5（action=list时默认50）
        id: 记忆ID（action=delete 时必填）
        text: 要记住的内容（action=add 时必填）
        metadata: 可选元数据（action=add 时可选）
        agent_id: 按agent_id过滤（action=list时可选）
        texts: 多行文本，每行一条记忆（action=import 时必填）
    """
    act = (action or "search").strip().lower()
    if act == "search":
        return _mem0_search(query, limit)
    elif act == "search_all":
        return _mem0_search_all(query, limit)
    elif act == "add":
        return _mem0_add(text, metadata)
    elif act == "list":
        return _mem0_list(agent_id, limit)
    elif act == "delete":
        return _mem0_delete(id)
    elif act == "import":
        return _mem0_import(texts)
    else:
        return _xml_response("error", f"未知 action: {act}，可选: search/search_all/add/list/delete/import")


def _mem0_search(query: str, limit: int) -> str:
    """查询当前agent记忆。"""
    try:
        m = Mem0Service(agent_id="ai-agent", rag_final_count=int(limit or 5))
        results = m.search(query, score=0.1)
        m.close()
        return _xml_response("done", json.dumps({"query": query, "results": results}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"mem0_search 异常: {e}")
        return _xml_response("error", f"mem0 搜索失败: {e}")


def _mem0_search_all(query: str, limit: int) -> str:
    """跨域搜索所有agent记忆。"""
    import httpx
    try:
        resp = httpx.post("http://localhost:8000/search", json={
            "query": query,
            "top_k": int(limit or 10)
        }, timeout=10)
        if resp.status_code != 200:
            return _xml_response("error", f"mem0 跨域搜索失败: {resp.status_code}")
        data = resp.json()
        results = data.get("results", [])
        return _xml_response("done", json.dumps({"query": query, "results": results}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"mem0_search_all 异常: {e}")
        return _xml_response("error", f"mem0 跨域搜索失败: {e}")


def _mem0_add(text: str, metadata: dict | None) -> str:
    """写入记忆。"""
    try:
        m = Mem0Service(agent_id="ai-agent")
        m.add_document(text, metadata)
        m.close()
        short = text[:60] + "..." if len(text) > 60 else text
        return _xml_response("done", json.dumps({"status": "ok", "text": short}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"mem0_add 异常: {e}")
        return _xml_response("error", f"mem0 写入失败: {e}")


def _mem0_list(agent_id: str | None, limit: int) -> str:
    """列出记忆。"""
    import httpx
    try:
        url = "http://localhost:8000/memories"
        params = {"limit": int(limit or 50)}
        if agent_id:
            params["agent_id"] = agent_id
        resp = httpx.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return _xml_response("error", f"mem0 列表失败: {resp.status_code}")
        data = resp.json()
        results = data.get("results", [])
        return _xml_response("done", json.dumps({"total": len(results), "memories": results}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"mem0_list 异常: {e}")
        return _xml_response("error", f"mem0 列表失败: {e}")


def _mem0_delete(id: str) -> str:
    """删除记忆。"""
    import httpx
    try:
        resp = httpx.delete(f"http://localhost:8000/memories/{id}", timeout=10)
        if resp.status_code == 200:
            return _xml_response("done", json.dumps({"status": "deleted", "id": id}))
        return _xml_response("error", f"mem0 删除失败: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.debug(f"mem0_delete 异常: {e}")
        return _xml_response("error", f"mem0 删除失败: {e}")


def _mem0_import(texts: str) -> str:
    """批量导入记忆。"""
    import httpx
    try:
        lines = [t.strip() for t in texts.strip().split("\n") if t.strip()]
        imported = 0
        for line in lines:
            resp = httpx.post("http://localhost:8000/memories", json={
                "messages": [{"role": "user", "content": line}],
                "agent_id": "ai-agent"
            }, timeout=10)
            if resp.status_code == 200:
                imported += 1
        return _xml_response("done", json.dumps({"status": "ok", "imported": imported, "total": len(lines)}))
    except Exception as e:
        logger.debug(f"mem0_import 异常: {e}")
        return _xml_response("error", f"mem0 导入失败: {e}")


# 向后兼容别名
mem0_search = _mem0_search
mem0_search_all = _mem0_search_all
mem0_add = _mem0_add
mem0_list = _mem0_list
mem0_delete = _mem0_delete
mem0_import = _mem0_import
