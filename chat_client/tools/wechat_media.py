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

# JSON 序列化常见占位串：模型/上游可能把 None 序列化成 "null"/"None" 等字符串。
# 若当真实 user_key 传出去，会命中 clawbot 的"机器人未运行"（找不到该槽位）。
# 语义上是"未指定"，应回落自动取当前会话，而不是当作真实用户。
_PLACEHOLDER_KEYS = {"", "null", "none", "undefined", "nil", "n/a", "unknown"}


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


def _hint_target_key(uk: str) -> str:
    """发送失败时说明"应该发给谁"，附在"机器人未运行"这类报错上。

    为什么不列举其他在线账号：媒体发出去不可逆，一旦把候选 user_key 列成名单，
    调用方（模型）就可能挑错，把文件发到另一个人的微信里。正确语义是——有对话
    上下文时目标唯一锁定当前会话，不存在"挑一个"的余地；没有上下文时才必须由
    调用方显式指定，此时也只说明规则，不给候选名单（避免诱导乱猜）。
    """
    sid = (_current_session_id() or "").strip()
    if sid:
        return (f"发送目标已锁定当前会话 user_key={sid}（媒体不可跨账号发送）。"
                "该账号未登录/未运行时，请在小程序重新扫码连接，而不是改用其他账号。")
    return (f"当前无对话上下文，必须显式传入正确的 user_key（形如 wx_<hash>，"
            f"可到 clawbot bottings 目录核对账号状态）；本次传入的是 {uk}。")


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
            （普通对话场景够用）。
            注意：有对话上下文时**只能发给当前会话**，显式传其他 user_key
            会被拒绝（媒体发出去无法撤回，不允许跨账号发送）。
            仅定时任务 / 子会话等无会话上下文的场景才需显式指定。
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
    # 占位串（"null"/"None"/"undefined" 等）视为未指定 → 回落当前会话。
    # 全部小写比较，覆盖大小写混用（"NULL"/"Null"）。
    uk = (user_key or "").strip()
    if uk.lower() in _PLACEHOLDER_KEYS:
        uk = ""
    sid = (_current_session_id() or "").strip()
    # 跨账号硬拦截：有对话上下文时，目标必须是当前会话。显式传别的 user_key
    # 一律拒绝——媒体发出去不可逆，发错人无法撤回。定时任务等无上下文场景
    # （sid 为空）才放行显式指定，因为那时没有"当前会话"可锁。
    if sid and uk and uk != sid:
        return _xml_response(
            "error",
            f"拒绝跨账号发送：当前会话是 {sid}，而你要求发给 {uk}。"
            "媒体一旦发出无法撤回，只能发给当前对话方。若确需发给该账号，"
            "请在该账号的会话里调用本工具。")
    uk = uk or sid
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
        msg = str(body.get("msg") or "发送失败")
        # "未运行/未登录"通常是该账号掉线或从未扫码。说明"该发给谁"，
        # 但绝不列举其他在线账号（见 _hint_target_key：媒体不可逆，不能诱导换人）。
        if "未运行" in msg or "未连接" in msg or "未登录" in msg:
            msg += "。" + _hint_target_key(uk)
        return _xml_response("error", msg)

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
