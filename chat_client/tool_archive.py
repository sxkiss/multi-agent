"""
@input: hashlib, json, os, re, time
@output: maybe_archive_result / read_tool_archive / archive_stats — 工具结果精简+归档（P6）
@position: Context layer — 工具结果过大时精简入上下文、完整内容落盘归档、按需召回
@auto-doc: Update header and folder INDEX.md when this file changes

P6（2026-10-15，boss 反馈「上下文分解显示工具返回占 93%，若做工具结果精简/归档
能大幅降首 token 与成本」）：

背景：bt 现状是**硬截断**（`MAX_TOOL_RESULT_LEN = 50000`，只保留头部）。
问题有三：
1. 阈值太高（50K 字符 ≈ 12K+ token/条），大结果照样进上下文，占比 93%；
2. 只留头——模型常需要「尾部」（错误行、汇总行、退出码），截断即丢关键信息；
3. 一旦截断，**完整内容永久丢失**，模型无法按需取回。

- **精简（slim）**：超阈值的结果保留「头 + 尾 + 摘要标记」，中间折叠；
- **归档（archive）**：完整原文落盘 `tool_archives/<session>/<id>.txt`，
  上下文里只留引用标记 `<tool_archive id="..." .../>`；
- **召回（recall）**：新增 `ReadToolArchive` 工具，模型可按需取回完整内容
  （或按行范围读取），信息不丢失。

配置（config.json，均有默认值，向后兼容）：
- `tool_result_archive`: bool，默认 True —— 是否启用精简+归档
- `tool_result_max_chars`: int，默认 8000 —— 超过此字符数才精简
- `tool_result_head_chars`: int，默认 3000 —— 精简后保留的头部字符数
- `tool_result_tail_chars`: int，默认 1500 —— 精简后保留的尾部字符数
- `tool_result_archive_dir`: str，默认 "<base>/tool_archives"

设计取舍：
- 只对「文本类工具结果」精简；<tool> XML 结构（所有工具统一封装）保留标签，
  只精简其正文，避免破坏 `inject_tool_name` 注入的元信息。
- 归档失败（磁盘满/权限）时**降级为原样返回**，绝不因归档丢结果。
- 阈值/头尾长度可配；设为 0 可禁用。
"""
import hashlib
import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULTS = {
    "tool_result_archive": True,
    "tool_result_max_chars": 8000,
    "tool_result_head_chars": 3000,
    "tool_result_tail_chars": 1500,
}

_ARCHIVE_DIRNAME = "tool_archives"

# <tool> 结构：<tool>...<tool_name>X</tool_name>...<toolcall_result>正文</toolcall_result>...</tool>
_RESULT_RE = re.compile(r"(<toolcall_result>)(.*?)(</toolcall_result>)", re.DOTALL)
# 无 XML 结构时的纯文本工具结果
_ARCHIVE_REF_RE = re.compile(r"<tool_archive\s+id=\"([^\"]+)\"")


def _cfg(config: dict[str, Any] | None, key: str) -> Any:
    if not config:
        return DEFAULTS[key]
    val = config.get(key, DEFAULTS[key])
    return val


def _archive_root(base_dir: str, config: dict[str, Any] | None) -> str:
    custom = (config or {}).get("tool_result_archive_dir")
    if custom:
        return str(custom)
    return os.path.join(base_dir, _ARCHIVE_DIRNAME)


def _safe_session(session_id: str) -> str:
    """会话 id 转安全目录名（防路径穿越）。"""
    s = (session_id or "default").strip()
    s = re.sub(r"[^A-Za-z0-9_.:\-]", "-", s)
    return s.strip("-") or "default"


def _summarize(text: str) -> str:
    """给精简后的折叠段生成一句摘要（行数/字符数/疑似内容类型）。"""
    lines = text.count("\n") + 1
    chars = len(text)
    hint = ""
    low = text[:2000].lower()
    if "traceback" in low or "error" in low or "错误" in text[:2000]:
        hint = "，含错误/异常信息"
    elif text.lstrip().startswith("{") or text.lstrip().startswith("["):
        hint = "，疑似 JSON 数据"
    elif "<html" in low or "<!doctype" in low:
        hint = "，疑似 HTML 页面"
    return f"共 {chars} 字符 / {lines} 行{hint}"


def archive_result(result_str: str, *, base_dir: str, session_id: str,
                   tool_name: str = "", config: dict[str, Any] | None = None,
                   ) -> tuple[str, dict[str, Any] | None]:
    """按需精简+归档一个工具结果。

    返回 (处理后文本, 归档元数据或 None)。
    - 未超阈值 / 已禁用：原样返回，元数据 None
    - 超阈值：完整内容落盘，返回「头+折叠标记+尾」，元数据含 archive_id/path
    """
    if not isinstance(result_str, str) or not result_str:
        return result_str, None
    if not _cfg(config, "tool_result_archive"):
        return result_str, None

    max_chars = int(_cfg(config, "tool_result_max_chars") or 0)
    if max_chars <= 0 or len(result_str) <= max_chars:
        return result_str, None

    head_chars = int(_cfg(config, "tool_result_head_chars") or 0)
    tail_chars = int(_cfg(config, "tool_result_tail_chars") or 0)
    head_chars = max(0, head_chars)
    tail_chars = max(0, tail_chars)
    # 头尾之和不能超过原文，且至少留出折叠空间
    if head_chars + tail_chars >= len(result_str):
        return result_str, None

    # 优先只精简 <toolcall_result> 正文，保留 XML 元信息
    m = _RESULT_RE.search(result_str)
    if m:
        prefix, body, suffix = m.group(1), m.group(2), m.group(3)
        if len(body) <= max_chars:
            return result_str, None
        slim_body, meta = _slim_text(body, head_chars, tail_chars,
                                     base_dir=base_dir, session_id=session_id,
                                     tool_name=tool_name, config=config)
        new_text = result_str[:m.start()] + prefix + slim_body + suffix + result_str[m.end():]
        return new_text, meta

    # 纯文本结果：整体精简
    return _slim_text(result_str, head_chars, tail_chars,
                      base_dir=base_dir, session_id=session_id,
                      tool_name=tool_name, config=config)


