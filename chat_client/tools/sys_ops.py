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


@register_tool(category="系统", name_cn="查找大文件", risk_level="low")
def find_large_files(path: str = "/", min_size_mb: int = 100, limit: int = 20) -> str:
    """
    递归查找指定目录下超过指定大小的文件，按大小降序返回，用于磁盘空间排查。

    Args:
        path: 扫描起始目录，默认 /
        min_size_mb: 最小文件大小（MB），默认 100
        limit: 返回条数上限，默认 20
    """
    try:
        if not os.path.isdir(path):
            return _xml_response("error", f"目录不存在: {path}")
        min_bytes = max(1, int(min_size_mb)) * 1024 * 1024
        limit = min(max(1, int(limit)), 200)

        results = []
        scanned = 0
        for root, dirs, files in os.walk(path):
            # 跳过伪文件系统与常见噪音目录
            if root == "/":
                dirs[:] = [d for d in dirs if f"/{d}" not in _PRUNE_DIRS]
            else:
                dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
            for name in files:
                scanned += 1
                if scanned > 200000:
                    results.sort(key=lambda x: x[0], reverse=True)
                    lines = [f"{s / 1024 / 1024:.1f}MB\t{p}" for s, p in results[:limit]]
                    return _xml_response("done", "[扫描达到上限 20 万个文件，结果可能不完整]\n" + "\n".join(lines))
                fp = os.path.join(root, name)
                try:
                    st = os.stat(fp)
                    import stat as _stat
                    if _stat.S_ISREG(st.st_mode) and st.st_size >= min_bytes:
                        results.append((st.st_size, fp))
                except OSError:

                    logger.warning("循环处理时跳过异常", exc_info=True)
                    continue
        results.sort(key=lambda x: x[0], reverse=True)
        results = results[:limit]
        if not results:
            return _xml_response("done", f"未找到超过 {min_size_mb}MB 的文件")
        total_mb = sum(s for s, _ in results) / 1024 / 1024
        lines = [f"{s / 1024 / 1024:.1f}MB\t{p}" for s, p in results]
        return _xml_response("done", f"共扫描 {scanned} 个文件，Top{len(results)} 大文件（合计 {total_mb:.0f}MB）:\n" + "\n".join(lines))
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="系统", name_cn="目录占用分析", risk_level="low")
def dir_usage(path: str = ".", limit: int = 20) -> str:
    """
    统计指定目录下各子目录/文件的磁盘占用并降序排列（类似 du -sh /*）。

    Args:
        path: 目标目录，默认当前工作区
        limit: 返回条数上限，默认 20
    """
    try:
        if not os.path.isdir(path):
            return _xml_response("error", f"目录不存在: {path}")
        limit = min(max(1, int(limit)), 100)

        def _tree_size(p: str, budget: dict) -> int:
            total = 0
            try:
                entries = list(os.scandir(p))
            except OSError:
                return 0
            for e in entries:
                if budget["n"] <= 0:
                    break
                budget["n"] -= 1
                try:
                    if e.is_symlink():
                        continue
                    if e.is_file(follow_symlinks=False):
                        total += e.stat(follow_symlinks=False).st_size
                    elif e.is_dir(follow_symlinks=False):
                        name = e.name
                        if name in _PRUNE_DIRS or (os.path.dirname(e.path) == "/" and f"/{name}" in _PRUNE_DIRS):
                            continue
                        total += _tree_size(e.path, budget)
                except OSError:

                    logger.warning("循环处理时跳过异常", exc_info=True)
                    continue
            return total

        rows = []
        budget = {"n": 100000}
        try:
            entries = list(os.scandir(path))
        except OSError as e:
            return _xml_response("error", str(e))
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    size = _tree_size(e.path, budget)
                    rows.append((size, e.name + "/"))
                elif e.is_file(follow_symlinks=False):
                    rows.append((e.stat(follow_symlinks=False).st_size, e.name))
            except OSError:

                logger.warning("循环处理时跳过异常", exc_info=True)
                continue
        rows.sort(key=lambda x: x[0], reverse=True)
        rows = rows[:limit]
        if not rows:
            return _xml_response("done", "目录为空")

        def human(n: float) -> str:
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if n < 1024 or unit == "TB":
                    return f"{int(n)}{unit}" if unit == "B" else f"{n:.1f}{unit}"
                n /= 1024
            return f"{n}B"

        output = [f"{human(sz)}\t{name}" for sz, name in rows]
        return _xml_response("done", f"{path} 占用排行:\n" + "\n".join(output))
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="网络", name_cn="端口占用查询", risk_level="low")
def list_port_bindings(port: int | None = None) -> str:
    """
    查询当前 TCP 监听端口及占用进程。可指定端口号过滤。

    Args:
        port: 可选，仅查询该端口
    """
    success, output = _run_shell_cmd(["ss", "-tlnp"], timeout=30)
    if not success:
        success, output = _run_shell_cmd(["netstat", "-tlnp"], timeout=30)
        if not success:
            return _xml_response("error", f"查询失败: {output}")

    lines = output.splitlines()
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            return _xml_response("error", "port 参数必须是整数")
        header = [l for l in lines if l.strip()][:2]
        matched = [l for l in lines if re.search(rf":{port}\b", l)]
        result = "\n".join(header + matched) if matched else f"端口 {port} 无监听"
        return _xml_response("done", result)
    return _xml_response("done", output[:8000])


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


