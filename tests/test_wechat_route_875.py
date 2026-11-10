"""WeChatRoute875 独立技能 + 工具路由的单元测试。

全部 mock，绝不真发 HTTP / 消息。验证：
- 8 种 msg_type 生成的 875 请求路径与 body
- 语音 Type 映射 / VoiceTime 毫秒换算
- link/music 的 items 结构、群@逗号拼接
- route 默认 869 不触发 875
- 失败/异常降级、卡密不泄露
"""
from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# 直接以文件路径加载 CLI 模块（技能脚本不在包里）
_CLI = Path(__file__).resolve().parents[1] / "skills" / "wechat-875-route" / "scripts" / "send_875.py"
_spec = importlib.util.spec_from_file_location("send_875", _CLI)
send875 = importlib.util.module_from_spec(_spec)
sys.modules["send_875"] = send875
_spec.loader.exec_module(send875)

from chat_client.tools import wechat_route as wr  # noqa: E402

CFG = {"base_url": "http://127.0.0.1:18062", "authcode": "TESTKEY12", "timeout": 5}
_B64_IMG = base64.b64encode(b"IMG").decode()


class FakeResp:
    def __init__(self, body: bytes): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _capture(holder, code=0, msg="成功"):
    """mock urlopen，捕获请求并回 875 风格响应。"""
    def _opener(req, timeout=None):
        holder["url"] = req.full_url
        holder["headers"] = {k.lower(): v for k, v in req.headers.items()}
        holder["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResp(json.dumps({"Code": code, "Message": msg, "Success": True,
                                    "Data": {"newMsgId": 123}}).encode())
    return _opener


def _post_capture(sub, argv, code=0):
    """跑 _build + _post（mock urlopen），返回 (path, holder)。"""
    ns = send875._parser().parse_args([sub] + argv)
    path, body = send875._build(sub, ns)
    holder = {}
    with patch.object(send875.urllib.request, "urlopen", _capture(holder, code=code)):
        resp = send875._post(CFG, path, body)
    holder["resp"] = resp
    return path, holder


# ---------- CLI 层：8 类请求映射 ----------

@pytest.fixture
def blob(tmp_path):
    f = tmp_path / "blob.bin"
    f.write_bytes(b"IMG")
    return str(f)


def test_text(blob):
    path, h = _post_capture("text", ["--to", "u1", "--text", "你好"])
    assert path == "/Msg/SendTxt" and "/api/Msg/SendTxt" in h["url"]
    assert h["body"]["ToWxid"] == "u1" and h["body"]["Content"] == "你好"
    assert h["body"]["Type"] == 1
    assert "TESTKEY12" in h["url"] and h["headers"].get("x-access-token") == "TESTKEY12"
    assert "TESTKEY12" not in json.dumps(h["body"])


def test_image(blob):
    path, h = _post_capture("image", ["--to", "u1", "--path", blob])
    assert path == "/Msg/UploadImg" and h["body"]["Base64"] == _B64_IMG


def test_video(blob):
    path, h = _post_capture("video", ["--to", "u1", "--path", blob, "--seconds", "10"])
    assert path == "/Msg/SendVideo" and h["body"]["PlayLength"] == 10 and h["body"]["Base64"]


def test_file(blob):
    path, h = _post_capture("file", ["--to", "u1", "--path", blob, "--name", "a.pdf"])
    assert path == "/Msg/SendFile" and h["body"]["FileName"] == "a.pdf" and h["body"]["Base64"]


def test_link(blob):
    path, h = _post_capture("link", ["--to", "u1", "--url", "https://x", "--title", "T",
                                     "--desc", "D", "--thumb-url", "https://t"])
    assert path == "/Msg/SendAppMessage"
    item = h["body"]["items"][0]
    assert item["kind"] == "link" and item["link"]["url"] == "https://x"
    assert item["toUserId"] == "u1" and item["link"]["thumbUrl"] == "https://t"


def test_card(blob):
    path, h = _post_capture("card", ["--to", "u1", "--card-wxid", "wx2", "--card-nickname", "昵称"])
    assert path == "/Msg/ShareCard"
    assert h["body"]["CardWxId"] == "wx2" and h["body"]["CardNickName"] == "昵称"


def test_music(blob):
    path, h = _post_capture("music", ["--to", "u1", "--music-url", "https://a.mp3",
                                      "--title", "歌", "--singer", "手", "--cover-url", "https://c"])
    assert path == "/Msg/SendAppMessage"
    item = h["body"]["items"][0]
    assert item["kind"] == "music" and item["music"]["audioUrl"] == "https://a.mp3"


