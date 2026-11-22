import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid

from . import PROJECT_ROOT, register_tool
from .base import (
    _xml_response,
    truncate_output,
    SAFE_READONLY_COMMANDS,
    get_banned_commands,
    get_bash_timeout_ms,
)

logger = logging.getLogger(__name__)

# 后台命令最长存活时间（秒）：超时仍未退出的整组进程会被强制回收，防止
# 前台超时转后台后变成无人看管的 CPU 黑洞。可用环境变量覆盖，0 表示关闭。
BG_MAX_RUNTIME_SEC = int(os.environ.get("AGENT_BG_MAX_RUNTIME_SEC", "1800"))


def _kill_process_tree(process, grace: float = 2.0):
    """按进程组杀进程（POSIX），确保 shell 的孙进程（如 python3 卡死脚本）一并回收。"""
    try:
        if os.name != "nt":
            pgid = os.getpgid(process.pid)
            try:
                os.killpg(pgid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        else:
            process.terminate()
    except Exception:
        logger.warning("杀进程组失败，回退 terminate", exc_info=True)
        try:
            process.terminate()
        except Exception:
            pass


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
                shell=False if os.name == 'nt' else True,
                start_new_session=(os.name != 'nt'),
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
        self._maybe_start_watchdog()

        return cmd_id, None

    def adopt(self, process, command: str, cwd: str) -> str:
        """接管一个已在运行的进程（前台超时转后台）。不重跑命令，避免副本叠加。"""
        cmd_id = str(uuid.uuid4())
        cmd_info = {
            "id": cmd_id,
            "process": process,
            "output": [],
            "status": "running",
            "start_time": time.time(),
            "cwd": cwd,
            "command": command,
            "adopted": True,
        }
        with self.lock:
            self.commands[cmd_id] = cmd_info
        t = threading.Thread(target=self._read_output, args=(cmd_id, process))
        t.daemon = True
        t.start()
        self._maybe_start_watchdog()
        return cmd_id

    def _maybe_start_watchdog(self):
        """惰性启动后台命令寿命巡检：跑太久的整组进程强制回收。"""
        if BG_MAX_RUNTIME_SEC <= 0:
            return
        if getattr(self, "_watchdog_started", False):
            return
        self._watchdog_started = True

        def _loop():
            while True:
                time.sleep(60)
                now = time.time()
                victims = []
                with self.lock:
                    for _cid, c in self.commands.items():
                        if c["status"] == "running" and now - c["start_time"] > BG_MAX_RUNTIME_SEC:
                            victims.append(c)
                for c in victims:
                    logger.warning(
                        "后台命令超过 %ss 未退出，强制回收进程组: pid=%s cmd=%s",
                        BG_MAX_RUNTIME_SEC, getattr(c["process"], "pid", "?"),
                        (c.get("command") or "")[:200],
                    )
                    _kill_process_tree(c["process"])
                    with self.lock:
                        c["status"] = "stopped"
                        c["returncode"] = -9

        threading.Thread(target=_loop, daemon=True, name="bg-cmd-watchdog").start()

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
                    _kill_process_tree(cmd["process"])
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
    timeout 单位毫秒，省略时取主配置 agent.bash_timeout（默认 30 秒，可在设置页调整）；
    长任务（pip install / 大范围 grep / git 历史）请显式调大 timeout。
    """
    def execute(self, command: str, language: str = "shell", blocking: bool = True,
                cwd: str | None = None, timeout: int | None = None, description: str | None = None,
                args: list | None = None) -> str:
        import time as _time
        start_time = _time.time()

        if not command:
            return _xml_response("error", "command 参数不能为空")

        # 超时兜底：未显式传 timeout（或模型传了 null）时，读主配置的默认值。
        # 优先级：调用参数 > config.json 的 agent.bash_timeout > 内置 30s。
        try:
            timeout = int(timeout) if timeout not in (None, "", 0) else get_bash_timeout_ms()
        except (TypeError, ValueError):
            timeout = get_bash_timeout_ms()
        if timeout <= 0:
            timeout = get_bash_timeout_ms()

        # 安全检查：禁止危险命令（单Agent/破甲模式 is_allow_high()=True 时放行）
        base_cmd = command.split()[0].lower() if command.split() else ""
        from . import is_allow_high
        if base_cmd in get_banned_commands() and not is_allow_high():
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
            timeout_sec = timeout / 1000.0
            try:
                shell_cmd = command
                if os.name == 'nt':
                    shell_cmd = ["powershell", "-Command", command]
                # 关键：独立进程组 + 自己控超时。超时后接管同一进程转后台，
                # 绝不重新执行一遍命令（旧实现会导致副本不断叠加）。
                proc = subprocess.Popen(
                    shell_cmd, cwd=cwd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    encoding='utf-8', errors='replace',
                    shell=False if os.name == 'nt' else True,
                    start_new_session=(os.name != 'nt'),
                )
                output, _ = proc.communicate(timeout=timeout_sec)
                output = truncate_output(output)
                if description:
                    output = f"Description: {description}\n\n{output}"
                elapsed = _time.time() - start_time
                return _xml_response("done", output, metadata={
                    "exit_code": str(proc.returncode),
                    "duration": f"{elapsed:.2f}s"
                })
            except subprocess.TimeoutExpired:
                # 前台超时 → 接管已在运行的进程转后台（不重跑）
                cmd_id = _CMD_MANAGER.adopt(proc, command, cwd)
                result = (f"<terminal_id>new</terminal_id>\n"
                          f"<terminal_cwd>{cwd}</terminal_cwd>\n"
                          f"命令已达到 {timeout / 1000.0:g} 秒前台等待上限，已转入后台继续运行"
                          f"（同一进程，未重复执行）。后台最长存活 {BG_MAX_RUNTIME_SEC}s，"
                          f"超时会被整组强制回收。\n"
                          f"<command_id>{cmd_id}</command_id>\n"
                          f"【重要】该命令已在后台执行，禁止重复执行同一条命令。\n"
                          f"请先继续处理其他工作，需要结果时再用 CheckCommandStatus 查一次；"
                          f"不要连续轮询，也不要立刻紧接着查询（刚转后台时必然还没结果）。\n")
                return _xml_response("running", result)
            except Exception as e:
                return _xml_response("error", str(e))
        else:
            cmd_id, err = _CMD_MANAGER.start_command(command, cwd)
            if err:
                return _xml_response("error", err)
            result = (f"<terminal_id>new</terminal_id>\n<terminal_cwd>{cwd}</terminal_cwd>\n"
                      f"命令已在后台启动。\n"
                      f"<command_id>{cmd_id}</command_id>\n"
                      f"【重要】禁止重复执行同一条命令；请先继续其他工作，"
                      f"需要结果时再用 CheckCommandStatus 查一次，不要连续轮询。\n")
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
            return _xml_response("error", f"执行超时（>{timeout / 1000.0:g} 秒）")
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


@register_tool(category="Agent", name_cn="检查命令状态", risk_level="low")
class CheckCommandStatus:
    """
    Check the status and output of a non-blocking command.

    Args:
        command_id: ID of the command to get status for.
        output_priority: Priority for displaying command output. 'bottom' (show newest lines) or 'top'.
    """
    def execute(self, command_id: str, output_priority: str = "bottom", **kwargs) -> str:
        # 兼容历史调用：部分桥接层会给命令工具统一注入 cwd 等上下文参数，
        # 本工具不需要它们，用 **kwargs 吸收，避免 "unexpected keyword argument"。
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
    def execute(self, command_id: str, **kwargs) -> str:
        # 兼容历史调用：部分桥接层会给命令工具统一注入 cwd 等上下文参数，
        # 本工具不需要它们，用 **kwargs 吸收，避免 "unexpected keyword argument"。
        if _CMD_MANAGER.stop_command(command_id):
            return _xml_response("done", f"Command {command_id} stopped.")
        else:
            return _xml_response("error", f"Failed to stop command {command_id} (not running or not found).")