@register_tool(category="Agent", name_cn="查看日志尾部", risk_level="medium")
def tail_log(file_path: str, lines: int = 100, keyword: str = "") -> str:
    """
    快速查看大日志文件末尾 N 行（分块读取，不整读文件），可按关键字过滤。

    Args:
        file_path: 日志文件绝对路径
        lines: 显示末尾行数，默认 100（最大 2000）
        keyword: 可选，仅保留包含该关键字的行
    """
    try:
        real = os.path.realpath(os.path.abspath(file_path))
        SENSITIVE = {"/etc/shadow", "/etc/gshadow", "/etc/sudoers"}
        if any(real == sp or real.startswith(sp + os.sep) for sp in SENSITIVE):
            return _xml_response("error", f"Access denied: 敏感文件不允许读取 ({file_path})")

        if not os.path.isfile(real):
            return _xml_response("error", f"文件不存在: {file_path}")
        n = min(max(1, int(lines)), 2000)

        with open(real, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            data = b""
            while len(data) < 2 * 1024 * 1024 and size > 0:
                step = min(8192, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
                if data.count(b"\n") > n + 500:
                    break

        text_lines = data.decode("utf-8", errors="replace").splitlines()
        tail = text_lines[-(n * 3):]
        if keyword:
            tail = [l for l in tail if keyword in l][-(n * 2):]
        shown = tail[-n:]
        header = f"<path>{real}</path> 共显示 {len(shown)} 行"
        if keyword:
            header += f"（过滤关键字: {keyword}）"
        return _xml_response("done", header + "\n" + "\n".join(shown))
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="Agent", name_cn="压缩文件", risk_level="high")
def compress_files(paths: list[str], archive_path: str) -> str:
    """
    将多个文件/目录打包为 .tar.gz 或 .zip 压缩包（按 archive_path 后缀自动选择格式）。高危操作需用户确认。

    Args:
        paths: 要打包的文件或目录绝对路径列表
        archive_path: 输出压缩包路径（以 .tar.gz / .tgz / .zip 结尾）
    """
    try:
        from .edit import _is_path_allowed
        if not str(archive_path).endswith((".tar.gz", ".tgz", ".zip")):
            return _xml_response("error", "archive_path 必须以 .tar.gz / .tgz / .zip 结尾")
        if not _is_path_allowed(archive_path):
            return _xml_response("error", f"Path not allowed: {archive_path}")
        if not paths:
            return _xml_response("error", "paths 不能为空")

        out_dir = os.path.dirname(os.path.abspath(archive_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        count = 0
        if archive_path.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for p in paths:
                    p = os.path.abspath(str(p))
                    if not os.path.exists(p):
                        continue
                    if os.path.isfile(p):
                        zf.write(p, arcname=os.path.basename(p))
                        count += 1
                    else:
                        base_dir = os.path.dirname(p)
                        for root, dirs, files in os.walk(p):
                            dirs[:] = [d for d in dirs if d not in _PRUNE_DIRS]
                            for fn in files:
                                fp = os.path.join(root, fn)
                                zf.write(fp, arcname=os.path.relpath(fp, base_dir))
                                count += 1
        else:
            with tarfile.open(archive_path, "w:gz") as tf:

                def _filter(ti):
                    if ti.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE):
                        return ti
                    return None

                for p in paths:
                    p = os.path.abspath(str(p))
                    if not os.path.exists(p):
                        continue
                    tf.add(p, arcname=os.path.basename(p), filter=_filter)
                    count += 1

        size = os.path.getsize(archive_path)
        return _xml_response("done", f"打包完成: {archive_path}（{size / 1024:.1f}KB，包含来源 {count} 个文件项）")
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="Agent", name_cn="解压文件", risk_level="high")
def extract_archive(archive_path: str, dest_dir: str) -> str:
    """
    解压 .tar.gz / .zip 到目标目录。内置 Zip-Slip / ../ 路径穿越防护，目标目录受白名单限制。高危操作需用户确认。

    Args:
        archive_path: 压缩包路径
        dest_dir: 解压目标目录（不存在会自动创建）
    """
    try:
        from .edit import _is_path_allowed
        if not _is_path_allowed(dest_dir) or not _is_path_allowed(archive_path):
            return _xml_response("error", "Path not allowed")
        if not os.path.isfile(archive_path):
            return _xml_response("error", f"压缩包不存在: {archive_path}")

        os.makedirs(dest_dir, exist_ok=True)
        dest_real = os.path.realpath(dest_dir)
        extracted = 0

        def _member_ok(name: str) -> bool:
            if name.startswith("/") or ".." in name.split("/"):
                return False
            target = os.path.realpath(os.path.join(dest_real, name))
            return target == dest_real or target.startswith(dest_real + os.sep)

        if archive_path.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                bad = [n for n in zf.namelist() if not _member_ok(n)]
                if bad:
                    return _xml_response("error", f"发现非法路径成员（疑似 Zip-Slip 攻击），已中止: {bad[:5]}")
                zf.extractall(dest_dir)
                extracted = len(zf.namelist())
        else:
            allowed_types = (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE, tarfile.LNKTYPE)
            with tarfile.open(archive_path) as tf:
                members = tf.getmembers()
                bad = [m.name for m in members if not _member_ok(m.name) or m.type not in allowed_types]
                if bad:
                    return _xml_response("error", f"发现非法成员（路径穿越或特殊类型），已中止: {bad[:5]}")
                try:
                    tf.extractall(dest_dir, filter="data")
                except TypeError:
                    tf.extractall(dest_dir)
                extracted = len(members)

        return _xml_response("done", f"解压完成: {extracted} 个条目 → {dest_dir}")
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="系统", name_cn="结束进程", risk_level="high")
def kill_process(pid: int, signal_num: int = 15) -> str:
    """
    向指定进程发送信号结束它（默认 SIGTERM 优雅终止，9 为强制杀死）。高危操作需用户确认。

    Args:
        pid: 进程 ID
        signal_num: 信号编号，15=SIGTERM（默认），9=SIGKILL 强制，2=SIGINT
    """
    import signal as _signal
    try:
        pid = int(pid)
        sig = int(signal_num)
        if sig not in (_signal.SIGTERM, _signal.SIGKILL, _signal.SIGINT):
            return _xml_response("error", "仅支持信号 15(SIGTERM)/9(SIGKILL)/2(SIGINT)")
        if pid <= 1:
            return _xml_response("error", "拒绝操作 PID<=1（init 进程）")
        if pid == os.getpid():
            return _xml_response("error", "拒绝结束自身进程")

        name = ""
        try:
            with open(f"/proc/{pid}/cmdline", "r") as f:
                name = f.read().replace("\x00", " ").strip()[:120]
        except OSError:
            logger.warning("异常被静默吞掉，已记录", exc_info=True)

        os.kill(pid, sig)
        time.sleep(0.3)
        alive = os.path.exists(f"/proc/{pid}")
        status = "已退出" if not alive else "仍在运行（可尝试 signal_num=9 强制）"
        return _xml_response("done", f"信号 {sig} 已发送给 PID {pid} [{name or '未知进程'}]，当前状态: {status}")
    except ProcessLookupError:
        return _xml_response("error", f"进程 {pid} 不存在")
    except PermissionError:
        return _xml_response("error", f"无权限操作进程 {pid}")
    except (TypeError, ValueError):
        return _xml_response("error", "pid/signal_num 必须是整数")
    except Exception as e:
        return _xml_response("error", str(e))


