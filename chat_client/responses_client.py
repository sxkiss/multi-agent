#!/usr/bin/env python3
"""
小巧的 Responses API 客户端适配层。

把 chat 风格的 messages / tools 转成 Responses 的 instructions / input / tools，
并把 ``client.responses.create(stream=True)`` 的事件流"翻译"成 agent 主循环
已经认识的 chat.delta 形状（choices / usage），从而让主循环零改动即可跑通。

关键点（踩坑记录）：
    - assistant 的函数调用必须发成**带 id** 的 function_call item。
      上游 agnes 的 ResponseInput 反序列化强制要求 id，缺 id 会返回 400：
      ``Invalid JSON data: Failed to deserialize the JSON body into the target type:
      input: data did not match any variant of untagged enum ResponseInput``
    - agnes 的 ResponseInput 不接受 ``reasoning`` item，因此 reasoning_content
      不转成 input item（正文/工具照常）。
    - function_call_output 用 ``call_id`` 关联，``output`` 为纯文本。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterator


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _text_of(content: Any) -> str:
    """把 chat 的 content（str / list[part] / None）压平成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                if "text" in p and p.get("type") in (None, "text", "output_text", "input_text"):
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _user_content_items(content: Any) -> list[dict[str, Any]]:
    """user 消息 content → Responses 的 input content 数组（文本 / 图片）。"""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    items: list[dict[str, Any]] = []
    if isinstance(content, list):
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "image_url":
                url = p.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if url:
                    items.append({"type": "input_image", "image_url": url})
            else:
                items.append({"type": "input_text", "text": str(p.get("text") or "")})
    return items or [{"type": "input_text", "text": ""}]


def convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """chat 工具定义（{type:function, function:{...}}）→ Responses 工具定义。"""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            nt: dict[str, Any] = {
                "type": "function",
                "name": fn.get("name"),
                "description": fn.get("description") or "",
            }
            if fn.get("parameters") is not None:
                nt["parameters"] = fn["parameters"]
            if "strict" in fn:
                nt["strict"] = fn["strict"]
            out.append(nt)
        else:  # 已经是 Responses 形状，原样透传
            out.append(t)
    return out


def build_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
    store: bool = False,
) -> dict[str, Any]:
    """把 chat 历史 + 工具定义组装成 Responses 请求体。"""
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    fc_seq = 0

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")

        if role in ("system", "developer"):
            txt = _text_of(m.get("content"))
            if txt:
                instructions.append(txt)
            continue

        if role == "user":
            input_items.append(
                {"type": "message", "role": "user", "content": _user_content_items(m.get("content"))}
            )
            continue

        if role == "assistant":
            txt = m.get("content")
            if isinstance(txt, str) and txt:
                input_items.append(
                    {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": txt}]}
                )
            elif not isinstance(txt, str):
                flat = _text_of(txt)
                if flat:
                    input_items.append(
                        {"type": "message", "role": "assistant",
                         "content": [{"type": "output_text", "text": flat}]}
                    )
            # reasoning_content 不转（agnes ResponseInput 不接受 reasoning item）
            for tc in (m.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                cid = tc.get("id") or ""
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                if not cid:
                    fc_seq += 1
                    cid = f"fc_auto_{fc_seq}"
                # ← 带 id 是上游 agnes 的硬性要求
                input_items.append({
                    "type": "function_call",
                    "id": cid,
                    "call_id": cid,
                    "name": fn.get("name") or "",
                    "arguments": args,
                })
            continue

        if role == "tool":
            cid = m.get("tool_call_id") or ""
            output = _text_of(m.get("content"))
            item: dict[str, Any] = {"type": "function_call_output", "call_id": cid, "output": output}
            if cid:
                item["id"] = f"fco_{cid}"
            input_items.append(item)
            continue

    payload: dict[str, Any] = {
        "model": model,
        "input": input_items,
        "stream": True,
        "store": bool(store),
    }
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    ct = convert_tools(tools)
    if ct:
        payload["tools"] = ct
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
    if max_output_tokens:
        payload["max_output_tokens"] = max_output_tokens
    return payload


# --------------------------------------------------------------------------- #
# stream adapter：Responses 事件 → chat.delta 形状
# --------------------------------------------------------------------------- #
def _ns(**kw: Any) -> SimpleNamespace:
    return SimpleNamespace(**kw)


def _chunk(*, content: str | None = None, reasoning: str | None = None,
           tool_calls: list[Any] | None = None, cid: str | None = None) -> SimpleNamespace:
    delta = _ns(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    return _ns(choices=[_ns(delta=delta)], usage=None, id=cid)


def _usage_chunk(usage: Any, cid: str | None) -> SimpleNamespace:
    if usage is None:
        mapped = None
    else:
        mapped = _ns(
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            prompt_tokens=getattr(usage, "input_tokens", 0) or 0,
            completion_tokens=getattr(usage, "output_tokens", 0) or 0,
        )
    return _ns(choices=[], usage=mapped, id=cid)


def _tc(index: int, *, cid: str | None = None, name: str | None = None,
        args: str | None = None) -> SimpleNamespace:
    return _ns(index=index, id=cid, function=_ns(name=name, arguments=args))


def iter_chat_chunks(client: Any, payload: dict[str, Any]) -> Iterator[SimpleNamespace]:
    """调用 Responses 流，产出与 chat.completions 等价的 chunk 序列。"""
    stream = client.responses.create(**payload)
    for ev in stream:
        et = getattr(ev, "type", None)

        if et == "response.output_text.delta":
            yield _chunk(content=getattr(ev, "delta", "") or "")

        elif et in ("response.reasoning_text.delta", "response.reasoning_summary_text.delta"):
            yield _chunk(reasoning=getattr(ev, "delta", "") or "")

        elif et == "response.output_item.added":
            item = getattr(ev, "item", None)
            if item is not None and getattr(item, "type", None) == "function_call":
                idx = getattr(ev, "output_index", 0)
                cid = getattr(item, "call_id", None) or getattr(item, "id", None)
                yield _chunk(tool_calls=[_tc(idx, cid=cid, name=getattr(item, "name", None) or "", args="")])

        elif et == "response.function_call_arguments.delta":
            idx = getattr(ev, "output_index", 0)
            yield _chunk(tool_calls=[_tc(idx, args=getattr(ev, "delta", "") or "")])

        elif et in ("response.completed", "response.incomplete"):
            resp = getattr(ev, "response", None)
            usage = getattr(resp, "usage", None) if resp is not None else None
            yield _usage_chunk(usage, getattr(resp, "id", None))

        elif et in ("response.failed", "error"):
            err = getattr(ev, "response", None) or getattr(ev, "error", None) or ev
            raise RuntimeError(f"Responses API 失败: {err}")
