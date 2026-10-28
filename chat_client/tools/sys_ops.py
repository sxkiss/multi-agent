"""
系统运维增强工具集：磁盘排查 / 网络诊断 / 日志快查 / 文件归档 / 进程与服务管理 / 计划任务。
所有高危操作依赖 registry 的风险护栏（默认需用户确认），危险命令黑名单始终生效。
"""
import logging
import os
import re
import ssl
import tarfile
import time
import zipfile
from datetime import datetime

from . import PROJECT_ROOT, register_tool
from .agent_tools import _run_shell_cmd
from .base import _xml_response

logger = logging.getLogger(__name__)

# 伪文件系统与无需扫描的目录
_PRUNE_DIRS = {"/proc", "/sys", "/dev", "/run", "/snap",
               "node_modules", "__pycache__", ".git", ".venv", "venv"}


@register_tool(category="网络", name_cn="URL状态检测", risk_level="low")
def http_check(url: str, timeout_seconds: int = 10) -> str:
    """
    检测 HTTP(S) 地址可用性：状态码、响应耗时、重定向目标、内容预览。比 curl_url 输出更结构化。

    Args:
        url: 完整 URL（http:// 或 https://）
        timeout_seconds: 超时秒数，默认 10
    """
    import requests as _requests
    if not str(url).lower().startswith(("http://", "https://")):
        return _xml_response("error", f"仅支持 http:// 或 https:// 协议: {url}")
    try:
        t0 = time.time()
        resp = _requests.get(url, timeout=min(max(int(timeout_seconds), 1), 60),
                             allow_redirects=False,
                             headers={"User-Agent": "Mozilla/5.0 (compatible; ops-agent)"})
        cost = time.time() - t0
        info = [
            f"URL: {url}",
            f"状态码: {resp.status_code}",
            f"耗时: {cost:.2f}s",
            f"服务器: {resp.headers.get('server', 'N/A')}",
        ]
        if 300 <= resp.status_code < 400:
            info.append(f"重定向至: {resp.headers.get('location', 'N/A')}")
        snippet = resp.text[:300].replace("\n", " ")
        info.append(f"内容预览: {snippet}")
        verdict = "✅ 可用" if resp.status_code < 400 else "❌ 异常"
        return _xml_response("done", f"[{verdict}]\n" + "\n".join(info))
    except _requests.Timeout:
        return _xml_response("error", f"请求超时（>{timeout_seconds}s）")
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="网络", name_cn="SSL证书检测", risk_level="low")
def check_ssl_cert(domain: str, port: int = 443) -> str:
    """
    检测域名 HTTPS 证书的有效期与颁发信息，剩余不足 30 天给出告警。

    Args:
        domain: 域名（不含 https:// 前缀）
        port: 端口，默认 443
    """
    host = str(domain).strip().replace("https://", "").replace("http://", "").split("/")[0]
    try:
        pem = ssl.get_server_certificate((host, int(port)), timeout=10)
    except TimeoutError:
        return _xml_response("error", f"连接超时: {host}:{port}")
    except Exception as e:
        return _xml_response("error", f"获取证书失败: {e!s}")

    # 写临时文件供解析器使用
    tmp = os.path.join("/tmp", f"_sslchk_{int(time.time() * 1000)}.pem")
    decoded = None
    subject_cn = issuer_cn = ""
    try:
        with open(tmp, "w") as f:
            f.write(pem)
        try:
            from cryptography import x509  # noqa: F401
            decoded = ssl._ssl._test_decode_cert(tmp)
        except ImportError:
            decoded = ssl._ssl._test_decode_cert(tmp)
    except Exception:
        decoded = None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            logger.warning("异常被静默吞掉，已记录", exc_info=True)

    if not decoded:
        return _xml_response("error", "证书解析失败")

    subject_cn = next((v for item in decoded.get("subject", ()) for k, v in item if k == "commonName"), "")
    issuer_cn = next((v for item in decoded.get("issuer", ()) for k, v in item if k == "commonName"), "")
    not_before = decoded.get("notBefore", "")
    not_after = decoded.get("notAfter", "")

    lines = [
        f"域名: {host}:{port}",
        f"主题 CN: {subject_cn or 'N/A'}",
        f"颁发者: {issuer_cn or 'N/A'}",
        f"生效时间: {not_before}",
        f"到期时间: {not_after}",
    ]

    if not_after:
        days_left = ""
        try:
            dt = datetime.strptime(not_after.replace(" GMT", " +0000"), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            try:
                dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
            except Exception:
                dt = None
        if dt is not None:
            from datetime import timezone
            now_utc = datetime.now(timezone.utc)
            days_left = (dt - now_utc).days
            warn = ""
            if days_left < 0:
                warn = " ❌ 已过期！"
            elif days_left <= 30:
                warn = " ⚠️ 即将到期（30 天内）"
            lines.append(f"剩余天数: {days_left} 天{warn}")

    return _xml_response("done", "\n".join(lines))


