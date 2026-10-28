import glob
import os
import re
import shutil
from datetime import datetime

from . import PROJECT_ROOT, register_tool

# Import shared helper
from .base import _xml_response


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
        from .edit import _is_path_allowed, _is_protected_global_file
        if _is_protected_global_file(file_path):
            return _xml_response("error", f"Access denied: 全局敏感配置不允许直接读写，请使用专用 API ({file_path})")
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


