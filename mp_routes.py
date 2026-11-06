"""小程序用户数据与微信机器人 API。

优-1 第六阶段：从 web_server.py 拆出的小程序/机器人域路由。
"""
from __future__ import annotations

import logging
import os
import re

from fastapi import Request
from fastapi.responses import JSONResponse

import public

logger = logging.getLogger(__name__)

_BASE_DIR = ""
_AGENT = None


# 小程序用户数据（服务端权威存储）
# ============================================================
# 小程序本地存储在清缓存 / 换设备时会丢，签到这类有积累价值的数据
# 必须落服务端。身份来自 token 的 sub（wx:<openid>），与用户工作目录、
# 会话 ID 共用同一把钥匙 —— 微信机器人（clawbot）侧用同一 user_key 作
# session_id，两边即共享同一条会话与同一份数据（见下方 bot 转发区）。
_MP_STORE = None


def _mp_store():
    """惰性初始化：避免 import 期就创建目录（测试环境可能无写权限）。"""
    global _MP_STORE
    if _MP_STORE is None:
        from mp_store import MpStore
        _MP_STORE = MpStore(os.path.join(_BASE_DIR, "workspace", "users"))
    return _MP_STORE


def _mp_user_key(request: Request) -> str:
    """从 Bearer token 解析用户键；非终端登录态返回空串。

    不接受客户端传入的 user_key —— 身份只能来自服务端签发的 token，
    否则任何调用方都能读写他人数据。

    支持两类终端用户：
    - 小程序（wx:<openid>）：通过 openid 派生 wx_<hash>。
    - 网页卡密登录（card:<prefix>）：直接用前 6 位作为 user_key，
      与会话 ID 一致（card_<prefix>），便于 clawbot 扫码共享会话。
    """
    from auth import extract_token, verify_token, wx_session_id, card_session_id
    token = extract_token(request)
    if not token:
        return ""
    payload = verify_token(_BASE_DIR, token)
    if not payload:
        return ""
    sub = str(payload.get("sub") or "")
    if sub.startswith("wx:"):
        openid = sub[3:]
        if not openid:
            return ""
        return wx_session_id(openid)
    if sub.startswith("card:"):
        prefix = sub[5:]
        if prefix and re.fullmatch(r"[0-9A-Z]{6}", prefix):
            return card_session_id(prefix)
    return ""


def _mp_guard(request: Request):
    """统一身份校验：返回 (user_key, error_response)。"""
    user_key = _mp_user_key(request)
    if not user_key:
        return "", JSONResponse(
            {"status": False, "msg": "未授权：请重新登录", "code": 401},
            status_code=401,
        )
    return user_key, None


async def api_mp_profile_get(request: Request):
    """拉取用户全量数据（签到 / 资料 / 设置 / 统计）。"""
    user_key, err = _mp_guard(request)
    if err:
        return err
    try:
        store = _mp_store()
        doc = store.touch_visit(user_key)
        return JSONResponse(public.return_data(True, data={
            "doc": doc,
            "today": store.today(),
        }))
    except Exception:
        logger.warning("[mp_profile] 读取失败", exc_info=True)
        return JSONResponse(public.returnMsg(False, "读取失败"), status_code=500)


