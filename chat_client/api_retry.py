"""
API 重试公共模块：agent.py / retrieval.py 共用。

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


# Responses 协议错误码 → 可重试集合。agnes 的 response.failed 事件里
# ResponseError.code 是字符串枚举（不是 HTTP 状态码），只有这几类是瞬时/服务端
# 故障，值得重试；其余（invalid_prompt / bio_policy / 图片相关）是确定性错误，
# 原样重试必然再失败，直接抛出。
RETRYABLE_RESPONSE_CODES = {
    "server_error", "rate_limit_exceeded", "vector_store_timeout",
    # 上游流中断/读取失败：典型瞬时故障（实测 agnes 代理返回
    # code='upstream_stream_read_error'、消息 'Upstream response stream was
    # interrupted'），重发请求往往即恢复，必须重试。
    "upstream_stream_read_error", "stream_read_error", "stream_error",
    "upstream_error", "upstream_connect_error", "upstream_timeout",
}

# 兜底：错误码未知时，按消息里的瞬时故障关键词判定。
# 各家代理自定义 code 名目繁多（不可能穷举），而"流被中断/超时/连接重置"
# 这类语义在 message 里高度一致，据此兜底可避免漏重试。
RETRYABLE_MSG_KEYWORDS = (
    "stream was interrupted", "stream interrupted", "interrupted",
    "timeout", "timed out", "connection reset", "connection_reset",
    "temporarily unavailable", "try again", "upstream",
    "econnreset", "epipe", "broken pipe", "incomplete chunked read",
    "server disconnected", "remote end closed",
)


class ResponsesAPIError(Exception):
    """Responses 协议层故障（response.failed / error 事件）的统一异常。

    携带 status_code / code，使 is_retryable_api_err 能判定是否重试，与
    chat.completions 路径的 openai.APIStatusError 行为对齐。无状态码/未知
    错误码时按"不可重试"处理，避免对确定性 4xx 死循环。
    """

    def __init__(self, message: str, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code

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
    code = getattr(e, "code", None)
    if code in RETRYABLE_RESPONSE_CODES:
        return True
    # 兜底：错误码不在白名单时，看消息是否含瞬时故障关键词
    if isinstance(e, ResponsesAPIError):
        msg = str(e.args[0] if e.args else "").lower()
        if any(k in msg for k in RETRYABLE_MSG_KEYWORDS):
            return True
    if isinstance(e, OSError) and e.errno in RETRYABLE_ERRNOS:
        return True
    return False


def _obj_get(obj: object, key: str) -> object:
    """统一读取属性/字典键，兼容 pydantic 对象与 dict 两种形态。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def extract_responses_status(ev: object) -> int | None:
    """从 Responses 的 error/failed 事件中提取 HTTP 状态码（若能拿到）。

    agnes 在 response.failed / error 事件中通常把上游 HTTP 状态放在
    error/response.error 的 status_code 字段；拿不到则 None。
    """
    for attr in ("error", "response"):
        obj = _obj_get(ev, attr)
        if obj is None:
            continue
        for key in ("status_code", "status", "code"):
            val = _obj_get(obj, key)
            if isinstance(val, int):
                return val
    return None


def extract_responses_code(ev: object) -> str | None:
    """从 Responses 的 error/failed 事件中提取字符串错误码（ResponseError.code）。

    agnes 的 response.failed 事件里 ResponseError.code 是字符串枚举
    （server_error / rate_limit_exceeded / invalid_prompt ...），并非 HTTP 状态码，
    因此必须单独提取才能判断是否值得重试。
    """
    for attr in ("error", "response"):
        obj = _obj_get(ev, attr)
        if obj is None:
            continue
        # response.error 才是 ResponseError（含 code/message）
        inner = _obj_get(obj, "error")
        if inner is None:
            inner = obj
        val = _obj_get(inner, "code")
        if isinstance(val, str):
            return val
    return None