"""统一媒体发送工具 SendMedia 测试：会话解析、类型推断、通道定位、端到端发送。"""
import sys
import os
import base64

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "octop-gateway"
))

_TMP_PNG = "/tmp/sendmedia_test.png"


def _setup_tmp_png() -> str:
    if not os.path.exists(_TMP_PNG):
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
        with open(_TMP_PNG, "wb") as f:
            f.write(png)
    return _TMP_PNG


def test_guess_media_type():
    from chat_client.tools.gateway_media_send import _guess_media_type
    assert _guess_media_type("/tmp/a.png") == "image"
    assert _guess_media_type("/tmp/b.mp4") == "video"
    assert _guess_media_type("/tmp/c.mp3") == "audio"
    assert _guess_media_type("/tmp/d.txt") == "file"


def test_session_id_parsing():
    from chat_client.tools.gateway_media_send import _resolve_channel_from_session
    info = _resolve_channel_from_session("gw_weixin_abc123")
    assert info is not None
    assert info["channel_type"] == "weixin"
    assert info["subject_id"] == "abc123"
    assert info["channel_id"] is None  # 无真实通道

    info2 = _resolve_channel_from_session("gw_telegram_123456")
    assert info2 is not None
    assert info2["channel_type"] == "telegram"
    assert info2["subject_id"] == "123456"

    # 非 gw 会话
    assert _resolve_channel_from_session("wx_73e9...") is None
    assert _resolve_channel_from_session("") is None


def test_send_media_missing_file():
    from chat_client.tools.gateway_media_send import SendMedia
    res = SendMedia(path="/tmp/does_not_exist.png")
    assert "error" in res
    assert "文件不存在" in res


def test_send_media_missing_path():
    from chat_client.tools.gateway_media_send import SendMedia
    res = SendMedia(path="")
    assert "error" in res
    assert "缺少参数 path" in res


def test_send_media_no_session_context():
    from chat_client.tools.gateway_media_send import SendMedia
    # 无 job 上下文 → _current_session_id() 返回 None
    res = SendMedia(path=_setup_tmp_png())
    assert "error" in res
    assert "无法定位目标通道" in res or "会话" in res


class _MockChannel:
    def __init__(self):
        self.part = None
        self.subject = None

    async def reply_media(self, subject, part):
        self.subject = subject
        self.part = part


class _MockMgr:
    def __init__(self):
        self.channel = _MockChannel()
        self._loop = None

    def get_channel(self, cid):
        return self.channel


def test_send_media_end_to_end():
    import platform_ext.gateway as gw_mod
    from chat_client.tools.gateway_media_send import _send_media_sync

    mock = _MockMgr()
    gw_mod._manager = mock
    gw_mod._manager_kind_of = {"mock_ch": "weixin"}

    res = _send_media_sync(
        path=_setup_tmp_png(),
        caption="hello",
        media_type="image",
        channel_type="weixin",
        subject_id="wxuser123",
    )
    assert "success" in res
    assert mock.channel.subject.subject_id == "wxuser123"
    assert mock.channel.part is not None
    assert mock.channel.part.type == "image"
    assert mock.channel.part.mime_type == "image/png"
    assert mock.channel.part.data  # base64