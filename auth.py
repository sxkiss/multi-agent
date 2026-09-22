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
PBKDF2_ITERATIONS = 200_000

# 免鉴权路径。登录接口本身 + 静态资源 + 首页 HTML。
# 首页必须放行：否则用户连登录页都加载不出来，形成"没 token → 打不开页面 →
# 无法登录"的死锁。静态资源同理。
PUBLIC_EXACT = {"/", "/index.html"}
PUBLIC_PREFIX = (
    "/api/auth/login",
    "/api/auth/status",
    "/api/auth/logout",
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


def create_token(base_dir: str, ttl_hours: int) -> str:
    now = int(time.time())
    payload = {
        "sub": "admin",
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
