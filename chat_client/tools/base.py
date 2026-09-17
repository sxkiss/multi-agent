from dataclasses import dataclass
from typing import Optional


@dataclass
class ToolResponse:
    """结构化工具响应"""
    status: str  # done, error, running
    content: str
    metadata: Optional[dict] = None
    is_error: bool = False

    def to_xml(self) -> str:
        meta_xml = ""
        if self.metadata:
            meta_items = "\n".join(f"<{k}>{v}</{k}>" for k, v in self.metadata.items())
            meta_xml = f"\n<metadata>\n{meta_items}\n</metadata>"
        return f"\n<tool>\n<toolcall_status>{self.status}</toolcall_status>{meta_xml}\n<toolcall_result>\n{self.content}\n</toolcall_result>\n</tool>\n"


def _xml_response(status: str, result: str, metadata: dict = None) -> str:
    """兼容旧接口，内部使用 ToolResponse"""
    resp = ToolResponse(status=status, content=result, metadata=metadata, is_error=(status == "error"))
    return resp.to_xml()


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
