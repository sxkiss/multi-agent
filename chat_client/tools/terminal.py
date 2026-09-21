import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

from . import PROJECT_ROOT, register_tool
from .base import _xml_response, truncate_output, SAFE_READONLY_COMMANDS, BANNED_COMMANDS

logger = logging.getLogger(__name__)


# --- Command Manager for Non-blocking Commands ---
class CommandManager:
    def __init__(self):
        self.commands = {}
        self.lock = threading.Lock()

    def start_command(self, command: str, cwd: str) -> tuple:
        cmd_id = str(uuid.uuid4())
        
        shell_cmd = command
        if os.name == 'nt':
             shell_cmd = ["powershell", "-Command", command]
        
        try:
            process = subprocess.Popen(
                shell_cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding='utf-8',
                errors='replace',
                shell=False if os.name == 'nt' else True 
            )
        except Exception as e:
            return None, str(e)

        cmd_info = {
            "id": cmd_id,
            "process": process,
            "output": [], # List of lines
            "status": "running",
            "start_time": time.time(),
            "cwd": cwd,
            "command": command
        }
        
        with self.lock:
            self.commands[cmd_id] = cmd_info

        # Start thread to read output
        t = threading.Thread(target=self._read_output, args=(cmd_id, process))
        t.daemon = True
        t.start()
        
        return cmd_id, None

    def _read_output(self, cmd_id, process):
        try:
            for line in iter(process.stdout.readline, ''):
                with self.lock:
                    if cmd_id in self.commands:
                        self.commands[cmd_id]["output"].append(line)
        except Exception:
            logger.warning("未处理的异常", exc_info=True)
        finally:
            try:
                process.stdout.close()
            except Exception:
                logger.warning("未处理的异常", exc_info=True)
            return_code = process.wait()
            
            with self.lock:
                if cmd_id in self.commands:
                    self.commands[cmd_id]["status"] = "done"
                    self.commands[cmd_id]["returncode"] = return_code

    def get_status(self, cmd_id: str, priority: str = "bottom", limit: int = 1000):
        with self.lock:
            if cmd_id not in self.commands:
                return None
            
            cmd = self.commands[cmd_id]
            output_lines = cmd["output"]
            
            if priority == "bottom":
                lines = output_lines[-limit:]
            else:
                lines = output_lines[:limit]
                
            return {
                "status": cmd["status"],
                "returncode": cmd.get("returncode"),
                "output": "".join(lines),
                "cwd": cmd["cwd"],
                "command": cmd["command"]
            }

    def stop_command(self, cmd_id: str):
        with self.lock:
            if cmd_id not in self.commands:
                return False
            
            cmd = self.commands[cmd_id]
            if cmd["status"] == "running":
                try:
                    cmd["process"].terminate() 
                    cmd["status"] = "stopped"
                except Exception:
                    logger.warning("未处理的异常", exc_info=True)
                return True
            return False

_CMD_MANAGER = CommandManager()

# --- 代码执行安全黑名单（合并自 PythonExecute / NodeExecute）---
_PY_DANGEROUS_PATTERNS = [
    r"\bos\.system\s*\(",
    r"\bos\.popen\s*\(",
    r"\bos\.exec[lv]",
    r"\bos\.spawn",
    r"\bsubprocess\s*\.",
    r"\bpty\s*\.\s*spawn",
    r"\bcommands\s*\.\s*(?:getoutput|getstatusoutput)",
]
_JS_DANGEROUS_PATTERNS = [
    r"\bchild_process\b",
    r"\brequire\s*\(\s*['\"](?:child_process|node:child_process)['\"]\s*\)",
    r"\bprocess\s*\.\s*binding\s*\(",
]


def _contains_dangerous_code(code: str, patterns: list) -> str | None:
    for pat in patterns:
        if re.search(pat, code):
            return pat
    return None


