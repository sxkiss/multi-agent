"""Agent 纯工具函数（与 Agent 类生命周期无关）。

从 chat_client/agent.py 拆出的 token 估算、取消上下文、ViewImage
内容转换等无状态辅助函数，便于单独测试与复用。
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from chat_client.tools.base import _xml_response

logger = logging.getLogger(__name__)

# L-1: tiktoken 编码器缓存在模块级，避免每次调用重复导入
_TIKTOKEN_ENC = None


def estimate_tokens(text: str) -> int:
    """估算 token 数量（CJK-aware：尽量贴近实际，避免高估/低估）。
    策略：优先尝试 tiktoken；降级到启发式估算。
    - CJK 字符（\\u4e00-\\u9fff）、全角标点、假名：约 1 字符/token（偏高，防截断过早）
    - ASCII 字母/数字：约 4 字符/token（更接近真实比）
    - 其余标点/空白：1 字符/token
    总体上限与下限均合理，避免英文严重高估、中文严重低估。

    模块级函数：_build_messages 的轮间压缩与主循环的工具内压缩闸门共用。
    """
    try:
        global _TIKTOKEN_ENC
        if _TIKTOKEN_ENC is None:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        if _TIKTOKEN_ENC:
            return max(1, len(_TIKTOKEN_ENC.encode(text)))
    except Exception:
        _TIKTOKEN_ENC = False  # 标记不可用，后续不再重试导入
    # 启发式 fallback：区分 CJK 与 ASCII
    cjk_count = sum(1 for ch in text
                    if ('\u4e00' <= ch <= '\u9fff')
                    or ('\u3000' <= ch <= '\u303f')
                    or ('\uff00' <= ch <= '\uffef'))
    ascii_chars = len(text) - cjk_count
    # CJK ~1.0 token/char，ASCII ~0.25 token/char；偏保守取中间值
    return max(1, int(cjk_count * 1.0 + ascii_chars * 0.35))

BINARY_EXTENSIONS = {
    '.zip', '.tar', '.gz', '.exe', '.dll', '.so', '.class', '.jar', '.war', '.7z',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.pdf', '.doc', '.docx', '.xls', '.xlsx',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav', '.ogg', '.mpg', '.mpeg',
    '.iso', '.bin', '.dat', '.db', '.sqlite', '.pyc', '.pyo'
}


# ============================================================
# 任务取消（chat_stop → 后台 Agent 线程真正中断）
#
# 背景：原先 chat_stop 只做 job.finish("stopped") + agent.close()，
# 后者仅关闭 RAG/HTTP 连接，不打断正在运行的 chat() 循环——停止后
# Agent 仍会继续流式接收/重试请求/执行工具，直到下一次 API 调用恰好失败。
# 现在 ChatJob 增加 cancel_event，Agent 主循环在本模块的检查点轮询。
# ============================================================

def _find_root_session_id(session_id: str, config: dict | None = None) -> str:
    """查找根会话 ID（有 IM 通道路由的会话）。

    逻辑：
    1. 如果自身 session_id 已在 _SESSION_ROUTES 中有记录 → 就是根会话
    2. 否则从 config 中向上查找 parent_session_id
    3. 都找不到则返回自身（兜底）

    这样无论 session_id 是什么格式（gw_、card_、ses_、UUID 等），
    只要能找到有通道路由的会话 ID，就能正确回灌消息。
    """
    from platform_ext.gateway_processor import get_session_route

    # 1. 检查自身是否有路由
    if get_session_route(session_id) is not None:
        return session_id

    # 2. 从 config 中向上查找
    if config:
        parent_sid = config.get("parent_session_id")
        if parent_sid and get_session_route(parent_sid) is not None:
            return parent_sid

    # 3. 兜底：返回自身
    return session_id


def _cancel_event():
    """取当前执行上下文（ChatJob）的取消事件；无任务上下文（如脚本直调 Agent）返回 None。"""
    from .tools import get_current_job
    job = get_current_job()
    return getattr(job, "cancel_event", None)


def _get_current_job():
    """取当前执行上下文的 job（工具并行执行时用于上下文隔离恢复）。"""
    from .tools import get_current_job
    return get_current_job()


def _is_cancelled() -> bool:
    """当前任务是否已被 chat_stop 请求停止。"""
    ev = _cancel_event()
    return ev is not None and ev.is_set()


def _sleep_interruptible(seconds: float) -> bool:
    """可被取消打断的等待。返回 True 表示等待期间收到取消请求。"""
    ev = _cancel_event()
    if ev is None:
        time.sleep(seconds)
        return False
    return ev.wait(seconds)


def _close_stream(stream) -> None:
    """关闭流式响应，释放底层连接（失败静默——取消路径不因清理异常中断）。"""
    try:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    except Exception:
        logger.debug("关闭响应流失败（已忽略）", exc_info=True)


def bad_args_hint(func_name: str) -> str:
    """工具参数非法（多为输出被截断）时回灌给模型的错误提示。

    单独抽出便于测试与复用：重点是让模型明白"别重发同一份超长参数"，
    而不是像旧实现那样静默按空参数执行、让模型误以为成功而无限重试。
    """
    return _xml_response(
        "error",
        f"Error: 工具 {func_name} 的参数不是合法 JSON（很可能是内容过长被截断）。"
        "不要重复提交同一份超长参数：请把大文件拆成多次较小的写入"
        "（先写骨架，再分次追加），或改用更短的实现后重试。",
    )


# ---------------- 图片（多模态）支持 ----------------
# 上游实测支持 OpenAI 风格 image_url 内容块（模型:auto 可正确识别图片内容）。
# 但历史上有两条链路会把图片吃掉：
#   1) webfetch 等把图片编码成 {"type":"file","mime":"image/png","url":"data:..."}，
#      而 _filter_file_blocks 会把 type="file" 整块删除 → 模型永远看不到图。
#   2) 图片若以 file 块存在，形状也不符合视觉协议（应为 image_url）。
# 这里统一做形状归一：图片 file 块 → image_url；非图片 file 块仍丢弃。

# ViewImage 结果里的 data:image URL 正则（用于从 tool result 文本中提取图片块）
_VIEWIMAGE_IMAGE_URL_RE = re.compile(r'(data:image/[\w-]+;base64,[A-Za-z0-9+/=]+)')
# ViewImage 路径正则：从 tool result 文本中提取 <path>...</path>
_VIEWIMAGE_PATH_RE = re.compile(r'<path>(.+?)</path>')
# 图片文件扩展名（用于判定 <path> 指向的是不是图片，避免把普通文本路径误判为 ViewImage）
_IMAGE_EXT_RE = re.compile(r'\.(?:png|jpe?g|gif|webp|bmp|svg|tiff?|ico)$', re.IGNORECASE)


def _viewimage_content_blocks(text: str) -> list | None:
    """从 ViewImage 结果文本里提取 <path>，读取图片并返回 image_url 内容块列表。
    若文本里无 path 或图片不可读则返回 None（保持原文本）。"""
    if not isinstance(text, str):
        return None
    _m = _VIEWIMAGE_PATH_RE.search(text)
    if not _m:
        return None
    img_path = _m.group(1).strip()
    if not os.path.isfile(img_path):
        return None
    try:
        import base64 as _b64
        import mimetypes as _mime_mod
        with open(img_path, "rb") as _f:
            _raw = _f.read()
        _b64_str = _b64.b64encode(_raw).decode("ascii")
        _mime = _mime_mod.guess_type(img_path)[0] or "image/jpeg"
        _data_url = f"data:{_mime};base64,{_b64_str}"
        return [
            {"type": "text", "text": "查看图片结果："},
            {"type": "image_url", "image_url": {"url": _data_url}},
        ]
    except Exception as _e:
        logger.debug("ViewImage 读取图片失败 %s: %s", img_path, _e)
        return None


def _restore_viewimage_markers(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把历史 tool 消息里丢失的 _func_name 标记补回来，使 _coerce_tool_contents 能识别 image_url。

    sessions.journal 归档只存 content 字段，丢失了 run 时注入的 _func_name。
    这里通过 pattern 启发式恢复：tool result 文本中含 <path> 且格式是图片扩展名
    的，视为 ViewImage 结果，补上 _func_name 和 image_url 内容块。
    其他工具的 list content 仍由 _coerce_tool_contents 压平为字符串。
    """
    if not messages:
        return messages
    out = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "tool":
            out.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, list):
            # 已有 _func_name 则保留，否则补一个（防止后续 _coerce 误判）
            if "_func_name" not in m:
                out.append({**m, "_func_name": "unknown"})
            else:
                out.append(m)
            continue
        # list content：检查是否为 ViewImage 的 image_url 块
        has_image_url = any(
            isinstance(b, dict) and b.get("type") == "image_url" for b in content
        )
        if has_image_url and "_func_name" not in m:
            m = {**m, "_func_name": "ViewImage"}
        out.append(m)
    return out


