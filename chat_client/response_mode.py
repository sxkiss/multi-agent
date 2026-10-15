"""
@input: os, re, typing
@output: resolve_response_mode / strip_thinking — 响应模式协商 + 思考文本清洗（P7，对齐 Octop）
@position: Gateway layer — 按渠道配置响应模式；清洗模型输出中的思考标签
@auto-doc: Update header and folder INDEX.md when this file changes

P7（2026-10-15，boss「修建议的全部」）：对齐 Octop
`gateway/process/response_mode.py` + `gateway/process/channel_thinking.py`。

1) 响应模式协商（response_mode）：
   - invoke：一次性返回完整响应（适合不支持流式的渠道）
   - stream：流式返回（默认，适合 web/SSE）
   按渠道/会话配置解析，缺省 stream。

2) 思考文本清洗（strip_thinking）：
   模型可能把思考标签（`<thinking>` / `思考:` / ```` ```thinking ````）混进正文，
   需剥离后再展示/落库。bt 此前散落在 agent.py 内处理，此处统一。
"""
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

RESPONSE_MODES = ("stream", "invoke")


def resolve_response_mode(config: dict[str, Any] | None,
                          channel: str = "") -> str:
    """解析响应模式：渠道级 > config 级 > 默认 stream。"""
    cfg = config or {}
    ch_modes = cfg.get("channel_response_modes") or {}
    if channel and channel in ch_modes:
        m = str(ch_modes[channel]).strip().lower()
        if m in RESPONSE_MODES:
            return m
    m = str(cfg.get("response_mode") or "stream").strip().lower()
    return m if m in RESPONSE_MODES else "stream"


# 思考标签模式（按优先级）
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_FENCE_RE = re.compile(
    r"```(?:thinking|think|reasoning)\b.*?```",
    re.DOTALL | re.IGNORECASE,
)
# 前导「思考:」段落（到首个空行结束）
_LEADING_THINK_RE = re.compile(
    r"^\s*(?:思考|thinking|reasoning)\s*[:：].*?(?:\n\s*\n|\Z)",
    re.DOTALL | re.IGNORECASE,
)


def strip_thinking(text: str, *, strip_leading: bool = True) -> str:
    """剥离模型输出中的思考标签，返回纯正文。

    - 成对 `<thinking>...</thinking>` / ```` ```thinking ```` 块 → 删除
    - 前导「思考: ...」段落（可选，默认删）
    - 流式未闭合的 `<thinking>`（无闭合）→ 从该标签起到结尾删除（防半截思考污染正文）
    """
    if not text:
        return text
    out = _THINK_BLOCK_RE.sub("", text)
    out = _THINK_FENCE_RE.sub("", out)
    # 未闭合的 <thinking>：截断
    m = re.search(r"<think(?:ing)?>", out, re.IGNORECASE)
    if m:
        out = out[:m.start()]
    if strip_leading:
        out = _LEADING_THINK_RE.sub("", out, count=1)
    return out.strip()


def clean_for_display(text: str) -> str:
    """展示前清洗（等价 strip_thinking 默认行为）。"""
    return strip_thinking(text)