@register_tool(category="Agent", name_cn="执行命令/代码", id="Bash", risk_level="high", timeout=3600)
class Bash:
    """
    执行 shell 命令或 Python/Node 代码片段（统一入口，原 Bash/Python/Node 工具已合并）。
    - language="shell"（默认）：在项目目录运行命令；blocking=False 时后台运行，用 CheckCommandStatus/StopCommand 管理。
    - language="python"/"node"：代码写入临时文件用对应解释器执行；禁止通过 os.system/subprocess/child_process 逃逸到系统 shell（安全黑名单拦截）。
    参数：command 必填（命令或代码）；blocking/cwd/timeout 仅 shell 生效；args 仅 python/node 生效。
    默认超时 15s；长任务（pip install / 大范围 grep / git 历史）请显式调大 timeout。
    """
    def execute(self, command: str, language: str = "shell", blocking: bool = True,
                cwd: str | None = None, timeout: int = 15000, description: str | None = None,
                args: list | None = None) -> str:
        import time as _time
        start_time = _time.time()

        if not command:
            return _xml_response("error", "command 参数不能为空")

        # 安全检查：禁止危险命令（单Agent/破甲模式 is_allow_high()=True 时放行）
        base_cmd = command.split()[0].lower() if command.split() else ""
        from . import is_allow_high
        if base_cmd in BANNED_COMMANDS and not is_allow_high():
            return _xml_response("error", f"命令 '{base_cmd}' 被安全策略禁止执行")

        lang = (language or "shell").lower()
        if lang in ("python", "py"):
            return self._run_script(command, "python3", _PY_DANGEROUS_PATTERNS, args, cwd, timeout)
        if lang in ("node", "js"):
            return self._run_script(command, "node", _JS_DANGEROUS_PATTERNS, args, cwd, timeout)

        # ---- shell 模式 ----
        if not cwd:
            cwd = PROJECT_ROOT
        if blocking:
            try:
                shell_cmd = command
                if os.name == 'nt':
                    shell_cmd = ["powershell", "-Command", command]
                timeout_sec = timeout / 1000.0
                result = subprocess.run(
                    shell_cmd, cwd=cwd, capture_output=True, text=True,
                    encoding='utf-8', errors='replace',
                    shell=False if os.name == 'nt' else True,
                    timeout=timeout_sec, check=False,
                )
                output = result.stdout
                if result.stderr:
                    output += f"\n{result.stderr}" if output else result.stderr
                output = truncate_output(output)
                if description:
                    output = f"Description: {description}\n\n{output}"
                elapsed = _time.time() - start_time
                return _xml_response("done", output, metadata={
                    "exit_code": str(result.returncode),
                    "duration": f"{elapsed:.2f}s"
                })
            except subprocess.TimeoutExpired:
                return _xml_response("error", f"Command timed out after {timeout} ms")
            except Exception as e:
                return _xml_response("error", str(e))
        else:
            cmd_id, err = _CMD_MANAGER.start_command(command, cwd)
            if err:
                return _xml_response("error", err)
            result = (f"<terminal_id>new</terminal_id>\n<terminal_cwd>{cwd}</terminal_cwd>\n"
                      f"Note: Command ID is provided for you to check command status later.\n"
                      f"<command_id>{cmd_id}</command_id>\n"
                      f"The command is running, you need to call check_command_status tool to get more logs.\n")
            return _xml_response("running", result)

    @staticmethod
    def _run_script(code: str, interpreter: str, danger_patterns: list, args: list | None,
                   cwd: str | None, timeout: int) -> str:
        pat = _contains_dangerous_code(code, danger_patterns)
        if pat:
            return _xml_response(
                "error",
                f"代码包含被禁止的 shell 调用特征（{pat}），已被安全策略拒绝执行。"
                "如需运行系统命令，请改用 language=shell。",
            )
        if not cwd:
            cwd = PROJECT_ROOT
        if not args:
            args = []
        timeout_sec = min(max(timeout / 1000.0, 1.0), 300.0)
        tmp_path = None
        try:
            suffix = ".py" if interpreter == "python3" else ".js"
            fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="exec_")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(code)
            start = time.time()
            result = subprocess.run(
                [interpreter, tmp_path] + list(args), cwd=cwd,
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=timeout_sec, check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            output = truncate_output(output)
            elapsed = time.time() - start
            return _xml_response(
                "done",
                output,
                metadata={
                    "exit_code": str(result.returncode),
                    "duration": f"{elapsed:.2f}s"
                }
            )
        except subprocess.TimeoutExpired:
            return _xml_response("error", f"执行超时（>{timeout} ms）")
        except FileNotFoundError:
            return _xml_response("error", f"未找到 {interpreter} 命令，请确认运行环境")
        except Exception as e:
            return _xml_response("error", f"执行失败: {e!s}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    logger.warning("异常被静默吞掉，已记录", exc_info=True)

# 向后兼容别名
RunCommand = Bash


class CheckCommandStatus:
    """
    Check the status and output of a non-blocking command.
    
    Args:
        command_id: ID of the command to get status for.
        output_priority: Priority for displaying command output. 'bottom' (show newest lines) or 'top'.
    """
    def execute(self, command_id: str, output_priority: str = "bottom") -> str:
        status_info = _CMD_MANAGER.get_status(command_id, output_priority)
        if not status_info:
            return _xml_response("error", "Command ID not found")
            
        logs = status_info["output"]
        status_str = status_info["status"]
        
        result = f"""<terminal_id>unknown</terminal_id>
<terminal_cwd>{status_info['cwd']}</terminal_cwd>
<command_id>{command_id}</command_id>
<command_status>{status_str.capitalize()}</command_status><command_run_logs>
```
{logs}
```
</command_run_logs>
"""
        return _xml_response("done", result)

@register_tool(category="Agent", name_cn="停止命令", risk_level="medium")
class StopCommand:
    """
    Terminate a running command.
    
    Args:
        command_id: The command id of the running command that you need to terminate.
    """
    def execute(self, command_id: str) -> str:
        if _CMD_MANAGER.stop_command(command_id):
            return _xml_response("done", f"Command {command_id} stopped.")
        else:
            return _xml_response("error", f"Failed to stop command {command_id} (not running or not found).")