def _file_block_to_image_url(block: Any):
    """把 file 块里的图片转成标准 image_url 块；非图片返回 None。

    只认 data:image/ 前缀或 image/* mime，避免把普通附件误当图片发给模型。
    """
    if not isinstance(block, dict) or block.get("type") != "file":
        return None
    url = block.get("url") or ""
    if not isinstance(url, str) or not url.strip():
        return None
    mime = str(block.get("mime") or block.get("mime_type") or "").lower()
    if not (mime.startswith("image/") or url.startswith("data:image/")):
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _content_for_token_estimate(content: Any) -> Any:
    """token 估算用：把图片的 base64 载荷替换为占位符。

    base64 长度与真实 token 数无关（上游按图像分辨率切 patch），若按字符估算，
    一张几十 KB 的图会被算成十几万 token，直接误触发上下文压缩。
    """
    if not isinstance(content, list):
        return content
    out = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            out.append({"type": "image_url", "image_url": {"url": "[image]"}})
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            out.append({"type": "text", "text": item["text"]})
        else:
            out.append({"type": "text", "text": "[non-text]"})
    return out


def _drop_image_blocks(content: Any) -> Any:
    """丢弃历史轮次里的图片，替换为占位文本。

    图片以 base64 存在历史里，若每轮都重发，上下文会迅速膨胀（且重复计费）。
    只保留最新一轮的图，旧图降级为文字提示，既省 token 又保留"此处曾有图"的语义。
    """
    if not isinstance(content, list):
        return content
    out, dropped = [], 0
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            dropped += 1
            continue
        out.append(item)
    if dropped:
        out.append({"type": "text", "text": f"[历史消息中的 {dropped} 张图片已省略]"})
    return out


def _is_viewimage_result(msg: dict[str, Any]) -> bool:
    """判断 tool 消息是否为 ViewImage 结果（含 image_url 内容块）。

    ViewImage 结果的特殊处理：
    - journal 里存纯文本（保留可检索性）；sessions.json 压缩快照里可能混合格式
    - _restore_viewimage_markers 在 _build_messages 入口补回 _func_name 和 image_url 块
    - 但快照加载（MemoryManager.load_session）可能跳过 _restore_viewimage_markers
      → 这里兜底识别，确保历史里的 ViewImage image_url 不被 _drop_image_blocks 丢弃
    """
    if msg.get("role") != "tool":
        return False
    if msg.get("_func_name") == "ViewImage":
        return True
    # 兜底：content 里有 image_url 块且文本含 <path>/home/.../*.png</path>
    c = msg.get("content")
    if isinstance(c, list):
        if any(b.get("type") == "image_url" for b in c if isinstance(b, dict)):
            return True
    if isinstance(c, str):
        _pm = _VIEWIMAGE_PATH_RE.search(c)
        if _pm and _IMAGE_EXT_RE.search(_pm.group(1)):
            return True
    return False
