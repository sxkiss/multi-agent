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

# 命令名黑名单：改为从 config.json 的 banned_commands 字段读取（默认空 = 不拦截任何命令）。
# 配置项修改后无需重启（每次执行命令时实时读取，带 2s 缓存）。
import json as _json
import os as _os
import time as _time

_BANNED_CACHE: dict[str, Any] = {"ts": 0.0, "cmds": frozenset()}
_BANNED_CACHE_TTL = 2.0


def get_banned_commands() -> frozenset[str]:
    """读取 config.json 的 banned_commands（命令名列表）。默认空集。"""
    now = _time.time()
    if now - _BANNED_CACHE["ts"] < _BANNED_CACHE_TTL:
        return _BANNED_CACHE["cmds"]  # type: ignore[return-value]
    cmds: frozenset[str] = frozenset()
    try:
        cfg_path = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
            "config.json",
        )
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        raw = cfg.get("banned_commands") or []
        if isinstance(raw, str):
            raw = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]
        cmds = frozenset(str(x).strip().lower() for x in raw if str(x).strip())
    except Exception:  # noqa: BLE001
        cmds = frozenset()
    _BANNED_CACHE["ts"] = now
    _BANNED_CACHE["cmds"] = cmds
    return cmds


# 兼容旧引用：模块加载时的快照（新代码请用 get_banned_commands() 实时读取）
BANNED_COMMANDS = get_banned_commands()


# --- Bash 工具默认超时（毫秒）---
# 取值优先级：config.json 的 agent.bash_timeout（秒）→ 内置默认 30s。
# 每次执行实时读取（2s 缓存），设置页保存后立即生效，无需重启。
DEFAULT_BASH_TIMEOUT_MS = 30_000
_BASH_TIMEOUT_CACHE: dict[str, Any] = {"ts": 0.0, "ms": DEFAULT_BASH_TIMEOUT_MS}
_BASH_TIMEOUT_CACHE_TTL = 2.0


def get_bash_timeout_ms() -> int:
    """读取 config.json 的 agent.bash_timeout（秒），换算为毫秒返回。

    缺失/非法时回退 30s。下限 1s、上限 3600s，避免误配成 0 或离谱值。
    """
    now = _time.time()
    if now - _BASH_TIMEOUT_CACHE["ts"] < _BASH_TIMEOUT_CACHE_TTL:
        return _BASH_TIMEOUT_CACHE["ms"]  # type: ignore[return-value]
    ms = DEFAULT_BASH_TIMEOUT_MS
    try:
        cfg_path = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
            "config.json",
        )
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = _json.load(f)
        agent_cfg = cfg.get("agent") if isinstance(cfg.get("agent"), dict) else {}
        raw = agent_cfg.get("bash_timeout", cfg.get("bash_timeout"))
        if raw is not None and str(raw).strip() != "":
            sec = float(raw)
            ms = int(min(max(sec, 1.0), 3600.0) * 1000)
    except Exception:  # noqa: BLE001
        ms = DEFAULT_BASH_TIMEOUT_MS
    _BASH_TIMEOUT_CACHE["ts"] = now
    _BASH_TIMEOUT_CACHE["ms"] = ms
    return ms


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
