"""
@input: json, logging, os, subprocess, typing, pathlib
@output: WeChatRoute 工具 — 经 WeChatRoute 消息回复工具向微信会话发送文本/图片/视频/语音/链接/文件/名片
@position: Tools layer — 微信出站通道（WeChatRoute 消息回复工具）
@auto-doc: Update header and folder INDEX.md when this file changes

定位：这是 [WeChatRoute] 会话里主动出站微信的第二条出路（第一条是文本自动回复）。
当模型生成/找到媒体文件，或需要主动发一条文本/链接/文件到微信时，调本工具投递。

工具的发送能力直接复用本地已装技能 wechat-869-media-sender 的 CLI 脚本：
    ~/.hermes/skills/wechat-869-media-sender/scripts/send_869_text.py
    ~/.hermes/skills/wechat-869-media-sender/scripts/send_869_media.py
这两个脚本从 ~/.openclaw/credentials/wechat-869.json 读取 869 baseUrl/key，
无需在 AG 侧持有任何敏感凭据。

发送目标 wxid：
- 默认取当前消息的 chat_id（私聊=对方 wxid；群聊以 @chatroom 结尾）
- 私聊发给私聊对象本身（即当前 chat_id）
- 群聊发到当前群 chat_id，不要误发回私聊，也不要默认发给 boss 本人
- 统一原则：谁在当前会话里触发，就往当前会话发；除非用户明确指定 to_wxid

支持的消息类型：
- text:    纯文本（支持群聊 @）
- image:   图片
- video:   视频（自动封面）
- voice:   语音（amr/wav/mp3，自动转 silk + 超长分片）
- link:    链接卡片
- file:    文件/附件
- card:    联系人名片
- music:   音乐卡片（点歌场景）
"""
import logging
import os
import subprocess
import json
from pathlib import Path
from typing import Any

from . import PROJECT_ROOT, register_tool, _current_session_id
from .base import _xml_response

logger = logging.getLogger(__name__)

# 869 发送脚本路径（优先 hermes，回退 openclaw）
_HERMES_SKILL = Path.home() / ".hermes" / "skills" / "wechat-869-media-sender"
_OPENCLAW_SKILL = Path.home() / ".openclaw" / "skills" / "wechat-869-media-sender"

if _HERMES_SKILL.exists():
    _SKILL_DIR = _HERMES_SKILL
elif _OPENCLAW_SKILL.exists():
    _SKILL_DIR = _OPENCLAW_SKILL
else:
    _SKILL_DIR = _HERMES_SKILL  # 默认，文件不存在时由 _resolve_scripts 报错

_SEND_TEXT = _SKILL_DIR / "scripts" / "send_869_text.py"
_SEND_MEDIA = _SKILL_DIR / "scripts" / "send_869_media.py"

# 占位串：模型可能把 None 序列化成 "null"/"None" 等，语义上是"未指定"，
# 应回落自动取当前会话，而不是当真实 wxid 发。
_PLACEHOLDER_KEYS = {"", "null", "none", "undefined", "nil", "n/a", "unknown"}


def _resolve_scripts() -> tuple[Path, Path]:
    """确认发送脚本存在，返回 (text_script, media_script)。"""
    if not _SEND_TEXT.exists() or not _SEND_MEDIA.exists():
        raise FileNotFoundError(
            f"未找到 869 发送脚本（请在 ~/.hermes/skills/wechat-869-media-sender 安装该技能）。"
            f"期望路径：{_SEND_TEXT} / {_SEND_MEDIA}"
        )
    return _SEND_TEXT, _SEND_MEDIA


def _default_target_wxid() -> str:
    """从当前会话上下文取默认接收方 wxid。"""
    sid = (_current_session_id() or "").strip()
    if not sid:
        return ""
    # session_id 格式可能为 wx_<hash>、 chatroom@<...>，或带前缀。
    # 直接返回，由调用方决定是否用。
    return sid