async def api_mp_profile_post(request: Request):
    """写操作：签到 / 资料 / 设置。

    请求体 {"action": "checkin"} 或 {"action": "update", "patch": {...}}。
    签到由服务端判定日期与连击（客户端时区不可信）。
    """
    user_key, err = _mp_guard(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(public.returnMsg(False, "参数错误"))

    action = str(body.get("action") or "").strip()
    try:
        store = _mp_store()
        if action == "checkin":
            r = store.checkin(user_key)
            return JSONResponse(public.return_data(True, data={
                "already": r["already"],
                "streak": r["streak"],
                "total": r["total"],
                "last": r["last"],
                "doc": r["doc"],
                "today": store.today(),
            }))
        if action == "update":
            patch = body.get("patch")
            if not isinstance(patch, dict):
                return JSONResponse(public.returnMsg(False, "缺少 patch"))
            doc = store.update(user_key, patch)
            return JSONResponse(public.return_data(True, data={"doc": doc}))
        if action == "import":
            # 首次迁移：本地已有数据并入服务端，只填空不覆盖（见 mp_store.import_data）
            data_in = body.get("data")
            if not isinstance(data_in, dict):
                return JSONResponse(public.returnMsg(False, "缺少 data"))
            doc = store.import_data(user_key, data_in)
            return JSONResponse(public.return_data(True, data={"doc": doc}))
        return JSONResponse(public.returnMsg(False, "未知 action"))
    except Exception:
        logger.warning("[mp_profile] 写入失败", exc_info=True)
        return JSONResponse(public.returnMsg(False, "保存失败"), status_code=500)


# ============================================================


# 微信机器人（clawbot）：每用户独立实例
# ============================================================
# 模型（2026-09-24 修正）：不是"一个共享机器人 + 绑定码认领"，而是
# **每个小程序用户拥有自己的微信机器人实例**，由用户本人在小程序里扫码登录。
#
# 为什么不需要绑定码：取二维码的请求本身就携带用户 token，网关解析出
# user_key 后才向 clawbot 申请二维码 —— "这个二维码属于谁"在创建时即确定，
# 扫码结果直接落在该用户槽位，中间没有可劫持环节。
#
# 会话共用：clawbot 用 session_id = user_key 调 /api/chat/start，与小程序
# 的 wx_session_id(openid) 完全一致 → 两边是同一条会话、同一份用户数据。
#
# 网关在此只做转发与身份注入，不持有任何 bot 登录态（都在 clawbot 侧）。
#
# 服务密钥：clawbot 与网关之间的服务间凭据。clawbot 不需要它也能工作
# （槽位由网关转发创建），但保留供服务间直连（如内部状态查询）使用。

def _service_key_path() -> str:
    return os.path.join(_BASE_DIR, "workspace", "clawbot.key")


def _service_secret() -> str:
    """服务间共享密钥。首次访问自动生成并落盘（workspace/ 在 gitignore 内）。

    与用户 token 分离：它只证明"调用方是 clawbot"，不代表任何终端用户身份，
    因此绝不能用它去读写用户数据。
    """
    path = _service_key_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        import secrets as _secrets
        os.makedirs(os.path.dirname(path), exist_ok=True)
        key = _secrets.token_urlsafe(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        logger.info("[clawbot] 已生成服务密钥: %s", path)
        return key
    except Exception:
        logger.warning("服务密钥读写失败", exc_info=True)
        return ""


def _check_service_key(request: Request) -> bool:
    """校验服务密钥（常量时间比较，防时序侧信道）。"""
    import hmac as _hmac
    expected = _service_secret()
    got = str(request.headers.get("x-service-key") or "").strip()
    if not expected or not got:
        return False
    return _hmac.compare_digest(expected, got)


def _clawbot_base_url() -> str:
    """clawbot 服务地址。默认本机 9877，可用 config.clawbot_url 覆盖。"""
    return str((_AGENT.config or {}).get("clawbot_url") or "").strip().rstrip("/") \
        or "http://127.0.0.1:9877"


def _clawbot_forward(method: str, path: str, payload=None, params=None) -> JSONResponse:
    """把请求转发给 clawbot 并原样回传其 JSON（自动附带服务密钥）。

    clawbot 未启动是"功能不可用"而非"服务端错误"，故单独给出 503 文案，
    避免前端把它当成 500 去重试或报"服务器异常"。
    """
    import requests as _rq
    url = f"{_clawbot_base_url()}{path}"
    headers = {"x-service-key": _service_secret()}
    try:
        if method == "GET":
            resp = _rq.get(url, params=params or {}, headers=headers, timeout=20)
        else:
            resp = _rq.post(url, json=payload or {}, headers=headers, timeout=20)
    except Exception:
        logger.warning("[clawbot] 转发失败 %s %s", method, path, exc_info=True)
        return JSONResponse(public.returnMsg(False, "微信机器人服务未启动"), status_code=503)
    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception:
        return JSONResponse(public.returnMsg(False, "微信机器人返回异常"), status_code=502)


async def api_mp_bot_qrcode(request: Request):
    """小程序为**当前登录用户**申请专属二维码（需用户 token）。

    身份来自 token，客户端无法为他人取码 —— 这是"每用户独立 bot"的安全边界。
    """
    user_key, err = _mp_guard(request)
    if err:
        return err
    return _clawbot_forward("POST", "/internal/bots/qrcode", {"user_key": user_key})


async def api_mp_bot_status(request: Request):
    """小程序查询**自己**机器人的连接状态（需用户 token）。"""
    user_key, err = _mp_guard(request)
    if err:
        return err
    return _clawbot_forward("GET", "/internal/bots/status", params={"user_key": user_key})


async def api_mp_bot_disconnect(request: Request):
    """用户断开自己的机器人（需用户 token）：清除登录态，可重新扫码。"""
    user_key, err = _mp_guard(request)
    if err:
        return err
    return _clawbot_forward("POST", "/internal/bots/disconnect", {"user_key": user_key})
async def api_service_token(request: Request):
    """clawbot 换取长期令牌（需服务密钥），用于调用聊天接口。

    与用户 token 同一套签名体系（sub=service:clawbot），但 TTL 长、专供服务间
    调用；用户登出不影响它，服务密钥轮换即可吊销。
    """
    if not _check_service_key(request):
        return JSONResponse({"status": False, "msg": "服务密钥无效", "code": 401}, status_code=401)
    try:
        from auth import create_token
        ttl_hours = 24 * 365 * 5   # 5 年：服务令牌，避免频繁轮换
        token = create_token(_BASE_DIR, ttl_hours, sub="service:clawbot")
        return JSONResponse(public.return_data(True, data={
            "token": token,
            "expires_in": ttl_hours * 3600,
        }))
    except Exception:
        logger.warning("[mp_bind] 服务令牌签发失败", exc_info=True)
        return JSONResponse(public.returnMsg(False, "签发失败"), status_code=500)


# ============================================================


def register_mp_routes(app, base_dir: str, agent_main) -> None:
    global _BASE_DIR, _AGENT
    _BASE_DIR = base_dir
    _AGENT = agent_main
    app.get("/api/mp/profile")(api_mp_profile_get)
    app.post("/api/mp/profile")(api_mp_profile_post)
    app.post("/api/mp/bot/qrcode")(api_mp_bot_qrcode)
    app.get("/api/mp/bot/status")(api_mp_bot_status)
    app.post("/api/mp/bot/disconnect")(api_mp_bot_disconnect)
    app.post("/api/service/token")(api_service_token)
