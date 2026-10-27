"""Feishu typing reaction 测试。

覆盖：
- _default_constraints 返回 typing_keepalive_interval=5.0
- _add_reaction 成功时记录 reaction_id 到 _typing_reactions
- _send_typing_indicator 触发 reaction 并修剪 LRU
- _remove_typing_reactions 删除所有 tracked reactions
- _clear_typing_indicator 转发到 _remove_typing_reactions
- _prune_typing_reactions 按 LRU 淘汰最旧条目
"""

from __future__ import annotations

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import platform_ext  # noqa: E402


class _FakeResp:
    """aiohttp response stand-in supporting async context manager."""

    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self) -> str:
        return self._body


class _FakeSession:
    """aiohttp.ClientSession stand-in; records post/delete calls."""

    def __init__(self, post_resp=None, delete_resp=None):
        self.post_calls: list[tuple[str, dict, dict]] = []
        self.delete_calls: list[tuple[str, dict]] = []
        self._post_resp = post_resp or _FakeResp(200, '{"data": {"id": "rid123"}}')
        self._delete_resp = delete_resp or _FakeResp(200, "")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._post_resp

    def delete(self, url, **kwargs):
        self.delete_calls.append((url, kwargs))
        return self._delete_resp


def _make_channel():
    from octop_gateway.channels.feishu import FeishuChannel

    processor = mock.AsyncMock()
    config = mock.MagicMock()
    config.app_id = "cli_test"
    config.app_secret = "secret"
    ch = FeishuChannel(processor, config, channel_id="feishu-test")
    return ch


@pytest.mark.asyncio
async def test_default_constraints_has_typing_interval():
    from octop_gateway.channels.feishu import (
        FeishuChannel,
        _TYPING_KEEPALIVE_INTERVAL,
    )

    ch = _make_channel()
    cons = ch._default_constraints()
    assert cons.typing_keepalive_interval == _TYPING_KEEPALIVE_INTERVAL
    assert cons.typing_keepalive_interval == 5.0


@pytest.mark.asyncio
async def test_add_reaction_records_reaction_id():
    from octop_gateway.channels.feishu import FeishuChannel

    ch = _make_channel()
    session = _FakeSession()
    with mock.patch.object(
        FeishuChannel, "_refresh_token", new=mock.AsyncMock(return_value="tok")
    ), mock.patch("octop_gateway.channels.feishu.aiohttp.ClientSession", return_value=session):
        rid = await ch._add_reaction("msg1", "Typing")
    assert rid == "rid123"
    assert ch._typing_reactions == {"msg1": "rid123"}
    assert len(session.post_calls) == 1
    assert "/messages/msg1/reactions" in session.post_calls[0][0]


@pytest.mark.asyncio
async def test_add_reaction_failure_records_nothing():
    from octop_gateway.channels.feishu import FeishuChannel

    ch = _make_channel()
    session = _FakeSession(post_resp=_FakeResp(400, "bad"))
    with mock.patch.object(
        FeishuChannel, "_refresh_token", new=mock.AsyncMock(return_value="tok")
    ), mock.patch("octop_gateway.channels.feishu.aiohttp.ClientSession", return_value=session):
        rid = await ch._add_reaction("msg1", "Typing")
    assert rid is None
    assert ch._typing_reactions == {}


@pytest.mark.asyncio
async def test_send_typing_indicator_refreshes_and_prunes():
    import asyncio

    from octop_gateway.channels.feishu import FeishuChannel
    from octop_gateway.models import ChannelSubject

    ch = _make_channel()
    ch._main_loop = asyncio.get_running_loop()
    subject = ChannelSubject(
        subject_id="user1",
        metadata={"message_id": "msg1", "to_handle": "user1"},
    )
    with mock.patch.object(
        FeishuChannel,
        "_add_reaction_async",
        autospec=True,
    ) as mock_add:
        await ch._send_typing_indicator(subject)
    mock_add.assert_called_once_with(ch, "msg1", "Typing")


@pytest.mark.asyncio
async def test_remove_typing_reactions_deletes_all():
    from octop_gateway.channels.feishu import FeishuChannel
    from octop_gateway.models import ChannelSubject

    ch = _make_channel()
    ch._typing_reactions.update({"msg1": "rid1", "msg2": "rid2"})
    session = _FakeSession()
    with mock.patch.object(
        FeishuChannel, "_refresh_token", new=mock.AsyncMock(return_value="tok")
    ), mock.patch("octop_gateway.channels.feishu.aiohttp.ClientSession", return_value=session):
        subject = ChannelSubject(subject_id="user1", metadata={"message_id": "msg1"})
        await ch._remove_typing_reactions(subject)
    assert ch._typing_reactions == {}
    assert len(session.delete_calls) == 2
    assert "/messages/msg1/reactions/rid1" in session.delete_calls[0][0]
    assert "/messages/msg2/reactions/rid2" in session.delete_calls[1][0]


@pytest.mark.asyncio
async def test_clear_typing_indicator_forwards_to_remove():
    from octop_gateway.channels.feishu import FeishuChannel
    from octop_gateway.models import ChannelSubject

    ch = _make_channel()
    ch._typing_reactions.update({"msg1": "rid1"})
    with mock.patch.object(
        FeishuChannel,
        "_remove_typing_reactions",
        new=mock.AsyncMock(return_value=None),
    ) as mock_remove:
        subject = ChannelSubject(subject_id="user1", metadata={"message_id": "msg1"})
        await ch._clear_typing_indicator(subject)
    mock_remove.assert_awaited_once_with(subject)


@pytest.mark.asyncio
async def test_prune_typing_reactions_evicts_oldest():
    from octop_gateway.channels.feishu import FeishuChannel, _TYPING_REACTION_MAX_SIZE

    ch = _make_channel()
    for i in range(_TYPING_REACTION_MAX_SIZE + 10):
        ch._typing_reactions[f"msg{i}"] = f"rid{i}"
    ch._prune_typing_reactions()
    assert len(ch._typing_reactions) == _TYPING_REACTION_MAX_SIZE
    assert "msg0" not in ch._typing_reactions
    assert f"msg{_TYPING_REACTION_MAX_SIZE + 9}" in ch._typing_reactions


@pytest.mark.asyncio
async def test_add_reaction_sends_correct_payload():
    from octop_gateway.channels.feishu import FeishuChannel

    ch = _make_channel()
    session = _FakeSession()
    with mock.patch.object(
        FeishuChannel, "_refresh_token", new=mock.AsyncMock(return_value="tok")
    ), mock.patch("octop_gateway.channels.feishu.aiohttp.ClientSession", return_value=session):
        await ch._add_reaction("msgX", "THUMBSUP")
    _, kwargs = session.post_calls[0]
    assert kwargs["json"] == {"reaction_type": {"emoji_type": "THUMBSUP"}}