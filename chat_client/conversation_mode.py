"""
@input: typing
@position: Agent layer — Ask/Plan/Craft 三档会话模式，控制工具白名单与行为
@auto-doc: Update header and folder INDEX.md when this file changes


- ask：只读问答，禁用所有写/执行类工具（HOST_READ_TOOLS 白名单）
- plan：先规划后执行，允许只读工具 + 计划产出（写计划文件），不直接改代码
- craft：全能力（默认），可写可执行

用途：用户可对某会话指定模式（如「只问不改」时用 ask），降低误操作风险。
通过 config.conversation_mode 或 web_server 的会话级设置注入。
"""
from typing import Any

# 只读工具白名单（ask 模式仅允许这些）
HOST_READ_TOOLS: frozenset[str] = frozenset({
    "Read", "Glob", "Grep", "ReadToolArchive", "LS",
    "WebFetch", "WebSearch", "curl_url", "http_check",
    "check_ssl_cert",
    "mem0_search", "mem0_search_all", "mem0_list",
    "KnowledgeSearch", "KnowledgeList",
    "ExpertMarket", "GetExpertPrompt",
    "SkillMarket",
    "ListScheduledTasks", "TodoRead", "TaskStatus", "CheckCommandStatus",
    "Skills",  # 读取技能（只加载工作流，不写）
})

# plan 模式：只读 + 计划相关（TodoWrite 允许，用于写计划）
PLAN_ALLOW_EXTRA: frozenset[str] = frozenset({"TodoWrite", "TaskSummary"})

MODES = ("ask", "plan", "craft")


def normalize_mode(mode: str | None) -> str:
    m = (mode or "craft").strip().lower()
    return m if m in MODES else "craft"


def resolve_mode(config: dict[str, Any] | None, session_mode: str | None = None) -> str:
    """解析会话模式：会话级 > config > 默认 craft。"""
    if session_mode:
        return normalize_mode(session_mode)
    if config:
        return normalize_mode(config.get("conversation_mode"))
    return "craft"


def filter_tools_for_mode(tool_names: list[str], mode: str) -> list[str]:
    """按模式裁剪工具列表。craft 不裁剪。"""
    m = normalize_mode(mode)
    if m == "craft":
        return list(tool_names)
    if m == "ask":
        return [t for t in tool_names if t in HOST_READ_TOOLS]
    if m == "plan":
        allowed = HOST_READ_TOOLS | PLAN_ALLOW_EXTRA
        return [t for t in tool_names if t in allowed]
    return list(tool_names)


def mode_instructions(mode: str) -> str:
    """返回注入系统提示的模式说明。"""
    m = normalize_mode(mode)
    if m == "ask":
        return ("\n\n## 会话模式：Ask（只读问答）\n"
                "当前为只读模式，仅可使用查询/读取类工具。"
                "不得修改文件、执行写操作或运行有副作用的命令。"
                "若用户要求改动，请说明当前是 Ask 模式并建议切换为 Craft。")
    if m == "plan":
        return ("\n\n## 会话模式：Plan（先规划后执行）\n"
                "当前为规划模式，先产出清晰的执行计划（可用 TodoWrite 记录），"
                "不直接修改代码/文件。计划确认后再由用户切换到 Craft 执行。")
    return ""
