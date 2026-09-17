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


@register_tool(category="知识", name_cn="查询记忆", risk_level="low")
def mem0_search(query: str, limit: int = 5) -> str:
    """
    从 mem0 记忆系统查询相关记忆。
    适用于回忆用户偏好、历史决策、技术踩坑等长周期上下文。
    Args:
        query: 搜索关键词
        limit: 返回条数，默认5
    """
    try:
        m = Mem0Service(agent_id="ai-agent", rag_final_count=int(limit or 5))
        results = m.search(query, score=0.1)
        m.close()
        return _xml_response("done", json.dumps({"query": query, "results": results}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"mem0_search 异常: {e}")
        return _xml_response("error", f"mem0 搜索失败: {e}")


@register_tool(category="知识", name_cn="跨域搜索记忆", risk_level="low")
def mem0_search_all(query: str, limit: int = 10) -> str:
    """
    跨域搜索所有 agent 的记忆（不限于当前 agent）。
    适用于查询其他工具（OpenCode、Claude Code、Hermes 等）的记忆。
    Args:
        query: 搜索关键词
        limit: 返回条数，默认10
    """
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


@register_tool(category="知识", name_cn="记住信息", risk_level="low")
def mem0_add(text: str, metadata: dict | None = None) -> str:
    """
    向 mem0 记忆系统写入重要信息。
    当用户表达稳定偏好、协作规则、技术决策、踩坑经验时主动调用。
    Args:
        text: 要记住的内容
        metadata: 可选元数据
    """
    try:
        m = Mem0Service(agent_id="ai-agent")
        m.add_document(text, metadata)
        m.close()
        short = text[:60] + "..." if len(text) > 60 else text
        return _xml_response("done", json.dumps({"status": "ok", "text": short}, ensure_ascii=False))
    except Exception as e:
        logger.debug(f"mem0_add 异常: {e}")
        return _xml_response("error", f"mem0 写入失败: {e}")


@register_tool(category="知识", name_cn="列出记忆", risk_level="low")
def mem0_list(agent_id: str | None = None, limit: int = 50) -> str:
    """
    列出 mem0 记忆系统中的所有记忆。
    Args:
        agent_id: 按 agent_id 过滤，不指定则列出全部
        limit: 最多返回条数，默认50
    """
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


@register_tool(category="知识", name_cn="删除记忆", risk_level="medium")
def mem0_delete(id: str) -> str:
    """
    删除指定 ID 的记忆（不可撤销）。
    Args:
        id: 记忆的 memory_id（通过 mem0_list 获取）
    """
    import httpx
    try:
        resp = httpx.delete(f"http://localhost:8000/memories/{id}", timeout=10)
        if resp.status_code == 200:
            return _xml_response("done", json.dumps({"status": "deleted", "id": id}))
        return _xml_response("error", f"mem0 删除失败: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.debug(f"mem0_delete 异常: {e}")
        return _xml_response("error", f"mem0 删除失败: {e}")


@register_tool(category="知识", name_cn="批量导入", risk_level="low")
def mem0_import(texts: str) -> str:
    """
    批量导入记忆（每行一条）。
    Args:
        texts: 多行文本，每行一条记忆，用换行符 \\n 分隔
    """
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