def _run(cmd: list[str], timeout: int = 120) -> dict:
    """执行发送脚本，返回解析后的 JSON 结果。

    869 脚本在 verify_send_result 检测到失败时抛出 RuntimeError 并返回非零退出码，
    stdout 中会输出错误 JSON（含 msg 字段）。本函数提取最具体的失败原因，
    避免把 "exit_code=1" 这类噪声当作错误信息返回给模型。
    """
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {"status": False, "msg": f"发送超时（>{timeout}s）"}
    except Exception as exc:
        return {"status": False, "msg": f"调用 869 脚本异常: {exc}"}

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    payload = None
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            pass

    if result.returncode != 0:
        # 优先从 payload 取 msg（869 脚本在失败时也会输出 JSON）
        if isinstance(payload, dict):
            detail = payload.get("msg") or payload.get("error") or ""
            if detail:
                return {"status": False, "msg": detail}
        # stderr 可能有 WARN 或 RuntimeError 详情
        if err:
            return {"status": False, "msg": err}
        return {"status": False, "msg": "发送失败（脚本返回非零退出码）"}

    # 成功路径：payload 已是 869 原始响应，简化为 {status, msg, data}
    if isinstance(payload, dict):
        data = payload.get("Data")
        # 提取新MsgId 作为送达证据
        new_msg_id = _extract_new_msg_id(data)
        success_info = {"status": True, "msg": "已发送", "newMsgId": new_msg_id}
        return success_info
    return {"status": True, "msg": out or "已发送"}


def _extract_new_msg_id(data: Any) -> int | None:
    """从 869 响应 Data 中提取 newMsgId（最可靠的送达证据）。"""
    if not isinstance(data, dict):
        return None
    for key in ("newMsgId", "NewMsgId", "new_msg_id"):
        val = data.get(key)
        if isinstance(val, int) and val > 0:
            return val
    return None


def _target_arg(to_wxid: str | None) -> str:
    """解析接收方 wxid：显式 > 当前会话；占位串回落当前会话。"""
    uk = (to_wxid or "").strip()
    if uk.lower() in _PLACEHOLDER_KEYS:
        uk = ""
    if not uk:
        uk = _default_target_wxid()
    if not uk:
        raise ValueError(
            "无法定位接收方：非对话环境请显式传入 to_wxid（形如 wx_<hash> 或 xxx@chatroom）"
        )
    return uk


