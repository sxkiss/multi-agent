"""
@input: json, os, time, typing, chat_client.memory
@output: analyze_context / export_session / fork_session / list_session_stats
@position: Session utilities — 上下文用量分解 / 会话导出 / 对话分支（P4）
@auto-doc: Update header and folder INDEX.md when this file changes

P4（2026-10-15，借鉴 Octop）：
- 上下文用量分解（context_breakdown）：估算系统提示/工具定义/历史/知识等各段占用
- 会话导出（export）：把会话导出为 Markdown / JSON
- 对话分支（fork）：从某条消息复制出一个新会话，原会话不变
bt 此前均无这些能力。
"""
import json
import os
import re
import time
import uuid
from typing import Any

# 各段估算的 token 折算（中英文混合，保守 2 字符/token，与 agent.py 一致）
_CHARS_PER_TOKEN = 2


def _estimate_tokens(text: str) -> int:
    return max(1, len(text or "") // _CHARS_PER_TOKEN) if text else 0


def _sessions_root(base_dir: str) -> str:
    return os.path.join(base_dir, "sessions")


def _load_session_json(base_dir: str, session_id: str) -> list[dict[str, Any]]:
    path = os.path.join(_sessions_root(base_dir), session_id, "sessions.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _content_to_text(content: Any) -> str:
    """把消息 content（可能是 str 或 block 列表）展平成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(str(b.get("text", "")))
                elif b.get("type") == "image_url":
                    parts.append("[图片]")
                else:
                    parts.append(str(b.get("text", "") or ""))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return str(content or "")


# ============================================================
# P4-a: 上下文用量分解
# ============================================================
def analyze_context(base_dir: str, session_id: str, *,
                    context_window_tokens: int = 256 * 1024,
                    system_prompt: str = "", tool_definitions: str = "",
                    skills: str = "", mcp: str = "", subagent: str = "") -> dict[str, Any]:
    """估算某会话的上下文占用，按段分解。

    P7（2026-10-15）：对齐 Octop context_breakdown 的 7 段模型：
      system_prompt / tool_definitions / rules / skills / mcp /
      subagent_definitions / conversation
    其中「conversation」由会话历史细分（对话正文/工具返回/思考/知识注入）；
    其余段为静态注入，可由调用方传入实测值（不传则从会话元数据估算）。

    返回 {total_tokens, window_tokens, usage_ratio, segments:[{name,tokens,ratio}],
          segments_dict: {key: tokens}}
    """
    msgs = _load_session_json(base_dir, session_id)
    # 会话内可归因的部分
    conv = {
        "conversation": 0,      # 对话正文
        "tool_results": 0,      # 工具返回（含 <tool_result>）
        "thinking": 0,          # 思考段
        "knowledge": 0,         # 知识/记忆注入
        "other": 0,
    }
    for m in msgs:
        text = _content_to_text(m.get("content"))
        if not text:
            continue
        toks = _estimate_tokens(text)
        role = str(m.get("role", ""))
        if "<tool_result>" in text or "<tool_name>" in text or role == "tool":
            conv["tool_results"] += toks
        elif "think" in text[:40].lower() and ("思考" in text or "<think" in text):
            conv["thinking"] += toks
        elif role in ("user", "assistant", "system"):
            if role == "system" or "[来源:" in text:
                conv["knowledge"] += toks
            else:
                conv["conversation"] += toks
        else:
            conv["other"] += toks

    # 静态注入段（对齐 Octop SEGMENT_KEYS）
    static = {
        "system_prompt": _estimate_tokens(system_prompt),
        "tool_definitions": _estimate_tokens(tool_definitions),
        "rules": 0,          # 护栏/规则注入（bt 目前极少，预留）
        "skills": _estimate_tokens(skills),
        "mcp": _estimate_tokens(mcp),
        "subagent_definitions": _estimate_tokens(subagent),
    }
    # 会话内细分并入 conversation 段（对话正文=正文+知识+思考+其他；工具返回单列）
    segments_dict: dict[str, int] = {
        **static,
        "conversation": conv["conversation"] + conv["thinking"] + conv["knowledge"] + conv["other"],
        "tool_results": conv["tool_results"],
    }
    total = sum(segments_dict.values())
    names = {
        "system_prompt": "系统提示",
        "tool_definitions": "工具定义",
        "rules": "规则注入",
        "skills": "技能摘要",
        "mcp": "MCP 工具",
        "subagent_definitions": "子代理定义",
        "conversation": "对话正文",
        "tool_results": "工具返回",
    }
    segments = [
        {"name": names[k], "key": k, "tokens": v,
         "ratio": round(v / total, 4) if total else 0}
        for k, v in segments_dict.items() if v > 0
    ]
    segments.sort(key=lambda s: s["tokens"], reverse=True)
    return {
        "status": True,
        "session_id": session_id,
        "messages": len(msgs),
        "total_tokens": total,
        "window_tokens": context_window_tokens,
        "usage_ratio": round(total / context_window_tokens, 4) if context_window_tokens else 0,
        "segments": segments,
        "segments_dict": segments_dict,
        "note": "token 为按字符数估算（2 字符≈1 token），非精确值，仅用于占比参考。",
    }


# ============================================================
# P4-b: 会话导出
# ============================================================
def export_session(base_dir: str, session_id: str, fmt: str = "markdown") -> dict[str, Any]:
    """导出会话为 Markdown 或 JSON。返回 {status, format, content, chars}。"""
    msgs = _load_session_json(base_dir, session_id)
    if not msgs:
        return {"status": False, "msg": f"会话为空或不存在: {session_id}"}
    fmt = (fmt or "markdown").strip().lower()

    if fmt in ("json", "raw"):
        return {"status": True, "format": "json", "session_id": session_id,
                "content": json.dumps(msgs, ensure_ascii=False, indent=2),
                "chars": len(json.dumps(msgs, ensure_ascii=False))}

    # Markdown
    meta = {}
    meta_path = os.path.join(_sessions_root(base_dir), session_id, "meta.json")
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8")) or {}
        except Exception:
            meta = {}
    lines = [
        f"# 会话导出 · {session_id}",
        "",
        f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 消息数：{len(msgs)}",
    ]
    if meta.get("workspace"):
        lines.append(f"- 工作目录：{meta['workspace']}")
    if meta.get("source"):
        lines.append(f"- 来源：{meta['source']}")
    lines += ["", "---", ""]

    role_label = {"user": "🧑 用户", "assistant": "🤖 助手", "system": "⚙️ 系统", "tool": "🔧 工具"}
    for m in msgs:
        role = str(m.get("role", "unknown"))
        text = _content_to_text(m.get("content"))
        if not text.strip():
            continue
        lines.append(f"## {role_label.get(role, role)}")
        lines.append("")
        lines.append(text.strip())
        lines.append("")

    content = "\n".join(lines)
    return {"status": True, "format": "markdown", "session_id": session_id,
            "content": content, "chars": len(content)}


# ============================================================
# P4-c: 对话分支（fork）
# ============================================================
def fork_session(base_dir: str, session_id: str, *,
                 up_to_message_id: str = "", new_session_id: str = "") -> dict[str, Any]:
    """从指定消息（含）复制出一个新会话，原会话不变。

    up_to_message_id 留空则复制全部消息。
    返回 {status, new_session_id, copied}
    """
    msgs = _load_session_json(base_dir, session_id)
    if not msgs:
        return {"status": False, "msg": f"源会话为空或不存在: {session_id}"}

    if up_to_message_id:
        cut = None
        for i, m in enumerate(msgs):
            if str(m.get("id", "")) == str(up_to_message_id):
                cut = i
                break
        if cut is None:
            return {"status": False, "msg": f"未找到消息 id: {up_to_message_id}"}
        msgs = msgs[:cut + 1]

    new_id = new_session_id or f"fork-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    dst_dir = os.path.join(_sessions_root(base_dir), new_id)
    os.makedirs(dst_dir, exist_ok=True)
    dst_file = os.path.join(dst_dir, "sessions.json")
    if os.path.exists(dst_file):
        return {"status": False, "msg": f"目标会话已存在: {new_id}"}

    # 原子写
    tmp = dst_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(msgs, f, ensure_ascii=False)
    os.replace(tmp, dst_file)

    # meta：标记来源，便于前端分组
    try:
        src_meta = {}
        src_meta_path = os.path.join(_sessions_root(base_dir), session_id, "meta.json")
        if os.path.exists(src_meta_path):
            src_meta = json.load(open(src_meta_path, encoding="utf-8")) or {}
        meta = dict(src_meta)
        meta["forked_from"] = session_id
        meta["forked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(dst_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return {"status": True, "new_session_id": new_id, "copied": len(msgs),
            "source": session_id}


# ============================================================
# 会话统计
# ============================================================
def list_session_stats(base_dir: str, limit: int = 50) -> dict[str, Any]:
    """列出各会话的规模统计（消息数/字符数/最后修改），便于管理。"""
    root = _sessions_root(base_dir)
    if not os.path.isdir(root):
        return {"status": True, "sessions": [], "total": 0}
    rows = []
    for name in os.listdir(root):
        d = os.path.join(root, name)
        sf = os.path.join(d, "sessions.json")
        if not os.path.isdir(d) or not os.path.exists(sf):
            continue
        try:
            st = os.stat(sf)
            msgs = _load_session_json(base_dir, name)
            chars = sum(len(_content_to_text(m.get("content"))) for m in msgs)
            rows.append({
                "session_id": name,
                "messages": len(msgs),
                "chars": chars,
                "est_tokens": chars // _CHARS_PER_TOKEN,
                "mtime": int(st.st_mtime),
            })
        except Exception:
            continue
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return {"status": True, "sessions": rows[:limit], "total": len(rows)}
