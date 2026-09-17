import glob
import os
import re
import shutil
from datetime import datetime

from . import PROJECT_ROOT, register_tool

# Import shared helper
from .base import _xml_response


@register_tool(category="系统", name_cn="获取当前时间", risk_level="low")
def get_current_time() -> str:
    """
    获取当前服务器时间（精确到秒）。
    返回格式化的日期时间字符串。
    """
    now = datetime.now().astimezone()
    return _xml_response("done", f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")

import subprocess
import sys;

# Use local public.py compatibility layer (standalone mode)
_base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _base_dir not in sys.path:
    sys.path.insert(0, _base_dir)
import logging

import public

logger = logging.getLogger(__name__)

# --- Tools ---

@register_tool(category="Agent", name_cn="Glob查找", risk_level="low")
def Glob(pattern: str, path: str | None = None) -> str:
    """
    Fast file pattern matching tool that works with any codebase size.
    - Supports glob patterns like "**/*.js" or "src/**/*.ts"
    - Returns matching file paths sorted by modification time (newest first)
    - Use this tool when you need to find files by name patterns
    
    Glob Pattern Syntax:
    - '*' matches any sequence of non-separator characters
    - '**' matches any sequence of characters, including separators
    - '?' matches any single non-separator character
    - '[...]' matches any character in the brackets
    
    Limitations:
    - Results are limited to 100 files (newest first)
    - Does not search file contents (use Grep tool for that)
    - Hidden files (starting with '.') are skipped
    
    Args:
        pattern: The glob pattern to match files against
        path: The directory to search in. If not specified, the current working directory will be used.
    """
    if not path:
        path = PROJECT_ROOT
    
    try:
        if not os.path.exists(path):
             return _xml_response("error", f"Path not found: {path}")

        search_path = os.path.join(path, pattern)
        files = glob.glob(search_path, recursive=True)
        
        # Filter only files and sort by mtime (descending)
        file_stats = []
        for f in files:
            if os.path.isfile(f):
                try:
                    mtime = os.path.getmtime(f)
                    file_stats.append((f, mtime))
                except Exception:
                    logger.warning("未处理的异常", exc_info=True)
        file_stats.sort(key=lambda x: x[1], reverse=True)
        
        limit = 100
        truncated = len(file_stats) > limit
        if truncated:
            file_stats = file_stats[:limit]
            
        output = [f[0] for f in file_stats]
        
        if not output:
            return _xml_response("done", "No files found")
            
        result = "\n".join(output)
        return _xml_response("done", result, metadata={
            "number_of_files": str(len(file_stats)),
            "truncated": str(truncated)
        })
    except Exception as e:
        return _xml_response("error", str(e))

@register_tool(category="Agent", name_cn="Grep搜索", risk_level="low")
def Grep(pattern: str, include: str | None = None, path: str | None = None) -> str:
    r"""
    Fast content search tool that finds files containing specific text or patterns.
    - Searches file contents using regular expressions
    - Supports full regex syntax (eg. "log.*Error", "function\s+\w+", etc.)
    - Filter files by pattern with the include parameter (eg. "*.js", "*.{ts,tsx}")
    - Returns file paths and line numbers with at least one match sorted by modification time
    
    Regex Pattern Syntax:
    - 'function' searches for the literal text "function"
    - 'log\..*Error' finds text starting with "log." and ending with "Error"
    - 'import\s+.*\s+from' finds import statements in JavaScript/TypeScript
    
    Limitations:
    - Results are limited to 100 files (newest first)
    - Performance depends on the number of files being searched
    - Very large binary files may be skipped
    - Hidden files (starting with '.') are skipped
    
    Args:
        pattern: The regex pattern to search for in file contents
        path: The directory to search in. Defaults to the current working directory.
        include: File pattern to include in the search (e.g. "*.js", "*.{ts,tsx}")
    """
    if not path:
        path = PROJECT_ROOT
        
    try:
        import glob as glob_module
        
        # 1. Find files
        files_to_search = []
        if os.path.isfile(path):
            files_to_search = [path]
        else:
            search_glob = include if include else "**/*"
            candidates = glob_module.glob(os.path.join(path, search_glob), recursive=True)
            files_to_search = [f for f in candidates if os.path.isfile(f)]

        regex = re.compile(pattern)
        matches = []
        MAX_LINE_LENGTH = 2000
        
        SENSITIVE_FILES = {"/etc/shadow", "/etc/gshadow", "/etc/sudoers"}
        for file_path in files_to_search:
            try:
                # 敏感凭证文件跳过（与 Read 工具黑名单一致）
                rp = os.path.realpath(os.path.abspath(file_path))
                if any(rp == sp or rp.startswith(sp + os.sep) for sp in SENSITIVE_FILES):
                    continue
                if "/.ssh/id_" in rp or rp.endswith(("_rsa", "_ed25519", "_ecdsa")):
                    continue
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    try:
                         mtime = os.path.getmtime(file_path)
                    except Exception:
                         mtime = 0
                         
                    for i, line in enumerate(lines):
                        if regex.search(line):
                            matches.append({
                                "path": file_path,
                                "lineNum": i + 1,
                                "lineText": line.rstrip(),
                                "mtime": mtime
                            })
            except Exception:
                logger.warning("循环处理时跳过异常", exc_info=True)
                continue
        # Sort by mtime desc
        matches.sort(key=lambda x: x["mtime"], reverse=True)
        
        limit = 100
        truncated = len(matches) > limit
        final_matches = matches[:limit] if truncated else matches
        
        if not final_matches:
            return _xml_response("done", "No matches found")
            
        output_lines = []
        
        current_file = ""
        for match in final_matches:
            if current_file != match["path"]:
                if current_file != "":
                    output_lines.append("")
                current_file = match["path"]
                output_lines.append(f"{match['path']}:")
            
            line_text = match["lineText"]
            if len(line_text) > MAX_LINE_LENGTH:
                line_text = line_text[:MAX_LINE_LENGTH] + "..."
            output_lines.append(f"  Line {match['lineNum']}: {line_text}")
            
        return _xml_response("done", "\n".join(output_lines), metadata={
            "number_of_matches": str(len(matches)),
            "truncated": str(truncated)
        })

    except Exception as e:
        return _xml_response("error", str(e))

@register_tool(category="Agent", name_cn="读取文件", risk_level="medium")
def Read(file_path: str, offset: int = 1, limit: int = 2000) -> str:
    """
    Read a file or directory from the local filesystem. If the path does not exist, an error is returned.
    
    Usage:
    - The filePath parameter should be an absolute path.
    - By default, this tool returns up to 2000 lines from the start of the file.
    - The offset parameter is the line number to start from (1-indexed).
    - To read later sections, call this tool again with a larger offset.
    - Use the grep tool to find specific content in large files or files with long lines.
    - If you are unsure of the correct file path, use the glob tool to look up filenames by glob pattern.
    - Contents are returned with each line prefixed by its line number as `<line>: <content>`.
    - Any line longer than 2000 characters is truncated.
    - Call this tool in parallel when you know there are multiple files you want to read.
    - Avoid tiny repeated slices (30 line chunks). If you need more context, read a larger window.
    
    Features:
    - Displays file contents with line numbers for easy reference
    - Can read from any position in a file using the offset parameter
    - Handles large files by limiting the number of lines read
    - Automatically truncates very long lines for better display
    
    Limitations:
    - Maximum file size is 250KB
    - Default reading limit is 2000 lines
    - Lines longer than 2000 characters are truncated
    - Cannot display binary files or images
    
    Args:
        file_path: The absolute path to the file to read.
        offset: The line number to start reading from (must be at least 1). Only provide if the file is too large to read at once.
        limit: The number of lines to read (must be at least 1, cannot be negative). Only provide if the file is too large to read at once.
    """
    try:
        # 敏感凭证文件黑名单：即使进程有权限也禁止读取
        real = os.path.realpath(os.path.abspath(file_path))
        SENSITIVE_PATHS = {
            "/etc/shadow", "/etc/sudoers", "/etc/gshadow",
            "/root/.ssh", "/etc/ssh",
        }
        for sp in SENSITIVE_PATHS:
            if real == sp or real.startswith(sp + os.sep):
                return _xml_response("error", f"Access denied: 敏感文件不允许读取 ({file_path})")
        if real.endswith("_rsa") or real.endswith("_ed25519") or real.endswith("_ecdsa") or "/.ssh/id_" in real:
            return _xml_response("error", f"Access denied: 私钥文件不允许读取 ({file_path})")

        if not os.path.exists(file_path):
            return _xml_response("error", f"File not found: {file_path}")
            
        if os.path.isdir(file_path):
            # Directory listing logic
            entries = os.listdir(file_path)
            entries.sort()
            
            start = offset - 1
            sliced = entries[start : start + limit]
            truncated = (start + len(sliced)) < len(entries)
            
            entry_lines = []
            for entry in sliced:
                full_p = os.path.join(file_path, entry)
                if os.path.isdir(full_p):
                    entry_lines.append(entry + "/")
                else:
                    entry_lines.append(entry)
            
            output = f"<path>{file_path}</path>\n<type>directory</type>\n<entries>\n"
            output += "\n".join(entry_lines)
            output += f"\n({len(sliced)} of {len(entries)} entries)"
            output += "\n</entries>"
            return _xml_response("done", output, metadata={
                "path": file_path,
                "type": "directory",
                "total_entries": str(len(entries))
            })

        # Check binary (simple check)
        BINARY_EXTENSIONS = {
            '.zip', '.tar', '.gz', '.exe', '.dll', '.so', '.class', '.jar', '.war', '.7z',
            '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.pdf', '.doc', '.docx', '.xls', '.xlsx'
        }
        ext = os.path.splitext(file_path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return _xml_response("done", f"<path>{file_path}</path>\n<type>binary</type>\n<content>Binary file detected (extension {ext}). Cannot read as text.</content>")

        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
            
        total_lines = len(lines)
        start_index = max(0, offset - 1)
        end_index = min(total_lines, start_index + limit)
        
        selected_lines = lines[start_index:end_index]
        
        content_lines = []
        for i, line in enumerate(selected_lines):
            content_lines.append(f"{start_index + i + 1}: {line.rstrip()}")
            
        output = f"<path>{file_path}</path>\n<type>file</type>\n<content>\n"
        output += "\n".join(content_lines)
        output += "\n</content>"
            
        return _xml_response("done", output, metadata={
            "path": file_path,
            "type": "file",
            "total_lines": str(total_lines),
            "showing_lines": f"{offset}-{end_index}"
        })
    except Exception as e:
        return _xml_response("error", str(e))

@register_tool(category="Agent", name_cn="写入文件", risk_level="high")
def Write(file_path: str, content: str) -> str:
    """
    Writes a file to the local filesystem.
    
    Usage:
    - This tool will overwrite the existing file if there is one at the provided path.
    - If this is an existing file, you MUST use the Read tool first to read the file's contents. This tool will fail if you did not read the file first.
    - ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
    - NEVER proactively create documentation files (*.md) or README files. Only create documentation files if explicitly requested by the User.
    - Only use emojis if the user explicitly requests it. Avoid writing emojis to files unless asked.
    
    Args:
        content: The content to write to the file
        file_path: The absolute path to the file to write (must be absolute, not relative)
    """
    try:
        from .edit import _is_path_allowed
        if not _is_path_allowed(file_path):
            return _xml_response("error", f"Path not allowed: {file_path}")
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return _xml_response("done", f"Wrote file successfully at: {file_path}")
    except Exception as e:
        return _xml_response("error", str(e))



def _run_shell_cmd(command: list, timeout: int = 300) -> tuple:
    """
    Common function to execute shell commands.
    Returns (success: bool, output: str)
    """
    try:
        # Use shell=False for security when passing a list
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        
        output = result.stdout.strip()
        if not output:
            output = result.stderr.strip()
        
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Error: Command timed out after {timeout} seconds."
    except FileNotFoundError:
        return False, f"Error: Command not found: {command[0]}"
    except Exception as e:
        return False, f"Error executing command: {e!s}"


@register_tool(category="系统", name_cn="获取服务状态", risk_level="low")
def get_service_status(service_name: str) -> str:
    """
    获取系统服务的状态 (Linux)。

    Args:
        service_name: 服务名称 (例如：'nginx', 'docker', 'mysql')。
    """
    success, output = _run_shell_cmd(["systemctl", "status", service_name])
    
    # Summary of first 20 lines
    summary = "\n".join(output.splitlines()[:20])
    
    if success:
        result = f"✅ Service '{service_name}' is active/running (or exited successfully).\n{summary}"
        return _xml_response("done", result)
    else:
        if "not found" in output.lower():
            return _xml_response("error", f"❌ Service '{service_name}' not found.")
        result = f"⚠️ Service '{service_name}' status check failed or inactive.\n{summary}"
        return _xml_response("done", result)


@register_tool(category="系统", name_cn="重启服务", risk_level="high")
def restart_service(service_name: str) -> str:
    """
    尝试重启指定的系统服务 (通常需要 root 权限)。

    Args:
        service_name: 服务名称。
    """
    success, output = _run_shell_cmd(["systemctl", "restart", service_name])
    if success:
        return _xml_response("done", f"✅ Service '{service_name}' restarted successfully.")
    else:
        return _xml_response("error", f"❌ Failed to restart '{service_name}':\n{output}")


@register_tool(category="系统", name_cn="获取系统资源", risk_level="low")
def get_system_resources() -> str:
    """
    获取当前系统 CPU 负载、内存使用和磁盘空间信息。

    returns:
        OS、 Load Average、 Memory、 Disk 信息字符串
    """
    try:
        # Load Average
        try:
            load1, load5, load15 = os.getloadavg()
            load_info = f"Load Avg: {load1:.2f}, {load5:.2f}, {load15:.2f}"
        except OSError:
            load_info = "Load Avg: N/A (Windows?)"
        
        # Memory
        mem_info = "Mem: Unknown"
        if os.path.exists('/proc/meminfo'):
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
                total = 0
                available = 0
                for line in lines:
                    if 'MemTotal' in line:
                        total = int(line.split()[1]) // 1024  # MB
                    if 'MemAvailable' in line:
                        available = int(line.split()[1]) // 1024  # MB
                used = total - available
                percent = (used / total * 100) if total > 0 else 0
                mem_info = f"Mem: {used}MB/{total}MB ({percent:.1f}%)"
        
        # Disk
        disk = shutil.disk_usage("/")
        total_gb = disk.total // (1024 ** 3)
        used_gb = disk.used // (1024 ** 3)
        disk_percent = (disk.used / disk.total * 100)
        disk_info = f"Disk (/): {used_gb}GB/{total_gb}GB ({disk_percent:.1f}%)"
        os_info = public.get_os_version()
        
        result = f"{load_info}\n{mem_info}\n{disk_info}\nOS: {os_info}"
        return _xml_response("done", result)
    except Exception as e:
        return _xml_response("error", f"Error getting resources: {e!s}")

@register_tool(category="网络", name_cn="域名检测", risk_level="low")
def check_domain(domain: str) -> str:
    """
    检测域名解析 (使用 dig 或 nslookup)。
    """
    # Try dig first
    success, output = _run_shell_cmd(["dig", "+short", domain])
    if success and output:
        return _xml_response("done", f"Dig result for {domain}:\n{output}")
    
    # Fallback to nslookup
    success, output = _run_shell_cmd(["nslookup", domain])
    if success:
        return _xml_response("done", f"Nslookup result for {domain}:\n{output}")
    
    return _xml_response("error", f"Error resolving domain {domain}.")


@register_tool(category="网络", name_cn="Ping检测", risk_level="low")
def ping_target(target: str) -> str:
    """
    Ping 目标主机 (发送 4 个包)。
    """
    success, output = _run_shell_cmd(["ping", "-c", "4", target])
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Ping failed:\n{output}")


@register_tool(category="网络", name_cn="Curl请求", risk_level="low")
def curl_url(url: str) -> str:
    """
    使用 curl 获取网页内容。
    """
    # 仅允许 http/https，禁止 file:// 等协议读取本地文件或访问内部服务
    if not str(url).lower().startswith(("http://", "https://")):
        return _xml_response("error", f"仅支持 http:// 或 https:// 协议: {url}")
    success, output = _run_shell_cmd(["curl", "-L", "-s", "--max-time", "10", url])
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Curl failed:\n{output}")


@register_tool(category="网站", name_cn="获取网站列表(不包含Docker站点)", risk_level="medium")
def get_sites() -> str:
    """
    获取在 AI 面板部署的全部网站列表（域名）;

    returns :
        [
            {
                "id": 1,
                "name": "www.example.com" or "ip_port", #网站域名或绑定的IP端口
                "project_type": "PHP|html" #网站类型

            }
        ]
    """
    import json
    # Standalone mode: read from panel sites config if available
    sites = []
    sites_dir = "/www/server/panel/vhost"
    if os.path.exists(sites_dir):
        nginx_dir = os.path.join(sites_dir, "nginx")
        if os.path.exists(nginx_dir):
            for f in os.listdir(nginx_dir):
                if f.endswith('.conf'):
                    name = f.replace('.conf', '')
                    # Remove type prefix
                    for prefix in ['php_', 'proxy_', 'phpmod_', 'wp2_', 'html_']:
                        if name.startswith(prefix):
                            name = name[len(prefix):]
                            break
                    sites.append({"id": name, "name": name, "project_type": "PHP"})
    return _xml_response("done", json.dumps(sites, ensure_ascii=False, indent=2))


@register_tool(category="网站", name_cn="获取网站配置", risk_level="medium")
def get_sites_conf(site_name: str) -> str:
    """
    获取网站的nginx或apache配置文件内容;

    Args:
        site_name: 网站域名或绑定的IP端口;

    returns :
        Nginx配置文件内容字符串;
    """
    
    site_data = public.M('sites').field('name,project_type').where("name=?", site_name).select()
    if not site_data:
        return _xml_response("error", f"Error: site '{site_name}' not found in panel.")
    
    res = site_data[0]['project_type'].lower()
    if res == 'php' or res == 'proxy' or res == 'phpmod' or res == 'wp2':
        res = ''
    else:
        res = res + '_'
    
    full_path = f"/www/server/panel/vhost/nginx/{res}{site_name}.conf"
    if not os.path.exists(full_path):
        full_path = f"/www/server/panel/vhost/apache/{res}{site_name}.conf"
        if not os.path.exists(full_path):
            return _xml_response("error", f"Error: configuration for site '{site_name}' not found.")
    
    with open(full_path, 'r') as f:
        config_content = f.read()
    return _xml_response("done", config_content)


@register_tool(category="网站", name_cn="获取网站访问日志", risk_level="medium")
def get_sites_logs(site_name: str) -> str:
    """
    获取网站的访问日志(最大1000行);

    Args:
        site_name: 网站域名或绑定的IP端口;

    returns :
        访问日志内容;
    """
    # Standalone mode: try to find log file directly
    log_paths = [
        f"/www/wwwlogs/{site_name}.log",
        f"/www/wwwlogs/{site_name}.access.log",
    ]
    for log_path in log_paths:
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()[-1000:]
                return _xml_response("done", "".join(lines))
            except Exception as e:
                return _xml_response("error", f"Error reading log: {e!s}")
    return _xml_response("error", f"Log file for '{site_name}' not found in /www/wwwlogs/")


@register_tool(category="网站", name_cn="获取全部网站流量分析数据", risk_level="medium")
def get_site_analysis() -> str:
    """
    获取全部网站的流量分析数据(最近7天);
    """
    return _xml_response("done", "流量分析功能需要 AI 面板环境支持，独立模式下暂不可用。")


@register_tool(category="数据库", name_cn="获取Mysql数据库列表", risk_level="medium")
def get_mysql_list() -> str:
    """
    获取面板中所有数据库列表（已对密码脱敏）

    returns :
        [
            {
                "name": "数据库名称",
                "username": "数据库用户名",
                "accept": "允许访问的IP",
                "type": "数据库类型"
            }
        ]
    """
    import json
    # Standalone mode: return empty list (no panel database access)
    return _xml_response("done", json.dumps([], ensure_ascii=False, indent=2))


@register_tool(category="系统", name_cn="获取资源占用TOP10进程", risk_level="low")
def get_top_processes() -> str:
    """
    获取系统中 CPU 和 内存 占用率最高的 TOP 10 进程。
    """
    output_parts = []
    
    # 1. CPU Top 10
    success_cpu, output_cpu = _run_shell_cmd(["ps", "-eo", "pid,user,%cpu,%mem,command", "--sort=-%cpu"])
    if success_cpu:
        lines = output_cpu.strip().splitlines()
        header = lines[0] if lines else ""
        top10 = lines[1:11]
        output_parts.append("--- CPU 占用 TOP 10 ---")
        output_parts.append(header)
        output_parts.extend(top10)
    else:
        output_parts.append(f"获取 CPU TOP 10 失败: {output_cpu}")
    
    output_parts.append("")  # 空行分隔
    
    # 2. Memory Top 10
    success_mem, output_mem = _run_shell_cmd(["ps", "-eo", "pid,user,%cpu,%mem,command", "--sort=-%mem"])
    if success_mem:
        lines = output_mem.strip().splitlines()
        header = lines[0] if lines else ""
        top10 = lines[1:11]
        output_parts.append("--- 内存 占用 TOP 10 ---")
        output_parts.append(header)
        output_parts.extend(top10)
    else:
        output_parts.append(f"获取内存 TOP 10 失败: {output_mem}")
    
    return _xml_response("done", "\n".join(output_parts))


@register_tool(category="网络", name_cn="获取服务器IP", risk_level="medium")
def get_server_ip() -> str:
    """
    获取服务器的内网和公网 IP 地址。
    返回信息包含:
    1. 各个网卡的 IP 地址
    2. 外部 IP 地址
    """
    info = []
    
    # Internal IPs with Interface names
    # Try ip -o -4 addr show first (Linux)
    success, output = _run_shell_cmd(['ip', '-o', '-4', 'addr', 'show'])
    if success:
        info.append("--- Network Interfaces (Internal) ---")
        info.append(output)
    else:
        # Fallback to hostname -I
        s, o = _run_shell_cmd(['hostname', '-I'])
        if s:
            info.append(f"Internal IPs: {o}")
        else:
            info.append("Internal IPs: Unable to retrieve (not Linux?)")
    
    # External IP
    external_ip = "Unknown"
    # Try multiple services
    services = ["https://www.ai-assistant.cn/Api/getIpAddress"]
    for service in services:
        s, o = _run_shell_cmd(["curl", "-s", "--connect-timeout", "3", service])
        if s and o:
            external_ip = o
            break
    
    info.append("\n--- External IP ---")
    info.append(external_ip)
    
    return _xml_response("done", "\n".join(info))


# --- New Tools ---

@register_tool(category="系统", name_cn="获取Docker信息", risk_level="low")
def get_docker_info() -> str:
    """获取 Docker 系统级信息 (docker info)。"""
    success, output = _run_shell_cmd(["docker", "info"])
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Error getting docker info: {output}")


@register_tool(category="系统", name_cn="获取Docker容器", risk_level="medium")
def get_docker_containers(all: bool = True) -> str:
    """
    获取 Docker 容器列表。

    Args:
        all: 是否显示所有容器 (包括未运行的)，默认为 True。
    """
    cmd = ["docker", "ps"]
    if all:
        cmd.append("-a")
    
    success, output = _run_shell_cmd(cmd)
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Error listing containers: {output}")


@register_tool(category="系统", name_cn="获取Docker容器详情", risk_level="medium")
def get_docker_inspect(container_id_or_name: str) -> str:
    """
    获取 Docker 容器详情。

    Args:
        container_id_or_name: Docker 容器 ID 或名称。
    """
    cmd = ["docker", "inspect", container_id_or_name]
    
    success, output = _run_shell_cmd(cmd)
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Error inspecting container {container_id_or_name}: {output}")


@register_tool(category="系统", name_cn="获取Docker容器日志", risk_level="medium")
def get_docker_logs(container_id_or_name: str) -> str:
    """
    获取 Docker 容器日志。

    Args:
        container_id_or_name: Docker 容器 ID 或名称。
    """
    cmd = ["docker", "logs", container_id_or_name]
    
    success, output = _run_shell_cmd(cmd)
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Error getting logs for container {container_id_or_name}: {output}")


@register_tool(category="系统", name_cn="获取防火墙状态", risk_level="medium")
def get_firewall_status() -> str:
    """获取 iptables 防火墙规则。"""
    success, output = _run_shell_cmd(["iptables", "-L", "-n", "-v"])
    if success:
        return _xml_response("done", output)
    return _xml_response("error", f"Error getting firewall status (requires root?): {output}")