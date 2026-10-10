#!/usr/bin/env python3
"""
@input: fastapi, jwt, hashlib, hmac, secrets, json, os; config.json
@output: 登录接口 /api/auth/* + JWT 校验中间件 + 凭据管理
@position: 安全层 — 网关鉴权，所有 /api/* 的默认保护
@auto-doc: Update header and folder INDEX.md when this file changes

网关鉴权模块
------------------------------------------------------------------
背景：网关原先 ~70 个路由全部零鉴权，且 GET /api/config 明文返回
api_key，任何人访问 9876 端口即可拿到模型密钥并读写文件。
本模块提供统一鉴权，是接入微信小程序等外部客户端的前置条件。

设计要点：
1. 仅依赖标准库 + PyJWT（bcrypt 可选，缺失时回退 PBKDF2-HMAC-SHA256）。
2. 令牌走 `Authorization: Bearer <token>`；SSE 端点因前端用 fetch
   实现（非 EventSource），同样可带 header，无需 query 传参。
3. 默认关闭鉴权（auth.enabled=false），保持既有部署行为不变；
   开启后除白名单外全部受保护，避免升级即锁死。
4. 密码只存 PBKDF2 派生值（salt+hash），永不落明文。
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Optional

import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 常量
# ------------------------------------------------------------------
TOKEN_ISSUER = "bt-agent"
DEFAULT_TTL_HOURS = 24
# 续期宽限期：token 过期后多久内仍允许换新（默认 30 天）。
#
# 为什么需要宽限：JWT 无状态、exp 写死，若只认严格有效期，用户离开几天回来
# 就必然 401，只能重新走微信授权。给一段宽限期后，客户端只要还在用（本地
# 存着 token），就能静默换新，体验上等于"登录态不过期"。
#
# 为什么不是无限：宽限期越长，遗失的 token 可兑换新 token 的窗口越长。
# 30 天是"连续活跃用户无感"与"泄漏后可控"的折中；真正吊销靠轮换 secret。
DEFAULT_REFRESH_GRACE_DAYS = 30
PBKDF2_ITERATIONS = 200_000

# 免鉴权路径。登录接口本身 + 静态资源 + 首页 HTML。
# 首页必须放行：否则用户连登录页都加载不出来，形成"没 token → 打不开页面 →
# 无法登录"的死锁。静态资源同理。
PUBLIC_EXACT = {"/", "/index.html", "/static/card-login.html"}
PUBLIC_PREFIX = (
    "/api/auth/login",
    # 微信登录必须放行：用户此刻还没有 token，若要求先鉴权就形成
    # "没 token → 不能登录 → 拿不到 token" 的死锁，多用户将完全无法进入。
    "/api/auth/wxlogin",
    # 卡密登录同样必须放行：用户还未持有 token，必须能无鉴权访问
    # 以换取首次登录令牌（与 wxlogin 同模式，防止死锁）。
    "/api/auth/cardlogin",
    # 卡密实时校验：同样需要在登录前调用，无 token 状态下的前置校验
    "/api/auth/card/verify",
    "/api/auth/status",
    "/api/auth/logout",
    # 续期必须放行：它的入参本身就是"刚过期"的 token，中间件若按严格
    # 有效期拦截，请求永远到不了这里，续期形同虚设。
    # 免鉴权不等于无鉴权——接口内部用 verify_token_allow_expired 校验
    # 签名与宽限期，安全性由该逻辑保证（见 api_auth_refresh）。
    "/api/auth/refresh",
    # clawbot 服务间接口：用 x-service-key 认证（见 web_server._check_service_key），
    # 不走用户 Bearer token。若在此拦截，clawbot 无法换取服务令牌。
    # 安全性由接口内部的服务密钥校验保证，不因免鉴权而降低。
    "/api/service/token",
    # OpenAI 兼容层：/v1/* 用"管理 key"鉴权（见 v1_compat._verify）。
    # 与 clawbot 同模式——白名单放行 + 接口内部校验，
    # 中间件放行不等于无鉴权，安全性由 v1_compat 内部校验保证。
    "/v1/",
    "/favicon.png",
    "/static/",
    "/assets/",
)

# ------------------------------------------------------------------
# 凭据存储：独立文件，避免与 config.json 的合并规则相互污染
# ------------------------------------------------------------------

def _auth_file(base_dir: str) -> str:
    return os.path.join(base_dir, "auth.json")


def _pbkdf2(password: str, salt: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    ).hex()


def _verify_password(password: str, stored: dict) -> bool:
    """校验密码。stored 形如 {salt, hash, iterations}。"""
    try:
        expected = stored.get("hash", "")
        got = _pbkdf2(password, stored.get("salt", ""), int(stored.get("iterations", PBKDF2_ITERATIONS)))
        return hmac.compare_digest(expected, got)
    except Exception:
        logger.warning("密码校验异常", exc_info=True)
        return False


def load_auth(base_dir: str) -> dict:
    """读取 auth.json；不存在则返回默认（未初始化 / 关闭）。"""
    path = _auth_file(base_dir)
    # secret 必须在默认键内：否则下面的键过滤会把它丢弃，导致每次读取都
    # 重新生成签名密钥，已签发 token 全部校验失败（表现为"登录成功却仍 401"）。
    default = {
        "enabled": False,
        "password": None,
        "token_ttl_hours": DEFAULT_TTL_HOURS,
        "secret": None,
        "admin_api_key": None,
    }
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return default
        out = dict(default)
        out.update({k: v for k, v in data.items() if k in default})
        return out
    except Exception:
        logger.warning("auth.json 读取失败，按未启用处理", exc_info=True)
        return default


def save_auth(base_dir: str, data: dict) -> bool:
    """写入 auth.json（权限 600，密钥文件不应被其他用户读取）。"""
    path = _auth_file(base_dir)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(path, 0o600)
        return True
    except Exception:
        logger.error("auth.json 写入失败", exc_info=True)
        return False


def set_password(base_dir: str, password: str) -> bool:
    """设置/重置管理员密码（PBKDF2 派生后落盘）。"""
    salt = secrets.token_hex(16)
    record = {
        "salt": salt,
        "hash": _pbkdf2(password, salt),
        "iterations": PBKDF2_ITERATIONS,
    }
    data = load_auth(base_dir)
    data["password"] = record
    return save_auth(base_dir, data)


def is_initialized(base_dir: str) -> bool:
    return bool(load_auth(base_dir).get("password"))


def get_admin_api_key(base_dir: str) -> str:
    """获取固定的"管理 key"（供 OpenAI 兼容层 /v1/* 作为 api_key 使用）。

    与登录 token 的区别：
    - 登录 token 是 JWT，有有效期，需走 /api/auth/login 换取；
    - 管理 key 是长期固定的随机串，一次配置即可长期使用，
      适合直接填进 OpenAI SDK 的 api_key（无需先登录换 token）。

    首次调用时生成并落盘（auth.json，权限 600），之后恒定不变。
    """
    data = load_auth(base_dir)
    key = data.get("admin_api_key")
    if key:
        return str(key)
    key = "sk-" + secrets.token_urlsafe(32)
    data["admin_api_key"] = key
    if not save_auth(base_dir, data):
        # 落盘失败时仍返回本次生成的 key（至少当前进程可用），并告警
        logger.error("管理 key 落盘失败，重启后将重新生成")
    return key


# ------------------------------------------------------------------
# JWT
# ------------------------------------------------------------------

def _secret(base_dir: str) -> str:
    """签名密钥：auth.json 中的 secret，缺失时惰性生成并持久化。

    若密钥丢失（文件被删），所有已签发 token 立即失效——这是可接受的
    安全取舍：宁可强制重新登录，也不让密钥可预测。
    """
    data = load_auth(base_dir)
    sec = data.get("secret")
    if not sec:
        sec = secrets.token_urlsafe(48)
        data["secret"] = sec
        save_auth(base_dir, data)
    return sec


def create_token(base_dir: str, ttl_hours: int, sub: str = "admin") -> str:
    """签发令牌。

    sub 用于区分主体：管理员为 "admin"，微信用户为 "wx:<openid>"。
    后续鉴权中间件据此判断请求来源。
    """
    now = int(time.time())
    payload = {
        "sub": sub,
        "iat": now,
        "exp": now + int(ttl_hours) * 3600,
        "iss": TOKEN_ISSUER,
    }
    return jwt.encode(payload, _secret(base_dir), algorithm="HS256")


def verify_token(base_dir: str, token: str) -> Optional[dict]:
    try:
        return jwt.decode(
            token, _secret(base_dir), algorithms=["HS256"], issuer=TOKEN_ISSUER
        )
    except jwt.ExpiredSignatureError:
        return None
    except Exception:
        return None


def verify_token_allow_expired(base_dir: str, token: str,
                               grace_seconds: int) -> Optional[dict]:
    """校验令牌，但允许"刚过期不久"的情况仍解析出 payload。

    为什么需要：JWT 无状态，签出后 exp 就写死了，服务端没有任何续期入口。
    纯 verify_token 一旦过期返回 None，前端只能让用户重新走微信授权。
    续期接口必须能读出"这个 token 是谁的"，才能在确认身份后签新 token——
    而过期了就验不过，所以这里临时关掉 exp 校验。

    安全边界：只放宽 exp 一项，签名、issuer 仍强制校验（PyJWT 的
    verify_exp=False 不影响其余声明）；且由调用方用 grace_seconds 限定
    "过期多久内才算数"，超出宽限期一律拒绝。绝不用于常规鉴权路径。
    """
    if grace_seconds <= 0:
        return None
    try:
        payload = jwt.decode(
            token, _secret(base_dir), algorithms=["HS256"], issuer=TOKEN_ISSUER,
            options={"verify_exp": False},
        )
    except Exception:
        return None
    try:
        exp = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if exp <= 0:
        return None
    # 只接受"过期未超过宽限期"；尚未过期或过期太久都不走这条路
    now = int(time.time())
    if now < exp:
        return payload          # 没过期，正常返回（调用方会签新 token）
    if now - exp > grace_seconds:
        return None             # 超宽限期：等同于作废
    return payload


# ------------------------------------------------------------------
# 请求侧提取
# ------------------------------------------------------------------

def extract_token(request: Request) -> Optional[str]:
    """从 Authorization 头或 ?token= 查询串提取令牌。

    支持查询串是因为部分客户端（含小程序某些场景、浏览器直接打开
    SSE 链接调试）不便设置 header；但该路径会进入访问日志，故仅作
    兼容而非推荐方式。
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    tok = request.query_params.get("token")
    return tok.strip() if tok else None


def is_public(path: str) -> bool:
    return path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIX)


# ------------------------------------------------------------------
# 微信登录（code2session）
# ------------------------------------------------------------------

WX_SECRET_FILE = os.path.join("miniprogram", ".wxsecret")
_WX_CODE_RE = re.compile(r"^[0-9a-zA-Z_\-]{1,64}$")


def load_wx_credentials(base_dir: str) -> tuple:
    """读取小程序 AppID/AppSecret。返回 (appid, secret)；缺失返回 ("","")。"""
    path = os.path.join(base_dir, WX_SECRET_FILE)
    if not os.path.exists(path):
        return "", ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("appid", "")).strip(), str(data.get("appsecret", "")).strip()
    except Exception:
        logger.warning("微信凭据读取失败", exc_info=True)
        return "", ""


def wx_code2session(base_dir: str, code: str) -> dict:
    """用临时登录凭证 code 换取 openid/session_key。

    调用微信 jscode2session 接口。失败时返回 {"ok": False, "msg": ...}，
    成功返回 {"ok": True, "openid": ..., "unionid": ...}。
    注意：session_key 绝不返回给客户端。
    """
    appid, secret = load_wx_credentials(base_dir)
    if not appid or not secret:
        return {"ok": False, "msg": "服务端未配置小程序凭据"}
    if not code or not _WX_CODE_RE.match(code):
        return {"ok": False, "msg": "code 格式不合法"}

    url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {
        "appid": appid,
        "secret": secret,
        "js_code": code,
        "grant_type": "authorization_code",
    }
    try:
        import requests
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception as e:
        logger.warning("微信 code2session 请求失败: %s", e)
        return {"ok": False, "msg": "微信接口请求失败"}

    errcode = data.get("errcode")
    if errcode:
        # 常见：40029 无效 code、45011 频率限制、40125 无效 appsecret
        logger.warning("微信登录失败 errcode=%s errmsg=%s", errcode, data.get("errmsg"))
        return {"ok": False, "msg": f"微信登录失败({errcode})"}
    openid = str(data.get("openid") or "").strip()
    if not openid:
        return {"ok": False, "msg": "未获取到 openid"}
    return {
        "ok": True,
        "openid": openid,
        "unionid": str(data.get("unionid") or "").strip(),
    }


def wx_session_id(openid: str) -> str:
    """由 openid 派生稳定的会话 ID。

    同一微信用户每次进入都映射回同一个会话目录，实现"会话绑定"。
    前缀 wx_ 便于在 sessions/ 中识别来源；截 openid 前缀是因
    session_id 最长 128 且需可读，openid 本身已足够唯一。
    """
    digest = hashlib.sha256(openid.encode("utf-8")).hexdigest()[:24]
    return f"wx_{digest}"


# ------------------------------------------------------------------
# 卡密登录（网页端）
# ------------------------------------------------------------------
# 与微信登录对称：微信用户由 openid 锚定，卡密用户由卡密前 6 位锚定。
# 两条路径派生出各自的 wx_xxx / card_xxx 会话 ID，共享同一套
# /api/mp/* 用户存储与 clawbot 扫码绑定 —— 前端（卡密）与小程序（微信）
# 因此能拿到完全一致的能力，只是身份来源不同。

CARD_KEYS_FILE = "card_keys.json"
# 卡密前 6 位即用户标识：只允许大写字母与数字，既便于口头传播，
# 也避免大小写混淆导致的"同一张卡两个人各登一次拿到两个会话"。
_CARD_PREFIX_RE = re.compile(r"^[0-9A-Z]{6}$")
# 完整卡密：前缀 + 校验段（允许连字符分隔，存储时归一化去掉）
_CARD_FULL_RE = re.compile(r"^[0-9A-Za-z\-_]{6,128}$")


def _card_file(base_dir: str) -> str:
    return os.path.join(base_dir, CARD_KEYS_FILE)


def load_card_keys(base_dir: str) -> dict:
    """读取卡密库。返回 {prefix: {...}}；文件缺失返回空字典。

    结构：{"ABC123": {"key": "ABC123-XXXX", "owner": "...", "expires": "..."}}
    以"前 6 位"为键 —— 它是用户标识，也是会话 ID 的来源。
    """
    path = _card_file(base_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.warning("card_keys.json 读取失败", exc_info=True)
        return {}
    cards = data.get("cards") if isinstance(data, dict) else None
    if not isinstance(cards, dict):
        return {}
    out = {}
    for prefix, rec in cards.items():
        if isinstance(rec, dict):
            out[str(prefix)] = rec
    return out


def normalize_card_key(raw: str) -> str:
    """归一化卡密输入：去空白与分隔符，转大写，便于用户粘贴/手输。"""
    return re.sub(r"[\s\-_]", "", str(raw or "")).upper()


def verify_card_key(base_dir: str, raw_key: str) -> tuple:
    """校验卡密。通过返回 (True, prefix)；失败返回 (False, 错误信息)。

    只认"前 6 位"作为身份 —— 完整卡密仅在此处比对一次，之后所有
    请求都靠 token 中的 sub=card:<prefix> 识别用户。
    """
    key = normalize_card_key(raw_key)
    if not key or not _CARD_FULL_RE.match(str(raw_key or "").strip()):
        return False, "卡密格式不合法"
    prefix = key[:6]
    if not _CARD_PREFIX_RE.match(prefix):
        return False, "卡密前 6 位必须为大写字母或数字"

    cards = load_card_keys(base_dir)
    rec = cards.get(prefix)
    if not rec:
        return False, "卡密无效"

    # 完整卡密比对：仅当前 6 位命中时才比，避免为不存在的卡泄露信息
    stored = normalize_card_key(rec.get("key", ""))
    if not stored or not hmac.compare_digest(stored, key):
        return False, "卡密无效"

    # 有效期：ISO 日期串，过期即拒绝（留空表示长期有效）
    expires = str(rec.get("expires") or "").strip()
    if expires:
        try:
            from datetime import datetime, timezone
            exp_day = datetime.strptime(expires, "%Y-%m-%d").date()
            today = datetime.now(timezone.utc).astimezone().date()
            if today > exp_day:
                return False, "卡密已过期"
        except ValueError:
            logger.warning("卡密 %s 的 expires 格式非法: %s", prefix, expires)
    return True, prefix


def card_session_id(prefix: str) -> str:
    """由卡密前 6 位派生会话 ID，与 wx_session_id 对称。

    直接用前缀而非哈希：前缀本身已规范化（6 位大写字母数字），
    可读且天然唯一，便于在 sessions/ 与 workspace/users/ 中定位。
    """
    return f"card_{prefix}"


# ------------------------------------------------------------------
# 卡密管理（管理员专用，仅由 web_server 的 /api/card/admin/* 调用）
# ------------------------------------------------------------------

def _card_file_write(base_dir: str, cards: dict) -> bool:
    """写回卡密库（原子写，权限 600 —— 卡密等同凭据）。"""
    path = _card_file(base_dir)
    try:
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"cards": cards}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        return True
    except Exception:
        logger.error("card_keys.json 写入失败", exc_info=True)
        return False


def _gen_unique_prefix(cards: dict) -> str:
    """生成未被占用的 6 位前缀（大写字母数字，排除易混字符 0/O/1/I）。

    排除易混字符：卡密多半靠口头/手抄传递，0 与 O、1 与 I 肉眼难分，
    一旦抄错就是"登录到别人的会话"（前缀即会话 ID），必须规避。
    """
    import random
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 无 0/O/1/I
    for _ in range(200):
        prefix = "".join(random.choice(alphabet) for _ in range(6))
        if prefix not in cards:
            return prefix
    raise RuntimeError("无法生成唯一卡密前缀（空间耗尽）")


def create_card(base_dir: str, days: int = 30, owner: str = "", note: str = "") -> dict:
    """生成一张新卡密。返回卡密记录（含明文 key，仅此一次可见）。

    结构：{prefix, key, owner, note, created_at, expires, days, activated_at}
    days<=0 表示长期有效（expires 为空串）。

    有效期起点：首次登录时（activate_card）才落 activated_at 并算出 expires，
    创建时刻不计时 —— 卡密可提前批量生成、按需分发，买家首次使用才开表。
    旧卡（expires 已由旧逻辑按 created_at 算好）继续沿用原 expires，不受影响。
    """
    cards = load_card_keys(base_dir)
    prefix = _gen_unique_prefix(cards)
    import secrets as _secrets
    # 校验段：8 位 URL 安全随机串，与前缀拼成完整卡密
    suffix = _secrets.token_urlsafe(12).replace("-", "").replace("_", "")[:8].upper()
    full_key = f"{prefix}-{suffix}"

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).astimezone()
    rec = {
        "key": full_key,
        "owner": str(owner or "")[:64],
        "note": str(note or "")[:200],
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "expires": "",   # 创建时不计时，首次激活再算
        "days": int(days or 0),
        "activated_at": "",  # 空串=尚未首次登录
    }
    cards[prefix] = rec
    _card_file_write(base_dir, cards)
    return {"prefix": prefix, **rec}


def activate_card(base_dir: str, prefix: str) -> dict:
    """卡密首次登录时调用：落 activated_at，并按 days 算出 expires。

    - days<=0：长期有效，expires 保持空串；
    - days>0 且此前未激活：激活时刻 + days 天为到期日（"今天"起算不算第一天被
      显式排除：从激活日 0 点起算，给足完整 N 天，避免"当天激活当天过期"）。
    - 已经激活（activated_at 非空）：幂等返回当前记录，不改 expires。
    返回更新后的完整记录；prefix 不存在返回 {}。
    """
    cards = load_card_keys(base_dir)
    rec = cards.get(prefix)
    if not rec:
        return {}
    if rec.get("activated_at"):
        return {"prefix": prefix, **rec}  # 已激活，幂等

    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).astimezone()
    rec["activated_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    days = int(rec.get("days") or 0)
    if days > 0:
        # 从激活日当天 0 点起算，给足完整 N 天：N 天后 0 点才过期
        today = now.date()
        expires_day = today + timedelta(days=days)
        rec["expires"] = expires_day.strftime("%Y-%m-%d")
    else:
        rec["expires"] = ""  # 长期有效
    _card_file_write(base_dir, cards)
    return {"prefix": prefix, **rec}


def delete_card(base_dir: str, prefix: str) -> bool:
    """删除卡密；不存在返回 False。"""
    cards = load_card_keys(base_dir)
    if prefix not in cards:
        return False
    del cards[prefix]
    return _card_file_write(base_dir, cards)


def update_card(base_dir: str, prefix: str, patch: dict) -> dict:
    """更新卡密的可编辑字段（owner / note / 有效期）。

    有效期用 days 重算：传 days 则按"今天+N天"顺延，
    传 days=0 视为长期有效。返回更新后的记录。
    """
    cards = load_card_keys(base_dir)
    rec = cards.get(prefix)
    if not rec:
        return {}
    if "owner" in patch:
        rec["owner"] = str(patch.get("owner") or "")[:64]
    if "note" in patch:
        rec["note"] = str(patch.get("note") or "")[:200]
    if "days" in patch:
        from datetime import datetime, timedelta, timezone
        days = int(patch.get("days") or 0)
        rec["days"] = days
        if days > 0:
            # 已激活：从激活日 0 点起算 N 天（与 activate_card 口径一致）；
            # 未激活：仍留空，等首次登录时按新 days 现算，避免"改期等于提前开表"。
            activated = str(rec.get("activated_at") or "").strip()
            if activated:
                act_day = datetime.strptime(activated[:10], "%Y-%m-%d").date()
                rec["expires"] = (act_day + timedelta(days=days)).strftime("%Y-%m-%d")
            else:
                rec["expires"] = ""
        else:
            rec["expires"] = ""
    _card_file_write(base_dir, cards)
    return {"prefix": prefix, **rec}


def list_cards(base_dir: str) -> list:
    """列出全部卡密（脱敏：完整 key 只回前缀 + 尾 4 位，防批量泄露）。"""
    cards = load_card_keys(base_dir)
    out = []
    for prefix, rec in cards.items():
        full = str(rec.get("key") or "")
        tail = full[-4:] if len(full) >= 4 else ""
        out.append({
            "prefix": prefix,
            "key_masked": f"{prefix}-****{tail}",
            "owner": rec.get("owner", ""),
            "note": rec.get("note", ""),
            "created_at": rec.get("created_at", ""),
            "expires": rec.get("expires", ""),
            "days": rec.get("days", 0),
            "activated_at": rec.get("activated_at", ""),
            "is_activated": bool(rec.get("activated_at")),
        })
    return out


def is_admin_token(base_dir: str, token: str) -> bool:
    """判断是否为管理员 token（sub == "admin"）。

    卡密用户的 sub 是 "card:<prefix>"，必须与之区分——
    否则卡密用户拿到自己的 token 就能改别人的卡。
    """
    payload = verify_token(base_dir, token)
    if not payload:
        return False
    return str(payload.get("sub") or "") == "admin"


def is_card_token(base_dir: str, token: str) -> bool:
    """判断是否为卡密用户 token（sub 以 "card:" 开头）。"""
    payload = verify_token(base_dir, token)
    if not payload:
        return False
    return str(payload.get("sub") or "").startswith("card:")


def user_session_id(base_dir: str, token: str) -> str:
    """从 token 解析出该用户有权操作的 session_id 前缀。

    返回值：
    - 小程序 token (sub=wx:<openid>) → "wx_" 前缀
    - 卡密 token (sub=card:<prefix>) → "card_" 前缀
    - 管理员 token / 非法 token → "" （无用户会话归属）

    调用方据此校验：请求中的 session_id 是否以该前缀开头。
    """
    payload = verify_token(base_dir, token)
    if not payload:
        return ""
    sub = str(payload.get("sub") or "")
    if sub.startswith("wx:"):
        return "wx_"
    if sub.startswith("card:"):
        return "card_"
    return ""


# ------------------------------------------------------------------
# 注册：路由 + 中间件
# ------------------------------------------------------------------

def register_auth(app: FastAPI, base_dir: str) -> None:
    """挂载鉴权路由与中间件。"""

    # ---- 登录 ----
    @app.post("/api/auth/login")
    async def api_auth_login(request: Request):
        cfg = load_auth(base_dir)
        try:
            body = await request.json()
        except Exception:
            body = {}
        pwd = str(body.get("password", ""))

        rec = cfg.get("password")
        if not rec:
            # 首次登录：用请求中的密码初始化（等价于"首次设置即管理员密码"）
            if not pwd:
                return JSONResponse({"status": False, "msg": "首次登录请携带 password 以设置管理员密码"})
            if not set_password(base_dir, pwd):
                return JSONResponse({"status": False, "msg": "凭据写入失败，请检查目录权限"})
            logger.warning("首次登录已初始化管理员密码")
        else:
            if not _verify_password(pwd, rec):
                return JSONResponse({"status": False, "msg": "密码错误"}, status_code=401)

        ttl = int(cfg.get("token_ttl_hours") or DEFAULT_TTL_HOURS)
        token = create_token(base_dir, ttl)
        return JSONResponse({
            "status": True,
            "data": {"token": token, "expires_in": ttl * 3600},
            "msg": "登录成功",
        })

    # ---- 微信登录：code 换 openid，并绑定稳定会话 ----
    @app.post("/api/auth/wxlogin")
    async def api_auth_wxlogin(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        code = str(body.get("code", "")).strip()
        if not code:
            return JSONResponse({"status": False, "msg": "缺少参数 code"}, status_code=400)

        result = wx_code2session(base_dir, code)
        if not result.get("ok"):
            return JSONResponse(
                {"status": False, "msg": result.get("msg", "微信登录失败")}, status_code=401
            )

        openid = result["openid"]
        cfg = load_auth(base_dir)
        ttl = int(cfg.get("token_ttl_hours") or DEFAULT_TTL_HOURS)
        token = create_token(base_dir, ttl, sub=f"wx:{openid}")
        return JSONResponse({
            "status": True,
            "data": {
                "token": token,
                "expires_in": ttl * 3600,
                # 返回绑定的会话 ID：小程序用它续聊，同一用户始终回到同一会话
                "session_id": wx_session_id(openid),
            },
            "msg": "登录成功",
        })

    # ---- 卡密登录：卡密验证 → 签发 token，与 wxlogin 完全对称 ----
    @app.post("/api/auth/cardlogin")
    async def api_auth_cardlogin(request: Request):
        """卡密登录：验证完整卡密，签发 JWT（sub=card:<前6位>）。

        与微信登录对称设计：
        - 微信用 code 换 openid → sub=wx:<openid>；
        - 卡密用 raw_key 验前缀 → sub=card:<prefix>。
        两者共享同一套 /api/mp/* 用户存储与 clawbot 扫码绑定接口。
        """
        try:
            body = await request.json()
        except Exception:
            body = {}
        raw_key = str(body.get("key", "")).strip()
        if not raw_key:
            return JSONResponse({"status": False, "msg": "缺少参数 key"}, status_code=400)

        ok, result = verify_card_key(base_dir, raw_key)
        if not ok:
            return JSONResponse(
                {"status": False, "msg": result}, status_code=401)

        prefix = result          # 通过验证后 result 就是前 6 位
        cfg = load_auth(base_dir)
        ttl = int(cfg.get("token_ttl_hours") or DEFAULT_TTL_HOURS)
        token = create_token(base_dir, ttl, sub=f"card:{prefix}")
        cards = load_card_keys(base_dir)
        rec = cards.get(prefix, {})
        # 首次登录激活：落 activated_at 并算出 expires（幂等，已激活则不变）
        rec = activate_card(base_dir, prefix)
        expires = str(rec.get("expires") or "").strip()
        days = int(rec.get("days") or 0)
        created_at = str(rec.get("created_at") or "").strip()
        activated_at = str(rec.get("activated_at") or "").strip()
        owner = str(rec.get("owner") or "").strip()
        return JSONResponse({
            "status": True,
            "data": {
                "token": token,
                "expires_in": ttl * 3600,
                "session_id": card_session_id(prefix),
                "prefix": prefix,
                "owner": owner,
                "expires": expires if expires else "长期有效",
                "days": days,
                "created_at": created_at,
                "activated_at": activated_at,
                "is_activated": bool(activated_at),
            },
            "msg": "登录成功",
        })

    # ---- 续期：用旧 token 换新 token（滑动续期）----
    @app.post("/api/auth/refresh")
    async def api_auth_refresh(request: Request):
        """用旧令牌换发新令牌，避免 24 小时到期后被迫重新微信授权。

        为什么必须单独开接口：JWT 的 exp 签出即固定，服务端没有会话表可
        以顺延。前端拿到 401（或主动定时调用）时带着旧 token 来这里，
        验明身份后签一张新的，用户侧完全无感。

        安全边界：
        - 签名与 issuer 仍强制校验，只放宽 exp 一项；
        - 过期超过宽限期（默认 30 天）拒绝，退回要求重新登录；
        - 不提升权限：新 token 的 sub 沿用旧 token，杜绝横向越权；
        - 服务令牌（sub=service:*）不参与，它本就是 5 年长期令牌。
        """
        cfg = load_auth(base_dir)
        token = extract_token(request)
        if not token:
            return JSONResponse(
                {"status": False, "msg": "缺少令牌", "code": 401}, status_code=401)

        grace_days = int(cfg.get("refresh_grace_days") or DEFAULT_REFRESH_GRACE_DAYS)
        grace_seconds = max(0, grace_days) * 86400
        payload = verify_token_allow_expired(base_dir, token, grace_seconds)
        if not payload:
            return JSONResponse(
                {"status": False, "msg": "令牌已失效，请重新登录", "code": 401},
                status_code=401)

        sub = str(payload.get("sub") or "")
        if not sub or sub.startswith("service:"):
            return JSONResponse(
                {"status": False, "msg": "该令牌不支持续期", "code": 401},
                status_code=401)

        ttl = int(cfg.get("token_ttl_hours") or DEFAULT_TTL_HOURS)
        new_token = create_token(base_dir, ttl, sub=sub)
        data = {"token": new_token, "expires_in": ttl * 3600}
        # 微信用户沿用同一会话：续期不该把用户踢到新会话，否则聊天记录"丢失"
        if sub.startswith("wx:"):
            data["session_id"] = wx_session_id(sub[3:])
        # 卡密用户同样沿用同一会话（key 不变则前缀不变 → 会话 ID 不变）
        elif sub.startswith("card:"):
            data["session_id"] = card_session_id(sub[5:])
        return JSONResponse({
            "status": True, "data": data, "msg": "已续期",
        })

    # ---- 状态：前端据此判断是否需要显示登录页 ----
    @app.get("/api/auth/status")
    async def api_auth_status():
        cfg = load_auth(base_dir)
        return JSONResponse({
            "status": True,
            "data": {
                "enabled": bool(cfg.get("enabled")),
                "initialized": bool(cfg.get("password")),
            },
        })

    @app.post("/api/auth/logout")
    async def api_auth_logout():
        # 无状态 JWT：服务端不维护会话，登出由前端丢弃 token 完成。
        return JSONResponse({"status": True, "msg": "已登出"})

    # ---- 中间件 ----
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        cfg = load_auth(base_dir)
        path = request.url.path

        # 未启用鉴权 → 完全放行，保持既有部署行为
        if not cfg.get("enabled"):
            return await call_next(request)

        if is_public(path):
            return await call_next(request)

        # OPTIONS 预检由 CORS 中间件处理，需放行否则浏览器跨域失败
        if request.method == "OPTIONS":
            return await call_next(request)

        token = extract_token(request)
        if not token or not verify_token(base_dir, token):
            return JSONResponse(
                {"status": False, "msg": "未授权：请重新登录", "code": 401},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
