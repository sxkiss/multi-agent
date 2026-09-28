from dataclasses import dataclass
from typing import Optional
import inspect


@dataclass
class ToolResponse:
    """结构化工具响应"""
    status: str  # done, error, running
    content: str
    metadata: Optional[dict] = None
    is_error: bool = False
    tool_name: str = ""

    def to_xml(self) -> str:
        meta_xml = ""
        if self.metadata:
            meta_items = "\n".join(f"<{k}>{v}</{k}>" for k, v in self.metadata.items())
            meta_xml = f"{meta_items}\n"
        tool_name_xml = f"<tool_name>{self.tool_name}</tool_name>\n" if self.tool_name else ""
        return f"\n<tool>\n{tool_name_xml}<toolcall_status>{self.status}</toolcall_status>\n<toolcall_result>\n{meta_xml}{self.content}\n</toolcall_result>\n</tool>\n"


def _xml_response(status: str, result: str, metadata: dict = None, tool_name: str = "") -> str:
    """兼容旧接口，内部使用 ToolResponse

    tool_name: 工具名，注入 <tool_name> 标签。留空时自动从调用方函数名推导
    （栈回溯到工具模块），使 325 处既有调用无需逐一改造。
    """
    if not tool_name:
        tool_name = _infer_tool_name()
    resp = ToolResponse(status=status, content=result, metadata=metadata,
                        is_error=(status == "error"), tool_name=tool_name)
    return resp.to_xml()


def _infer_tool_name() -> str:
    """从调用栈推导工具名：优先返回工具类名（如 Bash/Read/Write），
    避免返回泛化方法名 execute/run。

    典型调用链：
        agent.py → registry._guarded_execute(func) → Bash.execute() → _xml_response()
    因此从当前帧往上找，落在 tools/ 子模块里的函数名是 execute/run 时，
    取它的 self 类名作为工具名；普通函数则直接用函数名。
    """
    try:
        frame = inspect.currentframe()
        for _ in range(8):
            if frame is None:
                break
            mod = frame.f_globals.get("__name__", "")
            if mod.startswith("chat_client.tools.") and "base" not in mod:
                func = frame.f_code.co_name
                if func in ("_xml_response", "_infer_tool_name"):
                    frame = frame.f_back
                    continue
                # execute/run 是类方法 → 取 self 的类名
                if func in ("execute", "run", "__call__"):
                    self_obj = frame.f_locals.get("self")
                    if self_obj is not None:
                        return type(self_obj).__name__
                return func
            frame = frame.f_back
    except Exception:
        pass
    return ""


def inject_tool_name(xml: str, tool_name: str) -> str:
    """幂等注入 <tool_name> 标签：已有则不重复注入，非 tool 结构原样返回。

    供 _guarded_execute 统一调用——所有工具的执行结果（含风险拦截/超时转后台
    等兜底路径）都带上工具名，便于模型区分多个并行工具的结果。
    """
    if not tool_name or not xml:
        return xml
    if "<tool_name>" in xml:
        return xml
    # 仅对 <tool> 结构注入；其他纯文本结果不加工
    if "<tool>" not in xml:
        return xml
    return xml.replace("<tool>", f"<tool>\n<tool_name>{tool_name}</tool_name>", 1)


# --- 输出截断工具（参考 opencode 保留前后各一半） ---
MAX_OUTPUT_LENGTH = 30000
SAFE_READONLY_COMMANDS = frozenset({
    "ls", "echo", "pwd", "date", "cal", "uptime", "whoami", "id", "groups",
    "env", "printenv", "which", "type", "whereis", "whatis", "uname", "hostname",
    "df", "du", "free", "top", "ps", "cat", "head", "tail", "less", "more",
    "file", "stat", "wc", "sort", "uniq", "tr", "cut", "awk", "sed",
    "git status", "git log", "git diff", "git show", "git branch", "git tag",
    "git remote", "git ls-files", "git ls-remote", "git rev-parse", "git config --get",
    "git config --list", "git describe", "git blame", "git grep", "git shortlog",
})

BANNED_COMMANDS = frozenset({
    "alias", "curl", "curlie", "wget", "axel", "aria2c",
    "nc", "telnet", "lynx", "w3m", "links", "httpie", "xh",
    "http-prompt", "chrome", "firefox", "safari",
})


def truncate_output(content: str, max_len: int = MAX_OUTPUT_LENGTH) -> str:
    """截断输出，保留前后各一半（参考 opencode）"""
    if len(content) <= max_len:
        return content
    half = max_len // 2
    start = content[:half]
    end = content[-half:]
    truncated_lines = content[half:-half].count("\n")
    return f"{start}\n\n... [{truncated_lines} lines truncated] ...\n\n{end}"


def count_tokens_approx(text: str) -> int:
    """估算 token 数（4字符≈1 token）"""
    return len(text) // 4
