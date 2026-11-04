"""统一的外部内容安全工具。

集中处理两类高风险输入：
1. 用户提供的 URL 下载（防 SSRF、禁止/逐跳校验重定向、限制体积）；
2. ZIP/TAR 解压（防 Zip-Slip、符号链接、硬链接、设备文件与体积炸弹）。

这里的函数只负责“校验 + 安全写入”，不承担业务语义。调用方仍应设置
自己的业务限制（例如技能名、SKILL.md 位置等）。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import stat
import tarfile
import zipfile
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse


class UnsafeURLError(ValueError):
    """URL 指向内网、非公网地址或使用了不允许的协议。"""


class UnsafeArchiveError(ValueError):
    """压缩包包含越界路径、链接或特殊文件。"""


def _is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    # is_global 已排除私有、回环、链路本地、保留、组播、未指定地址；
    # 这里显式再挡一次，避免不同 Python 版本语义差异。
    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
    )


def _resolve_host_ips(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"无法解析域名: {host}") from exc
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0] not in ips:
            ips.append(sockaddr[0])
    return ips


def validate_public_url(url: str, *, allow_private: bool = False) -> str:
    """校验 URL 只指向公网 HTTP(S) 地址。

    - 拒绝 userinfo（http://user@host/ 这类混淆写法）；
    - 拒绝 localhost / .local / .internal 等明显内网域名；
    - IP 字面量直接校验，域名则解析全部 A/AAAA 记录并逐个校验；
    - allow_private=True 时仅跳过内网判断（保留协议等基础校验）。
    """
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https"):
        raise UnsafeURLError("仅支持 http:// 或 https:// 链接")
    if not parsed.hostname:
        raise UnsafeURLError("URL 缺少主机名")
    if parsed.username or parsed.password:
        raise UnsafeURLError("URL 不允许携带用户名或密码")
    host = parsed.hostname.strip().lower().rstrip(".")
    if not host:
        raise UnsafeURLError("URL 缺少主机名")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("URL 端口非法") from exc
    if not (1 <= port <= 65535):
        raise UnsafeURLError("URL 端口非法")

    if allow_private:
        return raw

    if host in ("localhost", "localhost.localdomain") or host.endswith(
        (".local", ".internal", ".localhost")
    ):
        raise UnsafeURLError(f"拒绝访问内网/本机地址: {host}")

    # 优先按 IP 字面量判断（含 IPv6）。ipaddress 同时处理了十进制/八进制
    # 形式被系统解析前的常见写法：这类写法不是合法 IP 字面量，会走域名解析，
    # 解析结果仍会逐个校验。
    try:
        ipaddress.ip_address(host)
        if not _is_public_ip(host):
            raise UnsafeURLError(f"拒绝访问内网/本机地址: {host}")
        return raw
    except ValueError:
        pass

    ips = _resolve_host_ips(host, port)
    if not ips:
        raise UnsafeURLError(f"域名无解析结果: {host}")
    bad = [ip for ip in ips if not _is_public_ip(ip)]
    if bad:
        raise UnsafeURLError(f"域名解析到非公网地址: {host} -> {', '.join(bad)}")
    return raw


def fetch_public_url(
    url: str,
    *,
    timeout: tuple[float, float] = (10, 60),
    headers: Optional[dict] = None,
    max_bytes: int = 30 * 1024 * 1024,
    max_redirects: int = 5,
    allow_private: bool = False,
):
    """下载公网 URL，逐跳校验重定向并限制体积。

    返回 ``(data, final_url)``。所有重定向目标在发起请求前重新做完整校验，
    跨域重定向不会被静默放行。
    """
    import requests

    current = validate_public_url(url, allow_private=allow_private)
    for _ in range(max_redirects + 1):
        validate_public_url(current, allow_private=allow_private)
        resp = requests.get(
            current,
            timeout=timeout,
            stream=True,
            allow_redirects=False,
            headers=headers or {"User-Agent": "ai-agent-secure-fetch"},
        )
        try:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise UnsafeURLError("重定向缺少 Location")
                current = urljoin(current, location)
                continue
            if resp.status_code != 200:
                raise UnsafeURLError(f"下载失败，HTTP {resp.status_code}: {current}")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeURLError(
                        f"文件超过下载上限（{max_bytes // 1024 // 1024}MB）"
                    )
                chunks.append(chunk)
            return b"".join(chunks), current
        finally:
            resp.close()
    raise UnsafeURLError(f"重定向次数超过 {max_redirects} 次")


def _safe_member_parts(name: str) -> list[str]:
    normalized = str(name or "").replace("\\", "/")
    if not normalized or "\x00" in normalized:
        raise UnsafeArchiveError("压缩包包含空文件名")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise UnsafeArchiveError(f"压缩包含绝对路径成员: {name}")
    if len(normalized) >= 2 and normalized[1] == ":":
        raise UnsafeArchiveError(f"压缩包含盘符路径成员: {name}")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchiveError(f"压缩包含路径穿越成员: {name}")
    return parts


def _inside(base_real: str, path: str) -> bool:
    real = os.path.realpath(path)
    return real == base_real or real.startswith(base_real + os.sep)


def validate_zip_members(zf: zipfile.ZipFile) -> None:
    """在解压前校验全部 ZIP 成员。"""
    for info in zf.infolist():
        if info.is_dir() or info.filename.endswith("/"):
            continue
        mode = (info.external_attr >> 16) & 0xFFFF
        ftype = stat.S_IFMT(mode)
        if ftype == stat.S_IFLNK:
            raise UnsafeArchiveError(f"压缩包含符号链接成员: {info.filename}")
        if ftype and ftype not in (stat.S_IFREG, stat.S_IFDIR):
            raise UnsafeArchiveError(f"压缩包含特殊文件成员: {info.filename}")
        if info.flag_bits & 0x1:
            raise UnsafeArchiveError(f"压缩包含加密成员，已拒绝: {info.filename}")
        _safe_member_parts(info.filename)


def safe_extract_zip(
    zf: zipfile.ZipFile,
    dest: str,
    *,
    strip_prefix: str = "",
    max_total_bytes: Optional[int] = None,
    max_members: Optional[int] = None,
) -> int:
    """安全解压 ZIP 到 dest，返回写入文件数。

    保留调用方原有的 prefix 语义：prefix 之外的成员不落地，但所有成员都会
    先经过安全校验，不能被“prefix 外藏恶意成员”绕过。
    """
    validate_zip_members(zf)
    infos = [i for i in zf.infolist() if not (i.is_dir() or i.filename.endswith("/"))]
    if max_members is not None and len(infos) > max_members:
        raise UnsafeArchiveError(f"压缩包文件数超出限制 ({len(infos)} > {max_members})")

    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    written_total = 0
    extracted = 0
    prefix = strip_prefix.replace("\\", "/")
    for info in infos:
        name = info.filename.replace("\\", "/")
        rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
        if not rel:
            continue
        parts = _safe_member_parts(rel)
        if not parts:
            continue
        target = os.path.join(dest_real, *parts)
        if not _inside(dest_real, target):
            raise UnsafeArchiveError(f"压缩包成员越界: {info.filename}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # 创建父目录后再校验一次，防止已有软链接把子目录带出根目录。
        if not _inside(dest_real, target):
            raise UnsafeArchiveError(f"压缩包成员越界: {info.filename}")
        with zf.open(info) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written_total += len(chunk)
                if max_total_bytes is not None and written_total > max_total_bytes:
                    raise UnsafeArchiveError("实际解压体积超出限制")
                out.write(chunk)
        extracted += 1
    return extracted


def validate_tar_members(
    tf: tarfile.TarFile, members: Optional[Iterable[tarfile.TarInfo]] = None
) -> list[tarfile.TarInfo]:
    """校验 TAR 成员并返回可安全解压的常规文件/目录列表。"""
    selected = list(members) if members is not None else tf.getmembers()
    safe: list[tarfile.TarInfo] = []
    for member in selected:
        if not (_is_safe_tar_member(member)):
            raise UnsafeArchiveError(
                f"备份包含链接或特殊文件成员，已拒绝: {member.name}"
            )
        _safe_member_parts(member.name)
        safe.append(member)
    return safe


def _is_safe_tar_member(member: tarfile.TarInfo) -> bool:
    if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo():
        return False
    return member.isdir() or member.isfile()


def safe_extract_tar(
    tf: tarfile.TarFile,
    dest: str,
    *,
    members: Optional[Iterable[tarfile.TarInfo]] = None,
) -> int:
    """安全解压 TAR（支持 tar.gz）到 dest，返回解压条目数。"""
    safe_members = validate_tar_members(tf, members)
    os.makedirs(dest, exist_ok=True)
    dest_real = os.path.realpath(dest)
    for member in safe_members:
        target = os.path.join(dest_real, *[p for p in member.name.replace("\\", "/").split("/") if p])
        if not _inside(dest_real, target):
            raise UnsafeArchiveError(f"备份成员越界: {member.name}")
        if member.isdir():
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        src = tf.extractfile(member)
        if src is None:
            raise UnsafeArchiveError(f"备份成员无法读取: {member.name}")
        with src, open(target, "wb") as out:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
    return len(safe_members)
