"""agnes Responses API：function_call 的 id / call_id 必须带 fc_ 前缀。

踩坑记录（2026-10-21）：
    agnes 上游的 ResponseInput 是 untagged enum，反序列化时对 function_call
    有格式要求——``id`` 必须以 ``fc_`` 开头。BT 历史里的 tool_call id 是
    OpenAI 风格的 ``call_xxx``，原样透传会触发::

        Invalid JSON data: Failed to deserialize the JSON body into the target type:
        input: data did not match any variant of untagged enum ResponseInput

    agnes 返回 400，sub2api / ccload 把它包装成 500，表现为「多轮对话报 500」。

实测矩阵（直连 api.agnes-ai.cn/v1/responses，仅改 id）：
        id="fc_1"       -> 200
        id="fc_abc"     -> 200
        id="call_1"     -> 400  ← BT 旧行为
        id="FC_abc"     -> 400  （大小写敏感）
        无 id           -> 400

为什么 call_id 也必须规范化：
    BT → ccload(3333) → sub2api(3000) → agnes 链路有两次协议转换。
    ccload 把 responses 转成 chat 时用的是 ``call_id``，sub2api 再把 chat
    转回 responses 时又把这个值当成 ``id``。若 call_id 仍是 ``call_xxx``，
    BT 侧的 id 修复会被中间层完全抵消（实测：仍报同一 400/500）。
    因此 id 与 call_id 必须同时规范化为 fc_。
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


def _fc_items(payload: dict) -> list[dict]:
    return [it for it in payload["input"] if it.get("type") == "function_call"]


def _fco_items(payload: dict) -> list[dict]:
    return [it for it in payload["input"] if it.get("type") == "function_call_output"]


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
    fcs = _fc_items(payload)
    assert fcs, "应当生成 function_call item"
    for fc in fcs:
        assert fc["id"].startswith("fc_"), f"id 缺少 fc_ 前缀: {fc['id']}"
        assert fc["call_id"].startswith("fc_"), f"call_id 缺少 fc_ 前缀: {fc['call_id']}"


def test_call_id_matches_id_so_chain_conversion_survives():
    """id 与 call_id 必须一致——ccload/sub2api 两次转换都靠这个值。"""
    payload = build_request(_history("call_00_ABC123"), TOOLS, "agnes-2.5-flash")
    fc = _fc_items(payload)[0]
    assert fc["id"] == fc["call_id"], "id 与 call_id 不一致会导致中间层转换后失配"


def test_function_call_output_pairs_with_rewritten_call_id():
    """tool 消息的 call_id 必须同步改写成同一个 fc_ 值，否则配对断裂。"""
    payload = build_request(_history("call_00_ABC123"), TOOLS, "agnes-2.5-flash")
    fc = _fc_items(payload)[0]
    fcos = _fco_items(payload)
    assert fcos, "应当生成 function_call_output item"
    assert fcos[0]["call_id"] == fc["call_id"], (
        f"配对断裂: fc.call_id={fc['call_id']} fco.call_id={fcos[0]['call_id']}"
    )


def test_existing_fc_prefix_id_is_kept_verbatim():
    """已经带 fc_ 前缀的 id 原样保留，不要二次改写。"""
    payload = build_request(_history("fc_5"), TOOLS, "agnes-2.5-flash")
    fc = _fc_items(payload)[0]
    assert fc["id"] == "fc_5"
    assert fc["call_id"] == "fc_5"


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
    ids = [fc["id"] for fc in _fc_items(payload)]
    assert len(ids) == 2
    assert len(set(ids)) == 2, f"id 重复: {ids}"
    assert "fc_1" in ids, "历史已有的 fc_1 应被保留"
    assert all(i.startswith("fc_") for i in ids)


def test_same_raw_id_maps_to_same_fc_id():
    """同一原始 id 在多轮里必须稳定映射到同一个 fc_ 值。"""
    msgs = [
        {"role": "user", "content": "北京天气"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_same", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_same", "content": "晴"},
        {"role": "user", "content": "再查一次"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "call_same", "type": "function",
                         "function": {"name": "get_weather", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_same", "content": "雨"},
        {"role": "user", "content": "继续"},
    ]
    payload = build_request(msgs, TOOLS, "agnes-2.5-flash")
    ids = [fc["id"] for fc in _fc_items(payload)]
    assert len(set(ids)) == 1, f"同一原始 id 被映射成了多个值: {ids}"


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
    fcs = _fc_items(payload)
    assert fcs
    for fc in fcs:
        assert fc["id"].startswith("fc_"), fc["id"]
        assert fc["call_id"].startswith("fc_"), fc["call_id"]
