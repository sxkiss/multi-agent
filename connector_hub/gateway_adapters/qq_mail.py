"""QQ mail and other IMAP/SMTP mailbox gateway."""

from __future__ import annotations

import contextlib
import email
import imaplib
import json
import queue
import smtplib
import threading
import time
from email import policy
from email.mime.text import MIMEText
from typing import Any

from connector_hub.gateway_adapters.local_shims import (
    IMAP_CONNECT_TIMEOUT,
    NETEASE_IMAP_HOSTS,
    resolve_mail_servers,
    correct_netease_imap_host,
    correct_netease_smtp_host,
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_emails",
        "description": "Search recent emails in INBOX (IMAP)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "IMAP search criteria, default ALL",
                },
                "limit": {"type": "integer", "description": "Max messages, default 10"},
            },
        },
    },
    {
        "name": "read_email",
        "description": "Read a single email body by UID",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "IMAP UID"},
            },
            "required": ["uid"],
        },
    },
    {
        "name": "send_email",
        "description": "Send a plain-text email via SMTP",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]

_IMAP_ID_ARGS = (
    '("name" "Octop" "version" "1.0.0" "vendor" "Octop" "support-email" "support@octop.local")'
)

# imaplib 默认未注册 ID 命令。
imaplib.Commands["ID"] = ("AUTH", "NONAUTH", "SELECTED")


def list_tools() -> list[dict[str, Any]]:
    return TOOLS


def call_tool(creds: dict[str, Any], name: str, args: dict[str, Any]) -> str:
    if name == "search_emails":
        return _email_search(creds, args)
    if name == "read_email":
        return _email_read(creds, args)
    if name == "send_email":
        return _email_send(creds, args)
    raise ValueError(f"unknown tool: {name}")


def _parse_message(raw: bytes) -> email.message.Message:
    """Parse raw RFC822 bytes with ``email.policy.default``.

    The default compat32 policy returns ``email.header.Header`` objects for
    header fields carrying raw (non MIME-encoded) non-ASCII bytes; those are
    not JSON serializable and crash ``search_emails``/``read_email`` with
    ``TypeError: Object of type Header is not JSON serializable``. The modern
    policy always yields ``str`` subclasses and additionally decodes
    MIME-encoded words (so subjects render as readable text instead of
    ``=?utf-8?q?...?=``).
    """
    return email.message_from_bytes(raw, policy=policy.default)


def _safe_header(msg: email.message.Message, field: str) -> str:
    """Return *field* as a plain ``str``, safe for JSON under any policy."""
    return str(msg.get(field, "") or "")


def _email_search(creds: dict[str, Any], args: dict[str, Any]) -> str:
    query = str(args.get("query") or "ALL")
    limit = int(args.get("limit") or 10)
    imap = _imap_session(creds)
    try:
        imap.select("INBOX")
        # NetEase rejects CHARSET UTF-8 on UID SEARCH; omit charset for compatibility.
        _typ, data = imap.uid("search", query)
        uids = (data[0] or b"").split()
        uids = uids[-limit:]
        out: list[dict[str, str]] = []
        for uid in reversed(uids):
            _typ, msg_data = imap.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            if not msg_data or not msg_data[0]:
                continue
            hdr = _parse_message(msg_data[0][1])
            out.append(
                {
                    "uid": uid.decode(),
                    "from": _safe_header(hdr, "From"),
                    "subject": _safe_header(hdr, "Subject"),
                    "date": _safe_header(hdr, "Date"),
                }
            )
        return json.dumps(out, ensure_ascii=False, indent=2)
    finally:
        _release(creds, imap)


