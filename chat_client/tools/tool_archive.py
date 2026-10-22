"""工具结果归档召回（P6，2026-10-15）。

当工具结果过大时，agent.py 会调用 `tool_archive.archive_result` 精简入上下文，
完整内容落盘 `tool_archives/<session>/<id>.txt`。本工具让模型**按需取回**完整内容，

@tool-as-app: 上下文治理（归档召回）
@output: 按 archive_id 取回被精简的工具结果全文/分段
@position: Tools layer — 上下文治理配套工具
"""

from . import PROJECT_ROOT, register_tool
from .base import _xml_response


@register_tool(category="Agent", name_cn="读取工具结果归档", risk_level="low")
def ReadToolArchive(archive_id: str, offset: int = 1, limit: int = 0) -> str:
    """
    取回被精简（折叠）的工具结果完整内容。当上下文中出现
    `[...已精简 ... 可用 ReadToolArchive(archive_id="xxx") 取回全文 ...]`
    标记时使用本工具，可读回完整原文，或按行范围分段读取。

    Args:
        archive_id: 归档标识（来自上下文中的折叠标记，如 "a1b2c3d4e5f6"）
        offset: 起始行号（从 1 开始，默认 1）
        limit: 读取行数（默认 0 = 读到末尾）
    """
    from ..tool_archive import read_tool_archive
    from . import _current_session_id

    aid = (archive_id or "").strip()
    if not aid:
        return _xml_response("error", "需要提供 archive_id（见上下文中的折叠标记）")

    session_id = _current_session_id() or ""
    try:
        res = read_tool_archive(aid, base_dir=PROJECT_ROOT, session_id=session_id,
                                offset=offset, limit=limit)
    except Exception as e:
        return _xml_response("error", f"读取归档失败: {e!s}")

    if not res.get("ok"):
        return _xml_response("error", res.get("error") or "读取归档失败")

    head = (f"<!-- archive={aid} lines={res['returned_lines']}/{res['total_lines']} "
            f"chars={res['chars']} -->")
    content = res["content"]
    if res["returned_lines"] < res["total_lines"]:
        content += (f"\n\n[...仅显示第 {res['offset']}~"
                    f"{res['offset'] + res['returned_lines'] - 1} 行，共 {res['total_lines']} 行。"
                    f"继续读取: ReadToolArchive(archive_id=\"{aid}\", offset="
                    f"{res['offset'] + res['returned_lines']})...]")
    return _xml_response("done", f"{head}\n{content}")