_SERVICE_ACTIONS = {"start", "stop", "restart", "reload", "enable", "disable", "status"}
_DOCKER_ACTIONS = {"start", "stop", "restart", "pause", "unpause", "rm"}


@register_tool(category="系统", name_cn="管理服务", risk_level="high")
def manage_service(action: str, service_name: str) -> str:
    """
    管理 systemd 服务：start / stop / restart / reload / enable / disable / status。高危操作需用户确认。

    Args:
        action: 动作 start|stop|restart|reload|enable|disable|status
        service_name: 服务名（如 nginx、mysql）
    """
    action = str(action).strip().lower()
    if action not in _SERVICE_ACTIONS:
        return _xml_response("error", f"不支持的动作: {action}（可选: {', '.join(sorted(_SERVICE_ACTIONS))}）")
    name = str(service_name).strip()
    if not re.fullmatch(r"[A-Za-z0-9@._\-]+", name):
        return _xml_response("error", f"服务名包含非法字符: {service_name}")

    success, output = _run_shell_cmd(["systemctl", action, name], timeout=60)
    summary = "\n".join(output.splitlines()[:30])
    icon = "✅" if success else "❌"
    return _xml_response("done" if success else "error", f"{icon} systemctl {action} {name}\n{summary}")


@register_tool(category="系统", name_cn="管理Docker容器", risk_level="high")
def docker_manage(action: str, container_id: str) -> str:
    """
    管理 Docker 容器生命周期：start / stop / restart / pause / unpause / rm（删除前校验必须已停止）。高危操作需用户确认。

    Args:
        action: 动作 start|stop|restart|pause|unpause|rm
        container_id: 容器 ID 或名称
    """
    action = str(action).strip().lower()
    if action not in _DOCKER_ACTIONS:
        return _xml_response("error", f"不支持的动作: {action}（可选: {', '.join(sorted(_DOCKER_ACTIONS))}）")
    cid = str(container_id).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+", cid):
        return _xml_response("error", f"容器标识包含非法字符: {container_id}")

    if action == "rm":
        ok, state = _run_shell_cmd(["docker", "inspect", "-f", "{{.State.Running}}", cid], timeout=20)
        if ok and state.strip() == "true":
            return _xml_response("error", f"容器 {cid} 正在运行，请先 stop 再删除")

    success, output = _run_shell_cmd(["docker", action, cid], timeout=90)
    icon = "✅" if success else "❌"
    return _xml_response("done" if success else "error", f"{icon} docker {action} {cid}\n{output[:2000]}")


