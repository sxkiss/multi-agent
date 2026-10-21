"""agnes Responses API：function_call.id 必须带 fc_ 前缀。

踩坑记录（2026-10-21）：
    agnes 上游的 ResponseInput 是 untagged enum，反序列化时对 function_call.id
    有格式要求——必须以 ``fc_`` 开头。BT 历史里的 tool_call id 是 OpenAI 风格
    的 ``call_xxx``，原样透传会触发::

        Invalid JSON data: Failed to deserialize the JSON body into the target type:
        input: data did not match any variant of untagged enum ResponseInput

    agnes 返回 400，sub2api / ccload 把它包装成 500，表现为「多轮对话报 500」。

    实测矩阵（直连 api.agnes-ai.cn/v1/responses）：
        id="fc_1"       -> 200
        id="fc_abc"     -> 200
        id="fc_"        -> 200
        id="call_1"     -> 400  ← BT 旧行为
        id="FC_abc"     -> 400  （大小写敏感）
        无 id           -> 400
    function_call_output.id 无前缀要求（带不带都 200）。
"""

from __future__ import annotations

from chat_client.responses_client import build_request

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


def _fc_ids(payload: dict) -> list[str]:
    return [it["id"] for it in payload["input"] if it.get("type") == "function_call"]


def _fc_items(payload: dict) -> list[dict]:
    return [it for it in payload["input"] if it.get("type") == "function_call"]


def _history(call_id: str) -> list[dict]:
    return [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "北京天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "晴 25 度"},
        {"role": "user", "content": "那上海呢"},
    ]


def test_openai_style_call_id_is_rewritten_with_fc_prefix():
    """OpenAI 风格 call_xxx 必须被改写成 fc_ 前缀，否则 agnes 400。"""
    payload = build_request(_history("call_00_ABC123"), TOOLS, "agnes-2.5-flash")
    ids = _fc_ids(payload)
    assert ids, "应当生成 function_call item"
    assert all(i.startswith("fc_") for i in ids), f"id 缺少 fc_ 前缀: {ids}"


def test_call_id_field_keeps_original_value():
    """call_id 必须保持原始值，否则 function_call_output 配对不上。"""
    payload = build_request(_history("call_00_ABC123"), TOOLS, "agnes-2.5-flash")
    fc = _fc_items(payload)[0]
    assert fc["call_id"] == "call_00_ABC123"

    outputs = [it for it in payload["input"] if it.get("type") == "function_call_output"]
    assert outputs and outputs[0]["call_id"] == "call_00_ABC123"
    # 配对闭环：fc.call_id 能在 output 里找到
    assert fc["call_id"] in {o["call_id"] for o in outputs}


def test_existing_fc_prefix_id_is_kept_verbatim():
    """已经带 fc_ 前缀的 id 原样保留，不要二次改写。"""
    payload = build_request(_history("fc_5"), TOOLS, "agnes-2.5-flash")
    assert _fc_ids(payload) == ["fc_5"]


def test_generated_ids_do_not_collide_with_existing():
    """生成的新 id 不能和历史里已有的 fc_N 撞号。"""
    msgs = [
        {"role": "user", "content": "北京天气"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "fc_1", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "fc_1", "content": "晴"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_new", "type": "function",
                 "function": {"name": "get_weather", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_new", "content": "雨"},
        {"role": "user", "content": "继续"},
    ]
    payload = build_request(msgs, TOOLS, "agnes-2.5-flash")
    ids = _fc_ids(payload)
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"id 重复: {ids}"
    assert all(i.startswith("fc_") for i in ids)


def test_missing_tool_call_id_still_gets_fc_prefix():
    """历史里 tool_call 没有 id 时，生成的兜底 id 也必须是 fc_ 前缀。"""
    msgs = [
        {"role": "user", "content": "北京天气"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "", "content": "晴"},
        {"role": "user", "content": "继续"},
    ]
    payload = build_request(msgs, TOOLS, "agnes-2.5-flash")
    ids = _fc_ids(payload)
    assert ids and all(i.startswith("fc_") for i in ids), ids
