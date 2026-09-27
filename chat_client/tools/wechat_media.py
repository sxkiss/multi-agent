"""
@input: json, logging, os, requests, typing, mimetypes
@output: ClawbotSendMedia 工具 — 经 clawbot 通道向微信会话发送媒体（图片/视频/文件/语音）
@position: Tools layer — 微信出站媒体通道（clawbot / ilink）
@auto-doc: Update header and folder INDEX.md when this file changes

定位：这是 [ClawbotRoute] 会话里除文本回复之外的第二条出路。文本回复由 clawbot
自动完成（AG 的回复文本经 SSE 回传后直接 sendmessage）；而**图片、视频、文件、
语音**必须走主动调用——AG 先生成/找到文件，再调本工具投递。

工具名为什么带 clawbot 前缀而非 WeChat：本工具只在 clawbot（ilink）通道下可用，
与 869 通道的 wechat-869-media-sender 是两套独立实现。前缀标明通道，避免模型
在 clawbot 会话里误调 869 技能（反之亦然）。

链路：
    AG 调 ClawbotSendMedia(path=...)
      → clawbot POST /internal/bots/send-media（x-service-key 鉴权）
        → BotSlot.send_media（取缓存的 context_token）
          → ilink getuploadurl → AES-128-ECB 加密 → CDN 上传 → sendmessage

设计要点：
- **不持有 ilink token**：工具只调本机内部接口，登录态与 context_token 都在
  clawbot 侧，避免 AG 侧触及敏感凭据。
- **会话靠 session_id 定位**：clawbot 用 session_id=user_key 调网关，因此本
  工具从当前任务上下文取 session_id 即为 user_key，无需模型传用户标识。
- **文件判断尽量自动化**：media_type 留空时按扩展名推断；识别不了才报错要求
  显式指定，避免模型猜错类型导致微信端展示异常。
"""
import logging
import os
from typing import Any

import requests

from . import PROJECT_ROOT, register_tool, _current_session_id
from .base import _xml_response

logger = logging.getLogger(__name__)

# 默认地址：clawbot 本机 9877（与 web_server._clawbot_base_url 的默认值一致）
DEFAULT_CLAWBASE = "http://127.0.0.1:9877"
_SEND_TIMEOUT = 120          # 上传+发送可能较久（大文件），放宽到 2 分钟
_SECRET_PATH = os.path.join(PROJECT_ROOT, "workspace", "clawbot.key")


def _clawbot_url() -> str:
    """clawbot 服务地址：config.clawbot_url 覆盖，否则本机 9877。"""
    try:
        from ...config import Config  # 仅在本包结构下可用
        url = str(getattr(Config, "clawbot_url", "") or "").strip().rstrip("/")
        if url:
            return url
    except Exception:
        pass
    try:
        import json
        cfg_path = os.path.join(PROJECT_ROOT, "config.json")
        with open(cfg_path, "r", encoding="utf-8") as f:
            url = str(json.load(f).get("clawbot_url") or "").strip().rstrip("/")
        if url:
            return url
    except Exception:
        pass
    return DEFAULT_CLAWBASE


def _service_key() -> str:
    """读 clawbot 服务密钥。与网关共用同一个文件（服务已在读时不重建）。"""
    try:
        if os.path.exists(_SECRET_PATH):
            with open(_SECRET_PATH, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        logger.warning("clawbot 服务密钥读取失败", exc_info=True)
    return ""


@register_tool(category="微信", name_cn="发送微信媒体", risk_level="medium")
def ClawbotSendMedia(path: str, user_key: str | None = None,
                     media_type: str | None = None, caption: str | None = None) -> str:
    """
    - 向微信会话发送媒体文件（图片 / 视频 / 文件 / 语音）
    - 适用：生成了图片/音频/文档后要发给对方、对方要某个已有文件
    - 仅在 clawbot 通道下可用（对方从微信发消息进来且机器人已登录）

    Args:
        path: 待发送文件的**绝对路径**。
        user_key: 目标用户的会话键，形如 wx_<hash>。**留空则取当前会话**
            （普通对话场景够用）。定时任务 / 子会话等没有会话上下文的场景
            必须显式指定，否则无法定位发给谁。
        media_type: 媒体类型，留空按扩展名自动推断。可选 image / video / file / voice。
            无法识别扩展名时必须显式指定（例如 .mp3 之外格式的音频）。
        caption: 附带说明文字。发 图片/视频/文件 时会额外发一条文本；
            发 语音 时作为语音转文字展示。

    Returns:
        发送结果：成功含文件名与字节数；失败给出明确原因（未连接/文件不存在/超限等）。
    """
    p = (path or "").strip()
    if not p:
        return _xml_response("error", "缺少参数 path")

    # 相对路径按工作目录解析，避免模型传相对路径时找不到文件
    if not os.path.isabs(p):
        try:
            from ...config import Config
            base = str(getattr(Config, "workspace", "") or PROJECT_ROOT)
        except Exception:
            base = PROJECT_ROOT
        p = os.path.join(base, p)

    if not os.path.exists(p):
        return _xml_response("error", f"文件不存在: {p}")
    if not os.path.isfile(p):
        return _xml_response("error", f"不是文件（可能是目录）: {p}")

    # 目标用户：显式 user_key 优先，否则取当前会话上下文。
    #
    # 为什么要支持显式传：定时任务 / 子会话由系统拉起，没有对话线程上下文，
    # set_current_job 从未被调用 → _current_session_id() 返回 None，工具就
    # 定位不到发给谁。这类场景必须让调用方显式指定 user_key。
    uk = (user_key or "").strip() or (_current_session_id() or "")
    if not uk:
        return _xml_response(
            "error",
            "无法定位目标用户：非对话环境（如定时任务）请显式传入 user_key"
            "（形如 wx_<hash>，可从 clawbot bottings 目录名或会话记录中获得）。")

    key = _service_key()
    if not key:
        return _xml_response("error", "clawbot 服务密钥未配置，无法调用发送接口")

    payload: dict[str, Any] = {
        "user_key": uk,
        "path": p,
        "media_type": (media_type or "").strip(),
        "text": (caption or "").strip(),
    }
    try:
        resp = requests.post(
            f"{_clawbot_url()}/internal/bots/send-media",
            json=payload, headers={"x-service-key": key}, timeout=_SEND_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("调用 clawbot 发送接口失败: %s", exc, exc_info=True)
        return _xml_response("error", f"无法连接微信机器人服务: {exc}")

    try:
        body = resp.json()
    except Exception:
        return _xml_response("error", f"微信机器人返回异常 HTTP {resp.status_code}")

    if isinstance(body, dict) and body.get("status") is False:
        return _xml_response("error", str(body.get("msg") or "发送失败"))

    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        name = data.get("file_name") or os.path.basename(p)
        size = int(data.get("bytes") or 0)
        hint = f"，已发送 \“{caption}\”" if caption else ""
        return _xml_response(
            "success",
            f"已发送到微信会话：{name}（{size / 1024:.1f} KB）{hint}",
            {"name": name, "bytes": size},
        )
    return _xml_response("success", "已发送到微信会话")
