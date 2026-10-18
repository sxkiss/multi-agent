"""通道回复链路与工作目录配置测试。

覆盖：
- _extract_sse_data 兼容 BT sse_pack 的裸文本/JSON/int 各种 data 格式；
- 通道配置支持 workspace 字段，发起对话时传给 chat_start；
- 面板 config_fields 含 workspace，_norm_cfg 会剔除 workspace。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------- SSE 解析 ----------

def test_extract_sse_data_bare_text():
    """BT sse_pack 对字符串 data 不 JSON 化，应解析为 message 事件。"""
    from platform_ext.gateway_processor import _extract_sse_data

    out = _extract_sse_data("id: 1\nevent: message\ndata: 你好世界\n\n")
    assert out["event"] == "message"
    assert out["data"]["content"] == "你好世界"


def test_extract_sse_data_bare_text_unescapes_newline():
    """BT sse_pack 把真实换行转成字面 \\n，解析时应还原成真实换行，
    避免通道收到字面 '\\n' 两个字符而不换行（与前端 App.vue 行为对齐）。"""
    from platform_ext.gateway_processor import _extract_sse_data

    # data 行里的 \\n 是字面反斜杠+n（SSE data 行不能含裸换行）
    out = _extract_sse_data("id: 1\nevent: message\ndata: 第一行\\n第二行\\n第三行\n\n")
    assert out["data"]["content"] == "第一行\n第二行\n第三行"
    assert "\n" in out["data"]["content"]


def test_extract_sse_data_json_dict():
    """JSON dict data（如 error 事件）应带 event 字段。"""
    from platform_ext.gateway_processor import _extract_sse_data

    out = _extract_sse_data('id: 2\nevent: error\ndata: {"msg": "出错了"}\n\n')
    assert out["event"] == "error"
    assert out["data"]["msg"] == "出错了"


def test_extract_sse_data_int_no_crash():
    """data 行是纯数字（int）不应崩溃，且内容保留。"""
    from platform_ext.gateway_processor import _extract_sse_data

    out = _extract_sse_data("id: 3\nevent: x\ndata: 42\n\n")
    assert isinstance(out, dict)
    assert out["data"]["content"] == "42"


def test_extract_sse_data_json_str():
    """JSON 字符串 data 应作为消息内容。"""
    from platform_ext.gateway_processor import _extract_sse_data

    out = _extract_sse_data('id: 4\nevent: message\ndata: "hi"\n\n')
    assert out["event"] == "message"
    assert out["data"]["content"] == "hi"


def test_extract_sse_data_message_end_no_data():
    """message_end 事件无 data 行也能识别。"""
    from platform_ext.gateway_processor import _extract_sse_data

    out = _extract_sse_data("id: 5\nevent: message_end\n\n")
    assert out["event"] == "message_end"


def test_extract_sse_data_dict_input():
    """dict 输入原样返回。"""
    from platform_ext.gateway_processor import _extract_sse_data

    src = {"event": "message", "data": {"content": "x"}}
    assert _extract_sse_data(src) is src


# ---------- 工作目录配置 ----------

class _FakeGateway:
    def __init__(self, cfg: dict):
        self._cfg = cfg

    def _gateway_cfg(self):
        return self._cfg


def test_channel_workspace_default_empty():
    from platform_ext.gateway_processor import _channel_workspace

    gw = _FakeGateway({"weixin": {"configured": True}})
    assert _channel_workspace(gw, "weixin") == ""


def test_channel_workspace_reads_config():
    from platform_ext.gateway_processor import _channel_workspace

    gw = _FakeGateway({"weixin": {"configured": True, "workspace": "/data/ws"}})
    assert _channel_workspace(gw, "weixin") == "/data/ws"


def test_norm_cfg_strips_workspace():
    from platform_ext.gateway import _norm_cfg

    norm = _norm_cfg("weixin", {
        "token": "t", "account_id": "a", "workspace": "/data/ws",
    })
    # workspace 是对话元数据，不传给 SDK
    assert "workspace" not in norm
    assert norm["accounts"] == [{"account_id": "a", "token": "t", "base_url": "https://ilinkai.weixin.qq.com"}]


def test_platform_kinds_include_workspace_field():
    from platform_ext.gateway import _build_platform_kinds

    kinds = _build_platform_kinds()
    for kind, meta in kinds.items():
        names = [f.get("name") for f in meta.get("config_fields", [])]
        assert "workspace" in names, f"{kind} config_fields 缺少 workspace 字段"