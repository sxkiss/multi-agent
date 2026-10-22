from dataclasses import dataclass
from typing import Any, Optional
import inspect
import re


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


_TOOL_NAME_RE = re.compile(r"<tool_name>(.*?)</tool_name>", re.DOTALL)
_TOOL_STATUS_RE = re.compile(r"<toolcall_status>(.*?)</toolcall_status>", re.DOTALL)
_TOOL_BODY_RE = re.compile(r"<toolcall_result>(.*?)</toolcall_result>", re.DOTALL)


def tool_result_for_model(result: Any) -> Any:
    """把工具结果转换成面向模型的纯文本，去掉 <tool> XML 外壳。

    背景：工具执行结果内部统一用 <tool>...</tool> 封装（便于归档/展示），但
    模型侧走的是原生 function calling。把 XML 结果原样回灌，部分模型会误以为
    工具调用协议也是 XML，进而在 reasoning/正文里输出 <invoke>/<parameter>
    等伪调用标签，长会话下退化成重复闭合标签死循环。

    这里只在「发给模型」的边界做归一化：保留状态与正文，去掉工具调用标签；
    归档、前端展示仍使用原始 XML。非 <tool> 结构原样返回。
    """
    if not isinstance(result, str) or "<tool>" not in result:
        return result
    body_m = _TOOL_BODY_RE.search(result)
    if not body_m:
        return result
    name_m = _TOOL_NAME_RE.search(result)
    status_m = _TOOL_STATUS_RE.search(result)
    name = (name_m.group(1).strip() if name_m else "") or "unknown"
    status = (status_m.group(1).strip() if status_m else "done") or "done"
    body = body_m.group(1).strip("\n")
    return f"--- tool result: {name} ({status}) ---\n{body}\n--- end tool result ---"


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


def inject_duration(xml: str, seconds: float) -> str:
    """幂等注入 <duration> 耗时标签（单位：秒，保留两位小数，带 s 后缀）。

    约定沿用 Bash 工具既有的 duration 格式（如 "1.23s"），供模型感知每个工具
    的执行耗时。仅注入到 <tool> 结构且尚无 <duration> 标签时才生效——
    Bash 等工具已自带精确耗时，不覆盖。

    供 _guarded_execute 统一调用（正常完成 / 转后台 / 后台回灌路径）。
    """
    if not xml or "<tool>" not in xml or "<duration>" in xml:
        return xml
    dur_str = f"{seconds:.2f}s"
    # 注入到 <toolcall_result> 内首部（与 Bash metadata 的渲染位置一致），
    # 兼容无 <toolcall_result> 的非标准结果（原样不加工）。
    return xml.replace(
        "<toolcall_result>\n",
        f"<toolcall_result>\n<duration>{dur_str}</duration>\n",
        1,
    )


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
