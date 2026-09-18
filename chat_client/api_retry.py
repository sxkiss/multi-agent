"""
API 重试公共模块：agent.py / single_agent.py 两处共用。

背景：openai SDK 3.x 基于 httpx2（重命名版），流式中断抛 httpx2.RemoteProtocolError
等异常，这些异常不是 openai.OpenAIError 的子类；部分断流场景还会直接抛裸 OSError
（历史日志出现 [Errno 9] Bad file descriptor）。因此统一在此定义可重试判定，
三份调用代码保持一致，避免"修复 A 漏 B"。
"""

import json

import openai

try:
    import httpx2
except ImportError:  # 环境无 httpx2（未使用 openai SDK 3.x）时降级为空元组
    httpx2 = None

# openai SDK 3.x 基于 httpx2（重命名版）：流式中断/连接异常均属此家族
HTTPX_RETRYABLE = (
    httpx2.RemoteProtocolError, httpx2.ConnectError, httpx2.ReadError,
    httpx2.ProtocolError, httpx2.TransportError, httpx2.HTTPError,
    httpx2.TimeoutException, httpx2.WriteError, httpx2.CloseError,
) if httpx2 else ()

# 常规可重试集合：openai 官方 + JSON 解析失败（代理偶发返回空/非 JSON 响应体）+ httpx2
RETRYABLE = (
    openai.APIConnectionError, openai.RateLimitError,
    openai.InternalServerError, json.JSONDecodeError,
) + HTTPX_RETRYABLE

# 裸 OSError errno 白名单：断流/连接重置时 httpx 可能直接抛裸 OSError，不包装成
# httpx2 异常。errno 含义：
#   9(EBADF) 104(ECONNRESET) 32(EPIPE) 110(ETIMEDOUT) 112(EHOSTUNREACH) 113(ECONNREFUSED)
RETRYABLE_ERRNOS = {9, 32, 104, 110, 112, 113}

# 可重试的 HTTP 状态码（400/408/409/425/499 为代理常见瞬时错误，5xx 服务端瞬时错误）
RETRYABLE_STATUS_CODES = (400, 408, 409, 425, 429, 499, 500, 502, 503, 504)


def is_retryable_api_err(e) -> bool:
    """判断异常是否值得重试。

    - isinstance 命中 openai / httpx2 / JSONDecodeError 家族 → True
    - 携带 status_code 且在可重试状态码范围 → True
    - 裸 OSError 且 errno 在断流白名单 → True
    """
    if isinstance(e, RETRYABLE):
        return True
    status = getattr(e, "status_code", None)
    if status in RETRYABLE_STATUS_CODES:
        return True
    if isinstance(e, OSError) and e.errno in RETRYABLE_ERRNOS:
        return True
    return False