@register_tool(category="系统", name_cn="计划任务管理", risk_level="high")
def cron_manage(action: str, job_line: str = "", comment: str = "") -> str:
    """
    管理当前用户 crontab 计划任务：
    - list: 列出全部任务
    - add: 新增任务行（job_line 如 "0 3 * * * /usr/bin/backup.sh"，可选 comment 标记注释便于后续按标记删除）
    - remove: 按 comment 标记（连同其注释行）或精确整行 job_line 删除
    高危操作需用户确认。

    Args:
        action: 动作 list|add|remove
        job_line: cron 任务完整行（add/remove 时使用）
        comment: 任务标记注释（add/remove 时使用）
    """
    import subprocess as _sp
    action = str(action).strip().lower()
    if action not in ("list", "add", "remove"):
        return _xml_response("error", "action 仅支持 list|add|remove")

    success, current = _run_shell_cmd(["crontab", "-l"], timeout=20)
    current_lines = current.splitlines() if success else []

    def _write_crontab(payload: str):
        proc = _sp.run(["crontab", "-"], input=payload.encode(), capture_output=True, timeout=20, check=False)
        return proc.returncode == 0, proc.stderr.decode(errors="ignore")

    if action == "list":
        body = "\n".join(current_lines) if any(l.strip() for l in current_lines) else "（当前无计划任务）"
        return _xml_response("done", body)

    marker = f"# agent-cron: {comment}" if comment else ""

    if action == "add":
        job_line = job_line.strip()
        if not job_line:
            return _xml_response("error", "job_line 不能为空")
        if any(ch in job_line for ch in ("\n", "\r")):
            return _xml_response("error", "job_line 不允许包含换行符")
        if any(ln.strip() == job_line for ln in current_lines):
            return _xml_response("error", "该任务行已存在")
        new_lines = current_lines[:]
        if marker:
            new_lines.append(marker)
        new_lines.append(job_line)
        ok, err = _write_crontab("\n".join(new_lines) + "\n")
        if not ok:
            return _xml_response("error", f"写入 crontab 失败: {err}")
        added = (marker + "\n" if marker else "") + job_line
        return _xml_response("done", f"已新增计划任务:\n{added}")

    # remove
    if comment and marker:
        new_lines = []
        skip_next = False
        for ln in current_lines:
            if skip_next:
                skip_next = False
                continue
            if ln.strip() == marker:
                skip_next = True  # 连带删除其下一行任务
                continue
            new_lines.append(ln)
    elif job_line:
        target = job_line.strip()
        new_lines = [ln for ln in current_lines if ln.strip() != target]
    else:
        return _xml_response("error", "remove 需要 comment 或 job_line 参数")

    if len(new_lines) == len(current_lines):
        return _xml_response("error", "未找到匹配的计划任务")

    payload = "\n".join(new_lines) + ("\n" if any(l.strip() for l in new_lines) else "")
    ok, err = _write_crontab(payload)
    if not ok:
        return _xml_response("error", f"写入 crontab 失败: {err}")
    remaining = len([l for l in new_lines if l.strip()])
    return _xml_response("done", f"已删除计划任务（剩余 {remaining} 行有效内容）")


