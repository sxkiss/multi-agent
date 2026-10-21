#!/usr/bin/env python3
"""Anthropic Messages API 适配层。

把 BT 主循环认识的 OpenAI chat 风格 messages / tools 转换成 Anthropic
Messages API（/v1/messages）请求体，并把流式事件翻译回 chat.delta 形状
（choices / usage / tool_calls），使主循环零改动即可跑通。

与 responses_client.py 同思路：协议差异全部收敛在本模块，agent 主循环的
重复熔断 / 上下文管理 / SSE 事件 / 工具执行逻辑完全不变。

关键映射：
- 工具定义  {type:function, function:{...}}  -> {name, description, input_schema}
- assistant tool_calls -> content 里的 tool_use 块（保留 id）
- tool 消息  tool_call_id -> content 里的 tool_result 块
- 流事件    thinking / text / input_json  -> reasoning_content / content / tool_calls
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Iterator

import httpx

from chat_client.api_retry import AnthropicAPIError


# --------------------------------------------------------------------------- #
# 文本 / 内容提取
# --------------------------------------------------------------------------- #
def _text_of(content: Any) -> str:
    """把 chat 的 content（str / list[part] / None）压平成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text") or ""))
                elif p.get("type") == "image_url":
                    url = p.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url")
                    if url:
                        parts.append(f"[图片: {url}]")
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _user_content_blocks(content: Any) -> list[dict[str, Any]]:
    """user 消息 content -> Anthropic content 块（文本 / 图片 URL）。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for p in content:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if ptype == "image_url":
            url = p.get("image_url")
            if isinstance(url, dict):
                url = url.get("url")
            if url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            blocks.append({"type": "text", "text": str(p.get("text") or "")})
    return blocks or [{"type": "text", "text": ""}]


# --------------------------------------------------------------------------- #
# 工具定义转换
# --------------------------------------------------------------------------- #
def convert_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """OpenAI chat 工具定义 -> Anthropic tools（input_schema）。"""
    out: list[dict[str, Any]] = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            nt: dict[str, Any] = {
                "name": fn.get("name") or "",
                "description": fn.get("description") or "",
            }
            params = fn.get("parameters")
            nt["input_schema"] = params if isinstance(params, dict) else {"type": "object", "properties": {}}
            out.append(nt)
        else:
            # 已经是 Anthropic 形状，原样透传
            out.append(t)
    return out


# --------------------------------------------------------------------------- #
# 消息历史转换
# --------------------------------------------------------------------------- #
def _assistant_content_blocks(m: dict[str, Any]) -> list[dict[str, Any]]:
    """assistant 消息 -> Anthropic content 块（文本 + tool_use）。"""
    blocks: list[dict[str, Any]] = []
    txt = m.get("content")
    if isinstance(txt, str) and txt:
        blocks.append({"type": "text", "text": txt})
    elif isinstance(txt, list):
        for p in txt:
            if isinstance(p, dict) and p.get("type") in ("text", "output_text", "input_text"):
                blocks.append({"type": "text", "text": str(p.get("text") or "")})
    for tc in m.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if not isinstance(args, str):
            args = json.dumps(args or {}, ensure_ascii=False)
        # 空字符串或非 JSON 串（例如被截断的参数）不能 json.loads，否则整个
        # 请求体组装会因单条历史消息而崩，导致该 session 永远 500。兜底为空对象，
        # 让主循环后续按"非法参数"流程处理（或模型能自行纠正）。
        try:
            parsed_args = json.loads(args) if args else {}
        except json.JSONDecodeError:
            parsed_args = {}
        cid = tc.get("id") or ""
        blocks.append({
            "type": "tool_use",
            "id": cid,
            "name": fn.get("name") or "",
            "input": parsed_args,
        })
    return blocks


def _merge_adjacent_same_role(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并相邻同 role 的消息（Anthropic 协议要求 user/assistant 严格交替）。

    两类典型场景：
    1. 一轮并行调用多个工具时，每个 tool 消息会各自转成一条 user(tool_result)，
       连续多条 user 会被上游拒绝（表现为多轮对话 500「不符合 API 协议」）。
       Anthropic 要求所有 tool_result 放在同一条 user 消息的 content 数组里。
    2. 历史里连续两条 user 文本消息（如用户连发、系统注入），同样需要合并。

    合并时保持块顺序，content 统一为块数组。
    """
    merged: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        blocks = content if isinstance(content, list) else (
            [{"type": "text", "text": str(content or "")}] if content else []
        )
        if merged and merged[-1].get("role") == role:
            prev = merged[-1]
            prev_blocks = prev.get("content")
            if not isinstance(prev_blocks, list):
                prev_blocks = [{"type": "text", "text": str(prev_blocks or "")}] if prev_blocks else []
            prev["content"] = prev_blocks + blocks
        else:
            merged.append({"role": role, "content": list(blocks)})
    return merged