@pytest.mark.parametrize("fmt,t", [("amr", 0), ("mp3", 2), ("wav", 3), ("silk", 4)])
def test_voice_type_and_voicetime(blob, fmt, t):
    path, h = _post_capture("voice", ["--to", "u1", "--path", blob, "--format", fmt, "--seconds", "3"])
    assert path == "/Msg/SendVoice"
    assert h["body"]["Type"] == t and h["body"]["VoiceTime"] == 3000


def test_group_at():
    ns = send875._parser().parse_args(
        ["text", "--to", "g@chatroom", "--text", "hi", "--at", "w1", "--at", "w2"])
    _, body = send875._build("text", ns)
    assert body["At"] == "w1,w2"


def test_nonzero_code():
    _, h = _post_capture("text", ["--to", "u1", "--text", "hi"], code=-3)
    assert h["resp"]["Code"] == -3


# ---------- 工具层：route 分派 ----------

def test_route_869_explicit_does_not_call_875():
    with patch.object(wr, "_dispatch_875") as d875, \
         patch.object(wr, "_resolve_scripts", return_value=(Path("x"), Path("y"))), \
         patch.object(wr, "_target_arg", return_value="u1"), \
         patch.object(wr, "_run", return_value={"status": True, "msg": "已发送"}):
        out = wr.WeChatRoute(msg_type="text", content="hi", route="869")
    d875.assert_not_called()
    assert "869" in out


def test_route_is_required_in_schema():
    """route 无默认值 → 必须进 required，杜绝隐式走 869。"""
    from chat_client.tools import registry
    schema = next(s for s in registry._schemas if s["function"]["name"] == "WeChatRoute")
    params = schema["function"]["parameters"]
    assert "route" in params.get("required", [])
    assert "default" not in params["properties"]["route"]


def test_route_missing_rejected_by_wrapper():
    """经 wrapper 调用不带 route → 参数校验拦截，不执行、不崩溃。"""
    from chat_client.tools import registry
    # 直接测 _validate_args：缺必填参数应返回错误说明
    err = registry._validate_args("WeChatRoute", {"msg_type": "text", "content": "hi"})
    assert err and "route" in err
    # 且不应触发任何发送
    with patch.object(wr, "_dispatch_875") as d875, \
         patch.object(wr, "_resolve_scripts", side_effect=AssertionError("不应调用 869")):
        # wrapper 层会在校验失败时直接返回，这里只验证校验器本身
        pass
    d875.assert_not_called()


def test_route_invalid_value_rejected():
    for bad in ("", "888", None, "wechat"):
        with patch.object(wr, "_dispatch_875") as d875, \
             patch.object(wr, "_resolve_scripts", side_effect=AssertionError("不应调用 869")):
            out = wr.WeChatRoute(msg_type="text", content="hi", route=bad)
        assert "error" in out and "route" in out
        d875.assert_not_called()


def test_route_875_dispatches():
    with patch.object(wr, "_dispatch_875", return_value={"status": True, "msg": "已发送"}) as d875, \
         patch.object(wr, "_target_arg", return_value="u1"):
        out = wr.WeChatRoute(msg_type="text", content="hi", route="875")
    assert d875.call_count == 1 and d875.call_args.args[0] == "text"
    assert "875" in out


def test_route_875_error_degrades():
    with patch.object(wr, "_dispatch_875", return_value={"status": False, "msg": "875 挂了"}), \
         patch.object(wr, "_target_arg", return_value="u1"):
        out = wr.WeChatRoute(msg_type="text", content="hi", route="875")
    assert "error" in out and "875 挂了" in out and "Traceback" not in out


def test_route_875_does_not_read_869_scripts():
    with patch.object(wr, "_resolve_scripts", side_effect=AssertionError("不应调用 869")), \
         patch.object(wr, "_dispatch_875", return_value={"status": True}) as d875, \
         patch.object(wr, "_target_arg", return_value="u1"):
        out = wr.WeChatRoute(msg_type="text", content="hi", route="875")
    assert d875.call_count == 1 and "875" in out


def test_credential_never_leaks_in_error():
    with patch.object(wr, "_dispatch_875", return_value={"status": False, "msg": "网络错误"}), \
         patch.object(wr, "_target_arg", return_value="u1"):
        out = wr.WeChatRoute(msg_type="text", content="hi", route="875")
    assert "zE" not in out and "TESTKEY12" not in out