def _email_read(creds: dict[str, Any], args: dict[str, Any]) -> str:
    uid = str(args.get("uid") or "").strip()
    if not uid:
        raise ValueError("uid is required")
    imap = _imap_session(creds)
    try:
        imap.select("INBOX")
        _typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if not msg_data or not msg_data[0]:
            raise ValueError(f"email uid {uid} not found")
        msg = _parse_message(msg_data[0][1])
        body = _extract_body(msg)
        return json.dumps(
            {
                "uid": uid,
                "from": _safe_header(msg, "From"),
                "subject": _safe_header(msg, "Subject"),
                "date": _safe_header(msg, "Date"),
                "body": body,
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        _release(creds, imap)


def _email_send(creds: dict[str, Any], args: dict[str, Any]) -> str:
    to_addr = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or "")
    if not to_addr:
        raise ValueError("to is required")
    from_addr = str(creds.get("email") or "")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    host = correct_netease_smtp_host(from_addr, creds.get("smtp_host"))
    port = int(creds.get("smtp_port") or 587)
    with smtplib.SMTP(host, port, timeout=IMAP_CONNECT_TIMEOUT) as smtp:
        smtp.starttls()
        smtp.login(from_addr, str(creds.get("password") or ""))
        smtp.send_message(msg)
    return json.dumps({"ok": True, "to": to_addr}, ensure_ascii=False)


def _should_send_imap_id(imap: imaplib.IMAP4_SSL, host: str) -> bool:
    if host in NETEASE_IMAP_HOSTS:
        return True
    caps = {str(c).upper() for c in (imap.capabilities or ())}
    return "ID" in caps


def _imap_send_id(imap: imaplib.IMAP4_SSL) -> None:
    typ, _data = imap._simple_command("ID", _IMAP_ID_ARGS)
    if typ != "OK":
        raise imaplib.IMAP4.error(f"IMAP ID failed: {typ}")


def _imap_login(creds: dict[str, Any]) -> imaplib.IMAP4_SSL:
    user = str(creds.get("email") or "")
    host = correct_netease_imap_host(user, creds.get("imap_host"))
    port = int(creds.get("imap_port") or 993)
    password = str(creds.get("password") or "")
    imap: imaplib.IMAP4_SSL = imaplib.IMAP4_SSL(host, port, timeout=IMAP_CONNECT_TIMEOUT)
    if _should_send_imap_id(imap, host):
        _imap_send_id(imap)
    imap.login(user, password)
    return imap


# ---------------------------------------------------------------------------
# IMAP 连接池 + 登录重试
#
# 背景：QQ 邮箱 IMAP 对频繁登录有风控（AUTHENTICATIONFAILED / Login fail...
# login frequency limited）。旧实现每次工具调用都新建+登出连接，连续两次调用
# （如 search 后立刻 read）就会触发限制，只能靠人工等待恢复。
#
# 方案：
# 1) 连接池复用已登录连接，同一邮箱的操作共用连接，减少登录次数；
# 2) 登录失败时带退避重试（覆盖瞬时风控），重试仍失败才抛出。
# 池子按 (user, host, port) 键缓存；凭据变化时旧连接作废。空闲连接超过
# _POOL_IDLE_TTL 或被服务器断开时自动丢弃重建。
# ---------------------------------------------------------------------------

_POOL_IDLE_TTL = 240.0  # 秒；QQ 邮箱 IMAP 空闲超时较短，复用窗口保守取值
_LOGIN_RETRIES = 3
_LOGIN_RETRY_DELAYS = (2.0, 8.0)  # 第一次重试前等 2s，第二次等 8s

_pool_lock = threading.Lock()
_pool: dict[tuple[str, str, int], "queue.Queue[tuple[float, imaplib.IMAP4_SSL]]"] = {}
_pool_creds_sig: dict[tuple[str, str, int], str] = {}


def _creds_sig(creds: dict[str, Any]) -> str:
    return json.dumps(
        [str(creds.get("email") or ""), str(creds.get("password") or ""),
         str(creds.get("imap_host") or ""), str(creds.get("imap_port") or "")],
        ensure_ascii=False,
    )


def _pool_get(user: str, host: str, port: int, sig: str) -> imaplib.IMAP4_SSL | None:
    key = (user, host, port)
    with _pool_lock:
        if _pool_creds_sig.get(key) != sig:
            # 凭据已变化：清空该键下的旧连接
            old = _pool.pop(key, None)
            _pool_creds_sig.pop(key, None)
            if old is not None:
                _drain(old)
            return None
        q = _pool.get(key)
        if not q:
            return None
        try:
            born, imap = q.get_nowait()
        except queue.Empty:
            return None
    if time.monotonic() - born > _POOL_IDLE_TTL:
        _safe_logout(imap)
        return None
    # NOOP 既保活也校验连接仍可用；失败则丢弃
    try:
        if imap.noop()[0] != "OK":
            raise imaplib.IMAP4.error("NOOP not OK")
    except Exception:
        _safe_logout(imap)
        return None
    return imap


def _pool_put(user: str, host: str, port: int, sig: str, imap: imaplib.IMAP4_SSL) -> None:
    key = (user, host, port)
    with _pool_lock:
        stored = _pool_creds_sig.get(key)
        if stored is not None and stored != sig:
            _safe_logout(imap)
            return
        _pool_creds_sig[key] = sig
        q = _pool.setdefault(key, queue.Queue(maxsize=2))
        try:
            q.put_nowait((time.monotonic(), imap))
        except queue.Full:
            _safe_logout(imap)


def _drain(q: "queue.Queue[tuple[float, imaplib.IMAP4_SSL]]") -> None:
    while True:
        try:
            _born, imap = q.get_nowait()
        except queue.Empty:
            return
        _safe_logout(imap)


def _safe_logout(imap: imaplib.IMAP4_SSL) -> None:
    with contextlib.suppress(Exception):
        imap.logout()


def _release(creds: dict[str, Any], imap: imaplib.IMAP4_SSL) -> None:
    """Return a pooled connection after a tool call, or drop it if broken."""
    user = str(creds.get("email") or "")
    try:
        if imap.socket() is None:
            raise imaplib.IMAP4.error("connection already closed")
        if imap.noop()[0] != "OK":
            raise imaplib.IMAP4.error("NOOP not OK")
        host = getattr(imap, "host", "") or ""
        port = int(getattr(imap, "port", 0) or 0)
        _pool_put(user, host, port, _creds_sig(creds), imap)
    except Exception:
        _safe_logout(imap)


def _is_login_rate_limit_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "authenticationfailed" in text or "login fail" in text or "frequency" in text


def _imap_session(creds: dict[str, Any]) -> imaplib.IMAP4_SSL:
    """Return a logged-in IMAP connection: pool first, fresh login with retry otherwise."""
    user = str(creds.get("email") or "")
    host = correct_netease_imap_host(user, creds.get("imap_host"))
    port = int(creds.get("imap_port") or 993)
    sig = _creds_sig(creds)

    imap = _pool_get(user, host, port, sig)
    if imap is not None:
        return imap

    last_exc: BaseException | None = None
    for attempt in range(_LOGIN_RETRIES):
        try:
            fresh = _imap_login(creds)
            try:
                if fresh.select("INBOX")[0] != "OK":
                    raise imaplib.IMAP4.error("SELECT INBOX failed on fresh login")
            except Exception:
                _safe_logout(fresh)
                raise
            _pool_put(user, host, port, sig, fresh)
            return fresh
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < _LOGIN_RETRIES - 1 and _is_login_rate_limit_error(exc):
                time.sleep(_LOGIN_RETRY_DELAYS[min(attempt, len(_LOGIN_RETRY_DELAYS) - 1)])
                continue
            raise
    raise last_exc  # pragma: no cover


def probe_credentials(creds: dict[str, Any]) -> None:
    """Validate mailbox credentials via IMAP login + INBOX select."""
    # _imap_session already SELECTs INBOX on fresh logins and raises on failure;
    # pooled connections are kept alive for reuse instead of being logged out.
    _imap_session(creds)


def _decode_payload(part: email.message.Message, payload: bytes) -> str:
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        # Declared charset has no installed codec (e.g. "unknown-8bit", or a
        # bogus name from a broken client): fall back to UTF-8 instead of
        # crashing the whole read_email call.
        return payload.decode("utf-8", errors="replace")


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return _decode_payload(part, payload)
        return ""
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return _decode_payload(msg, payload)
    return str(payload or "")