@register_tool(category="系统", name_cn="安全清理临时文件", risk_level="medium")
def cleanup_temp(days: int = 7, target: str = "/tmp") -> str:
    """
    清理指定目录内超过 N 天未访问的普通文件（跳过子目录递归仅一层保护、跳过正在使用的 .pid/.sock 文件）。用于磁盘清理。

    Args:
        days: 超过多少天未访问即清理，默认 7
        target: 清理目录，默认 /tmp
    """
    try:
        real = os.path.realpath(target)
        if real in ("/", "/etc", "/usr", "/var", "/home", "/root", "/boot", PROJECT_ROOT):
            return _xml_response("error", f"拒绝清理系统关键目录: {target}")
        if not os.path.isdir(real):
            return _xml_response("error", f"目录不存在: {target}")

        cutoff = time.time() - max(1, int(days)) * 86400
        removed = freed = skipped = 0
        for entry in os.scandir(real):
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = entry.stat(follow_symlinks=False)
                if entry.name.endswith((".pid", ".sock", ".lock")):
                    skipped += 1
                    continue
                if st.st_atime < cutoff and st.st_mtime < cutoff:
                    os.remove(entry.path)
                    freed += st.st_size
                    removed += 1
            except OSError:

                logger.warning("循环处理时跳过异常", exc_info=True)
                continue
        return _xml_response("done", f"清理完成: 删除 {removed} 个文件，释放 {freed / 1024 / 1024:.1f}MB，跳过使用中文件 {skipped} 个")
    except Exception as e:
        return _xml_response("error", str(e))


@register_tool(category="系统", name_cn="授权写入目录", risk_level="high", timeout=15)
def GrantWriteAccess(path: str) -> str:
    """
    将指定目录追加到写白名单，使所有文件写入/编辑工具可以操作该目录及其子目录。
    用于自我迭代（修改自身代码）或运维需要操作系统路径。高危操作需 Boss 确认。

    Args:
        path: 要授权的目录绝对路径
    """
    from .edit import _add_extra_path, _is_path_allowed, _load_extra_paths
    rp = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
    if not os.path.isdir(rp):
        return _xml_response("error", f"目录不存在: {rp}")
    if _is_path_allowed(rp):
        return _xml_response("done", f"该目录已在白名单内: {rp}")
    if _add_extra_path(rp):
        return _xml_response("done",
            f"已授权写入: {rp}\\n当前全部白名单: {[_load_extra_paths()]}")
    return _xml_response("error", "授权失败")


@register_tool(category="系统", name_cn="查看与撤销写入授权", risk_level="low", timeout=15)
def ManageWritePaths(action: str = "list", path: str = "") -> str:
    """
    查看或撤销额外的写入授权目录。

    Args:
        action: list(查看) | revoke(撤销)
        path: revoke 时要撤销的目录
    """
    from .edit import _load_extra_paths, _remove_extra_path
    paths = _load_extra_paths()
    if action == "list":
        if not paths:
            return _xml_response("done", "无额外授权目录（默认：项目目录 + 用户主目录 + /tmp）")
        return _xml_response("done", "额外授权目录:\\n" + "\\n".join(paths))
    if action == "revoke":
        if not path:
            return _xml_response("error", "revoke 需要 path 参数")
        ok = _remove_extra_path(path)
        return _xml_response("done" if ok else "error",
                             f"{'已撤销' if ok else '未找到'}: {path}")
    return _xml_response("error", "action 仅支持 list|revoke")