@register_tool(category="微信", name_cn="发送微信消息", risk_level="medium")
def WeChatRoute(
    msg_type: str,
    content: str = "",
    to_wxid: str | None = None,
    path: str | None = None,
    title: str = "",
    desc: str = "",
    url: str = "",
    thumb_url: str = "",
    singer: str = "",
    cover_url: str = "",
    lyric: str = "",
    card_type: str = "摇一摇搜歌",
    at_wxids: list[str] | None = None,
    at_names: list[str] | None = None,
    voice_format: str = "amr",
    seconds: int = 2,
    file_name: str = "",
) -> str:
    """
    - 经 869 客户端向微信会话发送消息（文本/图片/视频/语音/链接/文件/名片/音乐卡片）
    - 适用：生成了媒体文件要发给对方、主动发一条文本/链接、分享文件或名片

    Args:
        msg_type: 消息类型，必填。可选：
            - text:   纯文本（群聊可配 at_wxids/at_names 真艾特）
            - image:  图片（需 path）
            - video:  视频（需 path，自动封面）
            - voice:  语音（需 path，amr/wav/mp3 自动转 silk）
            - link:   链接卡片（需 url，可选 title/desc/thumb_url）
            - file:   文件/附件（需 path，可选 file_name）
            - card:   联系人名片（需 content=<对方wxid>，title=<昵称>）
            - music:  音乐卡片（需 url=音乐直链，title=歌名，可选 singer/cover_url/lyric）
        content: 文本内容（text 类型的消息内容；card 类型时填对方 wxid）
        to_wxid: 接收方 wxid。**留空则取当前会话**（私聊=对方，群聊=群 id）。
            注意：有对话上下文时只能发给当前会话，不能跨账号发。
        path: 媒体文件路径（image/video/voice/file 必填）
        title: 标题（link 卡片标题 / card 昵称 / music 歌名）
        desc: 描述（link 卡片描述）
        url: 链接地址（link 类型必填；music 类型填音乐直链）
        thumb_url: 缩略图 URL（link 可选）
        singer: 歌手（music 可选）
        cover_url: 封面 URL（music 可选）
        lyric: 歌词（music 可选）
        card_type: 音乐卡模板，"摇一摇搜歌"(默认)/"原卡片"
        at_wxids: 群聊 @ 的 wxid 列表（text 类型，仅 @chatroom 生效）
        at_names: 群聊 @ 的显示名列表（与 at_wxids 数量一致）
        voice_format: 语音格式 amr/wav/mp3（默认 amr）
        seconds: 语音时长（秒，默认 2）
        file_name: 文件名（file 类型可选，默认取 path 文件名）

    Returns:
        发送结果：成功含消息类型与接收方；失败给出明确原因。
    """
    try:
        send_text, send_media = _resolve_scripts()
        uk = _target_arg(to_wxid)
    except Exception as exc:
        return _xml_response("error", str(exc))

    mt = (msg_type or "").strip().lower()
    py = "python3"

    try:
        if mt == "text":
            text = (content or "").strip()
            if not text:
                return _xml_response("error", "text 消息需要 content 参数")
            cmd = [py, str(send_text), "--to", uk, "--text", text]
            if at_wxids:
                for w in at_wxids:
                    cmd += ["--at", w]
            if at_names:
                for n in at_names:
                    cmd += ["--at-name", n]
            res = _run(cmd)

        elif mt == "image":
            if not path:
                return _xml_response("error", "image 消息需要 path 参数")
            if not os.path.exists(path):
                return _xml_response("error", f"文件不存在: {path}")
            res = _run([py, str(send_media), "send-image", "--to", uk, "--path", path])

        elif mt == "video":
            if not path:
                return _xml_response("error", "video 消息需要 path 参数")
            if not os.path.exists(path):
                return _xml_response("error", f"文件不存在: {path}")
            res = _run([py, str(send_media), "send-video", "--to", uk, "--path", path])

        elif mt == "voice":
            if not path:
                return _xml_response("error", "voice 消息需要 path 参数")
            if not os.path.exists(path):
                return _xml_response("error", f"文件不存在: {path}")
            res = _run([
                py, str(send_media), "send-voice",
                "--to", uk, "--path", path,
                "--format", voice_format, "--seconds", str(seconds),
            ])

        elif mt == "link":
            if not url:
                return _xml_response("error", "link 消息需要 url 参数")
            cmd = [
                py, str(send_media), "send-link",
                "--to", uk, "--url", url,
                "--title", (title or "").strip(),
                "--desc", (desc or "").strip(),
                "--thumb-url", (thumb_url or "").strip(),
            ]
            res = _run(cmd)

        elif mt == "file":
            if not path:
                return _xml_response("error", "file 消息需要 path 参数")
            if not os.path.exists(path):
                return _xml_response("error", f"文件不存在: {path}")
            cmd = [py, str(send_media), "send-file", "--to", uk, "--path", path]
            if file_name:
                cmd += ["--name", file_name]
            res = _run(cmd)

        elif mt == "card":
            card_wxid = (content or "").strip()
            if not card_wxid:
                return _xml_response("error", "card 消息需要 content=<对方wxid>")
            nick = (title or "").strip()
            if not nick:
                return _xml_response("error", "card 消息需要 title=<对方昵称>")
            res = _run([
                py, str(send_media), "send-card",
                "--to", uk,
                "--card-wxid", card_wxid,
                "--card-nickname", nick,
            ])

        elif mt == "music":
            music_url = (url or "").strip()
            if not music_url:
                return _xml_response("error", "music 消息需要 url=<音乐直链>")
            song_title = (title or "").strip()
            if not song_title:
                return _xml_response("error", "music 消息需要 title=<歌名>")
            res = _run([
                py, str(send_media), "send-music-card",
                "--to", uk,
                "--title", song_title,
                "--singer", (singer or "").strip(),
                "--music-url", music_url,
                "--jump-url", music_url,
                "--cover-url", (cover_url or "").strip(),
                "--lyric", (lyric or "").strip(),
                "--card-type", card_type,
            ])

        else:
            return _xml_response(
                "error",
                f"不支持的 msg_type={mt}，可选：text/image/video/voice/link/file/card/music"
            )
    except Exception as exc:
        logger.warning("WeChatRoute 发送异常: %s", exc, exc_info=True)
        return _xml_response("error", f"发送失败: {exc}")

    if res.get("status") is False:
        return _xml_response("error", res.get("msg") or "发送失败")

    return _xml_response(
        "success",
        f"已通过 869 发送 {mt} 消息到 {uk}",
        {"msg_type": mt, "to": uk},
    )