def build_request(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    model: str,
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
    max_output_tokens: int | None = None,
) -> dict[str, Any]:
    """把 chat 历史 + 工具定义组装成 Anthropic Messages 请求体。"""
    system_blocks: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            txt = _text_of(m.get("content"))
            if txt:
                system_blocks.append(txt)
            continue
        if role == "user":
            anthropic_messages.append({"role": "user", "content": _user_content_blocks(m.get("content"))})
            continue
        if role == "assistant":
            blocks = _assistant_content_blocks(m)
            if blocks:
                anthropic_messages.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            cid = m.get("tool_call_id") or ""
            output = _text_of(m.get("content"))
            # Anthropic 协议要求 tool_result 放在 user 角色的 content 块里，
            # 不能放在 assistant 消息中。
            anthropic_messages.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": cid,
                "content": output,
            }]})
            continue

    # 关键：合并相邻同 role 消息。并行工具调用的多条 tool_result 必须合并进
    # 同一条 user 消息，否则 Anthropic 会因「消息未交替」拒绝整个请求（500）。
    anthropic_messages = _merge_adjacent_same_role(anthropic_messages)

    payload: dict[str, Any] = {
        "model": model,
        "messages": anthropic_messages,
        "stream": True,
        "max_tokens": max_output_tokens or 4096,
    }
    if system_blocks:
        payload["system"] = "\n\n".join(system_blocks)
    ct = convert_tools(tools)
    if ct:
        payload["tools"] = ct
    if temperature is not None:
        payload["temperature"] = temperature
    if top_p is not None:
        payload["top_p"] = top_p
    # Anthropic 的 thinking 参数：仅当显式给出 reasoning_effort 才开启。
    # budget_tokens 必须小于 max_tokens，否则上游直接 400；这里取二者较小值并留 1k 余量。
    if reasoning_effort:
        budget = max(1024, min((max_output_tokens or 4096) - 1024, 32768))
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return payload


# --------------------------------------------------------------------------- #
# httpx 流式调用 /v1/messages
# --------------------------------------------------------------------------- #
def stream_messages(
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    *,
    default_headers: dict[str, str] | None = None,
    timeout: float = 120,
) -> Iterator[dict[str, Any]]:
    """调用 Anthropic Messages 流式接口，逐条产出 SSE 的 JSON 事件。

    SSE 行格式：event: <type>\ndata: <json>\n\n
    这里只关心 data 的 JSON（事件类型已包含在 data.type 里）。
    """
    url = base_url.rstrip("/") + "/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    headers.update(default_headers or {})
    try:
        with httpx.stream(
            "POST", url, headers=headers, json=payload, timeout=timeout,
        ) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", errors="replace")[:2000]
                raise AnthropicAPIError(
                    f"Anthropic API HTTP {resp.status_code}: {body}",
                    status_code=resp.status_code,
                )
            for line in resp.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if not data:
                    continue
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue
    except httpx.HTTPError as e:
        raise AnthropicAPIError(f"Anthropic 请求失败: {e}") from e


# --------------------------------------------------------------------------- #
# 流事件适配：Anthropic 事件 -> chat.delta 形状
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


def iter_anthropic_chunks(events: Iterator[dict[str, Any]]) -> Iterator[SimpleNamespace]:
    """把 Anthropic SSE 的 JSON 事件翻译成 chat.completions 等价的 chunk。

    events: stream_messages() 产出的 data JSON 字典序列。
    """
    tool_uses: dict[int, dict[str, Any]] = {}
    try:
        for ev in events:
            et = ev.get("type", "")
            if et == "message_start":
                msg = ev.get("message") or {}
                usage = msg.get("usage")
                if usage:
                    yield _usage_chunk(usage, msg.get("id"))
                continue
            if et == "content_block_start":
                block = ev.get("content_block") or {}
                index = ev.get("index", 0)
                btype = block.get("type", "")
                if btype == "tool_use":
                    tool_uses[index] = {
                        "id": block.get("id") or "",
                        "name": block.get("name") or "",
                        "args": "",
                    }
                    cid = tool_uses[index]["id"]
                    yield _chunk(tool_calls=[_tc(index, cid=cid, name=tool_uses[index]["name"], args="")])
                elif btype == "thinking":
                    yield _chunk(reasoning=block.get("thinking", "") or "")
                continue
            if et == "content_block_delta":
                delta = ev.get("delta") or {}
                dtype = delta.get("type", "")
                if dtype == "text_delta":
                    yield _chunk(content=delta.get("text", "") or "")
                elif dtype == "thinking_delta":
                    yield _chunk(reasoning=delta.get("thinking", "") or "")
                elif dtype == "input_json_delta":
                    index = ev.get("index", 0)
                    tu = tool_uses.setdefault(index, {"id": "", "name": "", "args": ""})
                    tu["args"] += delta.get("partial_json", "") or ""
                    # 只传 args 增量，不带 name/id，避免主循环把工具名重复累积成 "BashBash"
                    yield _chunk(tool_calls=[_tc(index, cid=None, name="", args=delta.get("partial_json", "") or "")])
                continue
            if et == "message_delta":
                usage = ev.get("usage")
                if usage:
                    yield _usage_chunk(usage, None)
                continue
            if et == "message_stop":
                break
    except Exception as e:
        raise AnthropicAPIError(f"Anthropic 流解析失败: {e}") from e


# 保持与 responses_client 相同的命名习惯
iter_chat_chunks = iter_anthropic_chunks