def _slim_text(text: str, head_chars: int, tail_chars: int, *,
               base_dir: str, session_id: str, tool_name: str,
               config: dict[str, Any] | None = None,
               ) -> tuple[str, dict[str, Any] | None]:
    """精简正文 + 落盘归档。归档失败则原样返回（不丢信息）。"""
    archive_id = hashlib.sha1(
        (session_id + "|" + tool_name + "|" + str(time.time()) + "|" + text[:64]).encode("utf-8")
    ).hexdigest()[:12]
    meta: dict[str, Any] = {
        "id": archive_id,
        "tool": tool_name,
        "chars": len(text),
        "session": session_id,
        "created": time.time(),
    }
    path = ""
    try:
        root = os.path.join(_archive_root(base_dir, config), _safe_session(session_id))
        os.makedirs(root, exist_ok=True)
        fname = f"{archive_id}.txt"
        path = os.path.join(root, fname)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta["path"] = path
        # 索引（便于统计/清理）：append 一行 jsonl
        try:
            with open(os.path.join(root, "index.jsonl"), "a", encoding="utf-8") as idx:
                idx.write(json.dumps(meta, ensure_ascii=False) + "\n")
        except OSError:
            pass
    except Exception as e:
        logger.warning("工具结果归档失败，降级为原样返回: %s", e)
        return text, None

    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars > 0 else ""
    folded = _summarize(text[head_chars:len(text) - tail_chars] if tail_chars else text[head_chars:])
    marker = (
        f"\n\n[...已精简 {folded}。完整内容已归档，"
        f"可用 ReadToolArchive(archive_id=\"{archive_id}\") 取回全文，"
        f"或 ReadToolArchive(archive_id=\"{archive_id}\", offset=N, limit=M) 分段读取。...]\n\n"
    )
    slim = head + marker + tail
    meta["slim_chars"] = len(slim)
    return slim, meta


def read_tool_archive(archive_id: str, *, base_dir: str, session_id: str = "",
                      offset: int = 1, limit: int = 0, config: dict[str, Any] | None = None,
                      ) -> dict[str, Any]:
    """读取归档的工具结果完整内容（或按行范围）。

    返回 {ok, content, total_lines, returned_lines, path, chars} 或 {ok: False, error}
    """
    aid = (archive_id or "").strip()
    if not aid or not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", aid):
        return {"ok": False, "error": f"非法 archive_id: {archive_id!r}"}
    root = _archive_root(base_dir, config)
    # 先按当前会话找；找不到再全局搜（归档可能来自其它会话）
    candidates = []
    if session_id:
        candidates.append(os.path.join(root, _safe_session(session_id), f"{aid}.txt"))
    candidates.append(os.path.join(root, "_shared", f"{aid}.txt"))
    # 全局搜索（有界：只扫两层目录）
    path = ""
    for c in candidates:
        if os.path.isfile(c):
            path = c
            break
    if not path and os.path.isdir(root):
        try:
            for sess in os.listdir(root):
                p = os.path.join(root, sess, f"{aid}.txt")
                if os.path.isfile(p):
                    path = p
                    break
        except OSError:
            pass
    if not path:
        return {"ok": False, "error": f"未找到归档 {aid}（可能已被清理）"}

    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        return {"ok": False, "error": f"读取归档失败: {e}"}

    lines = text.splitlines()
    total = len(lines)
    off = max(1, int(offset or 1))
    if limit and int(limit) > 0:
        chunk = lines[off - 1: off - 1 + int(limit)]
    else:
        chunk = lines[off - 1:]
    return {
        "ok": True,
        "content": "\n".join(chunk),
        "total_lines": total,
        "returned_lines": len(chunk),
        "offset": off,
        "path": path,
        "chars": len(text),
    }


def archive_stats(base_dir: str, session_id: str = "",
                  config: dict[str, Any] | None = None) -> dict[str, Any]:
    """归档统计（供 API/前端展示上下文治理效果）。"""
    root = _archive_root(base_dir, config)
    if not os.path.isdir(root):
        return {"exists": False, "sessions": 0, "files": 0, "bytes": 0}
    sessions = 0
    files = 0
    total_bytes = 0
    dirs = [_safe_session(session_id)] if session_id else sorted(os.listdir(root))
    for sess in dirs:
        d = os.path.join(root, sess)
        if not os.path.isdir(d):
            continue
        sessions += 1
        for fn in os.listdir(d):
            if fn.endswith(".txt"):
                files += 1
                try:
                    total_bytes += os.path.getsize(os.path.join(d, fn))
                except OSError:
                    pass
    return {"exists": True, "sessions": sessions, "files": files,
            "bytes": total_bytes, "dir": root}
