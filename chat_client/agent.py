"""
@input: openai, httpx2, chat_client.memory, chat_client.retrieval, chat_client.tools.registry
@output: Agent 主循环（流式/重试）、ChatJob、事件生成
@position: Core agent runtime — 所有对话模式的执行引擎
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import datetime
import hashlib
import json
import logging
import os
import platform
import re
import threading
import time
import traceback
import uuid
from collections.abc import Generator
from typing import Any, Callable

import openai

# openai SDK 3.x 基于 httpx2（重命名版），流式中断抛 httpx2.RemoteProtocolError 等
try:
    import httpx2
except ImportError:
    httpx2 = None

from chat_client.api_retry import is_retryable_api_err
from chat_client.memory import MemoryManager
from chat_client.retrieval import ExternalRAGService, RAGService, Mem0Service

from .tools import registry, set_session_allow_high, get_background_manager
from .tools.base import _xml_response, inject_tool_name, tool_result_for_model
from .tools import PROJECT_ROOT

logger = logging.getLogger(__name__)

# L-1: tiktoken 编码器缓存在模块级，避免每次调用重复导入
_TIKTOKEN_ENC = None


def estimate_tokens(text: str) -> int:
    """估算 token 数量（CJK-aware：尽量贴近实际，避免高估/低估）。
    策略：优先尝试 tiktoken；降级到启发式估算。
    - CJK 字符（\\u4e00-\\u9fff）、全角标点、假名：约 1 字符/token（偏高，防截断过早）
    - ASCII 字母/数字：约 4 字符/token（更接近真实比）
    - 其余标点/空白：1 字符/token
    总体上限与下限均合理，避免英文严重高估、中文严重低估。

    模块级函数：_build_messages 的轮间压缩与主循环的工具内压缩闸门共用。
    """
    try:
        global _TIKTOKEN_ENC
        if _TIKTOKEN_ENC is None:
            import tiktoken
            _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
        if _TIKTOKEN_ENC:
            return max(1, len(_TIKTOKEN_ENC.encode(text)))
    except Exception:
        _TIKTOKEN_ENC = False  # 标记不可用，后续不再重试导入
    # 启发式 fallback：区分 CJK 与 ASCII
    cjk_count = sum(1 for ch in text
                    if ('\u4e00' <= ch <= '\u9fff')
                    or ('\u3000' <= ch <= '\u303f')
                    or ('\uff00' <= ch <= '\uffef'))
    ascii_chars = len(text) - cjk_count
    # CJK ~1.0 token/char，ASCII ~0.25 token/char；偏保守取中间值
    return max(1, int(cjk_count * 1.0 + ascii_chars * 0.35))

BINARY_EXTENSIONS = {
    '.zip', '.tar', '.gz', '.exe', '.dll', '.so', '.class', '.jar', '.war', '.7z',
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.pdf', '.doc', '.docx', '.xls', '.xlsx',
    '.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav', '.ogg', '.mpg', '.mpeg',
    '.iso', '.bin', '.dat', '.db', '.sqlite', '.pyc', '.pyo'
}


# ============================================================
# 任务取消（chat_stop → 后台 Agent 线程真正中断）
#
# 背景：原先 chat_stop 只做 job.finish("stopped") + agent.close()，
# 后者仅关闭 RAG/HTTP 连接，不打断正在运行的 chat() 循环——停止后
# Agent 仍会继续流式接收/重试请求/执行工具，直到下一次 API 调用恰好失败。
# 现在 ChatJob 增加 cancel_event，Agent 主循环在本模块的检查点轮询。
# ============================================================

def _cancel_event():
    """取当前执行上下文（ChatJob）的取消事件；无任务上下文（如脚本直调 Agent）返回 None。"""
    from .tools import get_current_job
    job = get_current_job()
    return getattr(job, "cancel_event", None)


def _get_current_job():
    """取当前执行上下文的 job（工具并行执行时用于上下文隔离恢复）。"""
    from .tools import get_current_job
    return get_current_job()


def _is_cancelled() -> bool:
    """当前任务是否已被 chat_stop 请求停止。"""
    ev = _cancel_event()
    return ev is not None and ev.is_set()


def _sleep_interruptible(seconds: float) -> bool:
    """可被取消打断的等待。返回 True 表示等待期间收到取消请求。"""
    ev = _cancel_event()
    if ev is None:
        time.sleep(seconds)
        return False
    return ev.wait(seconds)


def _close_stream(stream) -> None:
    """关闭流式响应，释放底层连接（失败静默——取消路径不因清理异常中断）。"""
    try:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    except Exception:
        logger.debug("关闭响应流失败（已忽略）", exc_info=True)


def bad_args_hint(func_name: str) -> str:
    """工具参数非法（多为输出被截断）时回灌给模型的错误提示。

    单独抽出便于测试与复用：重点是让模型明白"别重发同一份超长参数"，
    而不是像旧实现那样静默按空参数执行、让模型误以为成功而无限重试。
    """
    return _xml_response(
        "error",
        f"Error: 工具 {func_name} 的参数不是合法 JSON（很可能是内容过长被截断）。"
        "不要重复提交同一份超长参数：请把大文件拆成多次较小的写入"
        "（先写骨架，再分次追加），或改用更短的实现后重试。",
    )


# ---------------- 图片（多模态）支持 ----------------
# 上游实测支持 OpenAI 风格 image_url 内容块（模型:auto 可正确识别图片内容）。
# 但历史上有两条链路会把图片吃掉：
#   1) webfetch 等把图片编码成 {"type":"file","mime":"image/png","url":"data:..."}，
#      而 _filter_file_blocks 会把 type="file" 整块删除 → 模型永远看不到图。
#   2) 图片若以 file 块存在，形状也不符合视觉协议（应为 image_url）。
# 这里统一做形状归一：图片 file 块 → image_url；非图片 file 块仍丢弃。


def _file_block_to_image_url(block: Any):
    """把 file 块里的图片转成标准 image_url 块；非图片返回 None。

    只认 data:image/ 前缀或 image/* mime，避免把普通附件误当图片发给模型。
    """
    if not isinstance(block, dict) or block.get("type") != "file":
        return None
    url = block.get("url") or ""
    if not isinstance(url, str) or not url.strip():
        return None
    mime = str(block.get("mime") or block.get("mime_type") or "").lower()
    if not (mime.startswith("image/") or url.startswith("data:image/")):
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _content_for_token_estimate(content: Any) -> Any:
    """token 估算用：把图片的 base64 载荷替换为占位符。

    base64 长度与真实 token 数无关（上游按图像分辨率切 patch），若按字符估算，
    一张几十 KB 的图会被算成十几万 token，直接误触发上下文压缩。
    """
    if not isinstance(content, list):
        return content
    out = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            out.append({"type": "image_url", "image_url": {"url": "[image]"}})
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            out.append({"type": "text", "text": item["text"]})
        else:
            out.append({"type": "text", "text": "[non-text]"})
    return out


def _drop_image_blocks(content: Any) -> Any:
    """丢弃历史轮次里的图片，替换为占位文本。

    图片以 base64 存在历史里，若每轮都重发，上下文会迅速膨胀（且重复计费）。
    只保留最新一轮的图，旧图降级为文字提示，既省 token 又保留"此处曾有图"的语义。
    """
    if not isinstance(content, list):
        return content
    out, dropped = [], 0
    for item in content:
        if isinstance(item, dict) and item.get("type") == "image_url":
            dropped += 1
            continue
        out.append(item)
    if dropped:
        out.append({"type": "text", "text": f"[历史消息中的 {dropped} 张图片已省略]"})
    return out


class Agent:
    # 集团协作工具：需要多 Agent 编排，单 Agent / Codex-X 模式不加载
    _GROUP_TOOLS: set[str] = {"RunCrew", "ConsultPeer", "CreateDepartment", "RecruitMember"}

    # ── 工具并行：按「资源冲突」判定（2026-10-15）──────────────────
    # 对齐 Claude Code / Cursor 的做法：不按工具名一刀切，而是看**资源冲突**。
    #   · 两个 Edit 改不同文件 → 可并行；改同一文件 → 串行
    #   · 两个 restart_service 重启不同服务 → 可并行；同一服务 → 串行
    #   · 只读工具（Read/Grep/查询类）无写资源 → 自由并行
    #   · 有全局共享状态、无法用资源键区分的（Bash/TodoWrite/MCP/Skills/
    #     InstallExpert/TaskSummary 等）→ 独占串行
    #   · 子代理（Task/ConsultPeer）无共享状态 → 并行
    # 可用 config 覆盖：tool_parallel_force（强制并行）、tool_parallel_deny
    #                  （强制串行，优先级最高）。

    # 独占工具：无可用资源键区分、共享全局状态，必须串行
    _PARALLEL_EXCLUSIVE: set[str] = {
        "Bash",                       # cwd/后台任务/环境共享，且顺序敏感
        "TodoWrite", "TaskSummary",   # 单文件读改写，全局唯一
        "MCP", "Skills",              # 插件/技能加载全局状态
        "InstallExpert", "RecruitMember", "CreateDepartment",  # 写 crew_agents.json
        "GrantWriteAccess", "ManageWritePaths",  # 全局写白名单
        "cron_manage", "cleanup_temp",           # 系统级共享
    }
    # 写类工具 → 资源键为文件路径（同文件串行，异文件并行）
    _WRITE_FILE_TOOLS: dict[str, tuple[str, ...]] = {
        "Write": ("file_path",),
        "Edit": ("file_path",),
        "LargeFileEdit": ("file_path",),
        "compress_files": ("archive_path",),   # 输出包路径
        "extract_archive": ("dest_dir",),       # 解压目标目录
    }
    # 服务/容器类工具 → 资源键为服务名/容器名（同目标串行，异目标并行）
    _RESOURCE_TOOLS: dict[str, tuple[str, ...]] = {
        "restart_service": ("service_name",),
        "manage_service": ("service_name",),
        "docker_manage": ("container_id",),
        "kill_process": ("pid",),
        "ScheduleTask": ("name",),
        "CancelScheduledTask": ("name_or_id",),
        "RunScheduledTaskNow": ("name_or_id",),
    }
    # 只读工具：无写资源，自由并行
    _PARALLEL_ALLOW: set[str] = {
        "Read", "Glob", "Grep", "ReadToolArchive",
        "TaskStatus", "CheckCommandStatus", "TaskStop", "StopCommand",
        "WebFetch", "WebSearch", "curl_url", "http_check", "ping_target",
        "check_domain", "check_ssl_cert", "get_server_ip",
        "get_current_time", "get_service_status", "get_system_resources",
        "get_top_processes", "get_docker_info", "get_docker_containers",
        "get_docker_inspect", "get_docker_logs", "get_firewall_status",
        "get_mysql_list", "get_sites", "get_sites_conf", "get_sites_logs",
        "get_site_analysis", "list_port_bindings", "dir_usage", "find_large_files",
        "tail_log",
        "mem0_search", "mem0_search_all", "mem0_list",
        "KnowledgeSearch", "KnowledgeList",
        "ExpertMarket", "GetExpertPrompt", "ExpertScenes",
        "SkillMarket", "SkillCategories",
        "ListScheduledTasks", "TodoRead",
        # 子代理：本身无共享状态，可并行（本次修复的主要目标）
        "Task", "ConsultPeer",
    }

    def _tool_resource_key(self, func_name: str, args: dict) -> str | None:
        """计算工具调用的「资源键」。返回 None 表示：
        - 只读/无冲突 → 自由并行（返回特殊值 "·free"）
        - 独占 → 返回 "·exclusive"（与任何其它调用冲突）
        """
        force = set(self.config.get("tool_parallel_force") or [])
        deny = set(self.config.get("tool_parallel_deny") or [])
        if func_name in deny:
            return "·exclusive"
        if func_name in force:
            return "·free"
        if func_name in self._PARALLEL_EXCLUSIVE:
            return "·exclusive"
        # 写文件类：资源键 = 规范化后的文件路径
        keys = self._WRITE_FILE_TOOLS.get(func_name)
        if keys:
            for k in keys:
                v = args.get(k)
                if v:
                    try:
                        return "file:" + os.path.realpath(str(v))
                    except Exception:
                        return "file:" + str(v)
            return "·exclusive"  # 拿不到路径，保守独占
        # 服务/容器类：资源键 = 目标标识
        rkeys = self._RESOURCE_TOOLS.get(func_name)
        if rkeys:
            for k in rkeys:
                v = args.get(k)
                if v not in (None, ""):
                    return f"res:{func_name}:{v}"
            return "·exclusive"
        if func_name in self._PARALLEL_ALLOW:
            return "·free"
        # 未知工具：保守独占（宁可慢，不可错）
        return "·exclusive"

    def _plan_parallel_groups(self, plans: list[dict]) -> list[list[dict]]:
        """把待执行计划按资源冲突分组：同组内无冲突可并行，组间串行。

        算法：贪心装箱——依次把每个计划放进「第一个无资源冲突的组」，
        放不进则新开一组。这样只读/异文件工具会尽量并入同组并行，
        冲突的自动落到后续组串行。保持原有相对顺序。
        """
        groups: list[list[dict]] = []
        group_keys: list[set[str]] = []
        for p in plans:
            key = p.get("_res_key")
            if key == "·exclusive":
                groups.append([p]); group_keys.append({"·exclusive"})
                continue
            placed = False
            for gi, gk in enumerate(group_keys):
                if "·exclusive" in gk:
                    continue  # 独占组不再并入
                if key == "·free" or key not in gk:
                    groups[gi].append(p)
                    gk.add(key)
                    placed = True
                    break
            if not placed:
                groups.append([p]); group_keys.append({key})
        return groups

    # 工具参数连续非法的熔断阈值：达到即结束本轮，避免"截断→非法→空跑→再截断"死循环
    BAD_ARGS_MAX_STREAK = 3

    # 进程级开关：上游网关（Responses 协议）无法反序列化 reasoning_content + tools 组合时，
    # 400 报 "input: data did not match any variant of untagged enum ResponseInput"。
    # 确认一次后全进程剥离 reasoning_content，避免每个新 Agent 实例都先吃一次 400。
    _upstream_rejects_reasoning = False

    @staticmethod
    def _is_response_input_400(err: Exception) -> bool:
        """识别 Responses 协议网关的确定性 400（json_parse_error / ResponseInput）。"""
        msg = str(err)
        return "ResponseInput" in msg or (
            "400" in msg and "json_parse_error" in msg
        )

    @staticmethod
    def next_bad_args_streak(current: int) -> int:
        """累计工具参数非法次数（解析成功时由调用方直接重置为 0）"""
        try:
            return int(current) + 1
        except (TypeError, ValueError):
            return 1

    def __init__(self, session_id: str, config: dict[str, Any] | None = None):
        self.session_id = session_id
        self.config = config or {}
        
        # 提取配置
        self.api_key = self.config.get("api_key")
        self.base_url = self.config.get("base_url")
        self.model_name = self.config.get("model_name") or self.config.get("default_model")
        self.rag_trigger_threshold = self.config.get("rag_trigger_threshold", 10)
        try:
            self.max_tool_iterations = max(1, int(self.config.get("max_tool_iterations", 9999)))
        except (TypeError, ValueError):
            self.max_tool_iterations = 9999
        # 工具参数非法（多为输出被截断）的连续计数，用于熔断，防止无限重试烧 token
        self._bad_args_streak = 0
        self.context_window_kb = self.config.get("context_window_kb", 512)
        self.enabled_tools = self.config.get("tools", [])
        self.original_tools = list(self.enabled_tools)  # 保存用户原始指定工具，防止被默认工具覆盖
        self.default_headers = self.config.get("default_headers", {})
        self.system_prompt = self.config.get("system_prompt", "")
        self.temperature = self.config.get("temperature", 1)
        self.top_p = self.config.get("top_p", 1)
        # reasoning_effort / thinking：优先用 web_server 传入值；缺省回退到配置文件，再回退到安全默认值
        # 注意：配置节在 self.config["agent"] 下，而非顶层，需从这里取
        _agent_cfg = self.config.get("agent", {}) if isinstance(self.config.get("agent"), dict) else {}
        # 工具前台等待上限（秒）：从配置注入到全局注册表，自动钳制到 [10, 120]。
        # 超过该上限的工具调用不终止，转后台执行，结果由主循环回灌。
        try:
            registry.set_tool_timeout(_agent_cfg.get("tool_timeout", 120))
        except Exception:
            logger.warning("[ToolTimeout] 注入 tool_timeout 失败，沿用当前值", exc_info=True)
        self.reasoning_effort = self.config.get("reasoning_effort") or _agent_cfg.get("reasoning_effort", "high")
        # 协议开关：chat（默认，走 /v1/chat/completions）| responses（走 /v1/responses）
        self.api_protocol = str(
            self.config.get("api_protocol") or _agent_cfg.get("api_protocol") or "chat"
        ).strip().lower()
        self.thinking = self.config.get("thinking", _agent_cfg.get("thinking", False))
        self.web_search = self.config.get("web_search", _agent_cfg.get("web_search", False))

        # ── 失控防护：单次输出有硬上限、拒绝死循环 ──
        # 单次响应最大输出 token（含思考 token）。0 = 不限制（不推荐）。
        # 此前完全不传 max_tokens，模型一旦退化会一直流式输出/思考到上游上限。
        try:
            self.max_output_tokens = int(
                self.config.get("max_output_tokens")
                or _agent_cfg.get("max_output_tokens", 32768)
            )
        except (TypeError, ValueError):
            self.max_output_tokens = 32768
        # 单次模型响应墙钟上限（秒）。防止"边想边流"永远不结束。0 = 不限制。
        try:
            self.response_timeout_seconds = float(
                self.config.get("response_timeout_seconds")
                or _agent_cfg.get("response_timeout_seconds", 600)
            )
        except (TypeError, ValueError):
            self.response_timeout_seconds = 600
        # 重复熔断：连续 N 轮输出完全相同的内容 / 完全相同的工具调用 → 判定为幻觉死循环
        try:
            self.repeat_limit = max(2, int(
                self.config.get("repeat_limit") or _agent_cfg.get("repeat_limit", 3)
            ))
        except (TypeError, ValueError):
            self.repeat_limit = 3
        self._repeat_content = None
        self._repeat_content_streak = 0
        self._repeat_tool_sig = None
        self._repeat_tool_streak = 0
        # reasoning 重复检测：同一轮思考里同一句话重复过多视为退化，
        # 命中后丢弃本次响应并重新发起请求（不中断任务）。
        # 默认：句子归一化后重复 >= 8 次触发；最多额外重试 1 次（共 2 次尝试）。
        try:
            self.reasoning_repeat_limit = max(3, int(
                self.config.get("reasoning_repeat_limit")
                or _agent_cfg.get("reasoning_repeat_limit", 8)
            ))
        except (TypeError, ValueError):
            self.reasoning_repeat_limit = 8
        try:
            self.reasoning_repeat_retries = max(0, int(
                self.config.get("reasoning_repeat_retries")
                or _agent_cfg.get("reasoning_repeat_retries", 3)
            ))
        except (TypeError, ValueError):
            self.reasoning_repeat_retries = 3
        # 轮询类工具允许同参数连续调用，不参与重复熔断
        self._repeat_exempt_tools = {"TaskStatus", "TaskList", "CheckCommandStatus"}

        # 官网知识库
        self.use_external_kb = self.config.get("use_external_kb", False)
        self.external_kb_appid = self.config.get("external_kb_appid", "app_002")

        # 全局记忆检索开关（mem0 / ExternalRAG）
        self.use_global_rag = self.config.get("use_global_rag", False)

        # 文档知识库开关（本机 knowledge/ 目录，与 mem0 记忆独立）
        # 与 use_global_rag 的区别：mem0 存"记忆/偏好"，这里存"文档/资料"
        self.use_document_kb = self.config.get("use_document_kb", True)
        self._doc_kb = None  # 懒加载（首次检索时才建 embedding 客户端）
        
        # Code mode configuration
        self.code_mode = self.config.get("code_mode", False)
        # 严格工具模式：完全以 config["tools"] 为准（集团模式下主对话=经理仅编排工具；
        # 成员代理各自定义工具），不追加任何默认/MCP 工具
        self.strict_tools = self.config.get("strict_tools", False)
        
        # 卡密用户隔离：检测是否为卡密会话，限制文件系统访问范围
        self._is_card_user = str(session_id).startswith("card_")
        if self._is_card_user:
            # 卡密用户 workspace: workspace/users/card_<PREFIX>/
            prefix = session_id[5:]  # 去掉 "card_" 前缀
            _base = self.config.get("base_dir", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            self._user_workspace = os.path.realpath(os.path.join(_base, "workspace", "users", f"card_{prefix}"))
            logger.info(f"卡密用户 {session_id}，工作目录限制为: {self._user_workspace}")
        else:
            self._user_workspace = None

        if self.code_mode:
            # Append environment info to system prompt
            self.current_dir = self.config.get("cwd")
            logger.debug(f"current_dir in config: {self.current_dir}")
            self.system_prompt += self._get_environment_info()
        
        # 加载 AGENTS.md 层级指令
        # 集团模式下始终加载；单 Agent 模式下仅在未指定系统提示词时加载
        # 单 Agent 模式下自动过滤集团模式相关内容（如 RunCrew 调度规则），减少 token 消耗
        if self.config.get("enable_agents_md", True):
            mode = self.config.get("mode", "group")
            # 单 Agent 模式已指定预设提示词时跳过 AGENTS.md
            should_load_md = mode == "group" or not self.system_prompt
            if should_load_md:
                agents_md = self._load_agents_md()
                if agents_md:
                    if mode in ("single", "codex_x"):
                        agents_md = self._filter_agents_md_for_single(agents_md)
                    self.system_prompt += f"\n\n## Project Instructions (AGENTS.md)\n\n{agents_md}"

        # 注入技能摘要，让 AI 知道有哪些技能可用（Codex 式三层披露 Layer 1）
        if not self.strict_tools and mode in ("single", "codex_x"):
            try:
                from .skills import skill_manager
                skill_summary = skill_manager.generate_skill_summary()
                if skill_summary:
                    self.system_prompt += f"\n\n{skill_summary}"
                    logger.info(f"技能摘要已注入系统提示（{len(skill_manager.all_enabled())} 个技能）")
            except Exception as e:
                logger.debug(f"技能摘要注入失败: {e}")
            try:
                from .experts import get_catalog
                _base = self.config.get("base_dir") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                expert_summary = get_catalog(_base).generate_expert_summary()
                if expert_summary:
                    self.system_prompt += f"\n\n{expert_summary}"
                    logger.info("专家库摘要已注入系统提示（自动发现 %d 位）",
                                len(get_catalog(_base).scan()))
            except Exception as e:
                logger.debug(f"专家摘要注入失败: {e}")
            # 技能使用硬规则：强制先评估技能再动手（2026-10-13 boss 反馈"有技能不用"）
            self.system_prompt += (
                "\n\n## 技能使用铁律（必须遵守）\n"
                "- 接到任何任务，**第一步先在上方技能清单里查找**有没有匹配的技能："
                "逐条比对任务的领域/触发词与技能的 description（尤其看\"遇到以下情况就使用\"\"适用场景\"\"触发词\"段落）。\n"
                "- **只要找到匹配技能，必须先调用 `Skills(name=\"技能名\")` 加载该技能的完整工作流**，再按技能流程执行；禁止跳过技能直接用通用手段硬干。\n"
                "- 技能匹配规则：任务涉及 excel/表格/数据分析→查数据分析类；ppt/演示→查 PPT 类；"
                "seo/排名→查 SEO 类；浏览器/爬虫→查 Web 类；逆向/安全→查安全类；微信/图片/视频→查社媒类；"
                "运维/部署/服务器→查 DevOps 类；写代码→查编程语言类；识别图片/OCR→查 SN 技能。\n"
                "- 一个任务可能匹配多个技能：按\"最贴合\"优先加载 1 个主技能，必要时再加载 1 个辅助技能。\n"
                "- 确实没有匹配技能（清单里无任何相关项）时，才允许用通用工具（Bash/Read/Write 等）执行。\n"
                "- **自查**：回复用户前，确认自己是否漏用了技能——若清单里有明显匹配项而你没加载，就是违规。"
            )
            # 市场工具主动查询模板（2026-10-13）：专家市场/技能市场 发现与角色选用
            self.system_prompt += (
                "\n\n## 市场发现工具（主动使用，不要等用户给链接）\n"
                "- 专家市场：429 个领域专家（含完整人设提示词），覆盖营销/技术/设计/运营等。"
                "当用户需要专业角色/人设/专家时（如\"帮我找营销专家\"\"用产品经理视角分析\"），"
                "主动调用 `ExpertMarket(keyword=...)` 搜索，再用 `GetExpertPrompt(name=...)` 取提示词作为角色设定。\n"
                "- **本地没有的专家会自动缓存**：`GetExpertPrompt`/`InstallExpert` 取用市场专家后，"
                "会落盘到 expert_market/ 并被自动发现，之后即算本地已有（无需重复联网）。"
                "所以遇到任务先看上方【本地专家库】，没有再查市场。\n"
                "- 技能市场：SkillHub 社区公开技能库。当本地技能不够、用户要找新技能/插件时，"
                "调用 `SkillMarket(keyword=...)` 或 `SkillCategories()` 探索。\n"
                "- 精选场景：`ExpertScenes()` 查看专家市场精选场景。\n"
                "- 用法示例：用户说\"我需要一个内容营销专家\"→ 调 `ExpertMarket(keyword=\"内容创作\")`"
                "→ 命中后调 `GetExpertPrompt(name=\"content-creator\")` 加载人设。\n"
                "- **自查**：回复用户前，若涉及专家角色/外部技能需求而你没调过市场工具，就是漏用。"
            )
            # 专家派发铁律（2026-10-21 boss 要求，对齐 Octop）：
            # 专家必须是**独立 agent 实例**执行，主会话只调度与转述，禁止自己扮演专家。
            self.system_prompt += (
                "\n\n## 专家派发铁律（必须遵守，优先级高于一般对话习惯）\n"
                "**核心原则：专家 = 独立 Agent 实例，不是主会话的一个人格。**\n"
                "- 当用户需求命中上方【本地专家库】中某位专家的领域或触发词（triggers），"
                "**必须把任务派发给独立 agent 执行，禁止主会话自己扮演/直接加载该专家人格**。\n"
                "- 派发方式（按优先级）：\n"
                "  1. 该专家**已是团队成员**（crew_agents.json 中已有该成员）→ "
                "直接 `Task(subagent_type=\"成员名\", description=..., prompt=..., expected_output=...)`；\n"
                "  2. 该专家**在本地专家库但尚未成为成员** → 先 `InstallExpert(name=\"专家id\")` "
                "落地为成员，再 `Task(subagent_type=\"成员名\", ...)` 派发；\n"
                "  3. 本地没有 → `ExpertMarket` 搜 → `InstallExpert` 落地 → `Task` 派发。\n"
                "- **主会话职责边界**：只做「意图识别 → 派发 → 等待结果 → 向你转述」。"
                "主会话不得自己执行专家的专业流程、不得自己扮演专家口吻、不得跳过 Task 直接把专家提示词"
                "塞进自己的上下文当成自己的人格。\n"
                "- **例外（仅限）**：用户明确要求「你本人直接回答/别派子 agent」时，才可主会话直接处理。\n"
                "- **自查**：回复前确认——这次涉及专家领域吗？如果涉及，我是否真的调用了 "
                "`Task(subagent_type=...)` 派给独立 agent？没有就是违规。\n"
                "- 用户只做闲聊或通用问题时，不触发本规则。\n"
            )
            # 注入 mem0 记忆使用提示
            self.system_prompt += "\n\n## 记忆系统\n- `mem0_search` 查询历史记忆（用户偏好、踩坑记录、已完成任务）\n- `mem0_add` 写入重要信息（用户明确说'记住'、协作规则、技术决策）\n- `mem0_list` 列出所有记忆（按 agent_id 过滤，默认50条）\n- `mem0_delete` 删除指定 ID 的记忆（不可撤销）\n- `mem0_import` 批量导入（多行文本，每行一条）\n- 涉及跨会话上下文、用户偏好、历史决策时主动调用，不要凭空回答"

        if not self.strict_tools:
            # 单 Agent 模式：默认全量工具，排除集团协作工具
            # 集团工具（Task/RunCrew/ConsultPeer/CreateDepartment/RecruitMember）需要多 Agent 编排
            _all_ids = {m["id"] for m in registry._metadata.values()}
            _add = _all_ids - self._GROUP_TOOLS
            # 工具下发裁剪（P1，2026-10-15）：AI_AGENT_TOOL_DISCLOSURE=trim 时，
            # 门控组（媒体/数据库/知识库/记忆/市场/定时/网站）默认不下发，
            # 默认 full = 全量，保持向后兼容。显式指定在 config["tools"] 里的工具
            # 或 config["tool_gate_enabled"] 组始终保留。
            try:
                from .tools import disclosure_mode, get_gated_tools
                if disclosure_mode() == "trim":
                    _enabled_groups = set(self.config.get("tool_gate_enabled", []) or [])
                    _extra_gate = {
                        t.strip() for t in
                        (self.config.get("tool_extra_gate") or "").split(",") if t.strip()
                    }
                    _gated = get_gated_tools(_enabled_groups, _extra_gate)
                    # 用户显式指定的工具（original_tools）优先保留，不受门控
                    _explicit = {registry.get_tool_id(t) or t for t in self.original_tools}
                    _gated -= _explicit
                    _add = _add - _gated
                    logger.info(
                        "[ToolDisclosure] trim 模式：门控 %d 个工具（启用组=%s），实际下发 %d 个",
                        len(_gated), sorted(_enabled_groups) or "无", len(_add | set(self.enabled_tools)),
                    )
            except Exception:
                logger.warning("[ToolDisclosure] 裁剪失败，回退全量下发", exc_info=True)
            self.enabled_tools.extend(t for t in _add if t not in self.enabled_tools)

            # ask=只读问答 / plan=先规划 / craft=全能力。按模式裁剪工具白名单。
            try:
                from .conversation_mode import resolve_mode, filter_tools_for_mode, mode_instructions
                _mode = resolve_mode(self.config, self.config.get("session_mode"))
                if _mode != "craft":
                    self.enabled_tools = filter_tools_for_mode(self.enabled_tools, _mode)
                    _instr = mode_instructions(_mode)
                    if _instr:
                        self.system_prompt += _instr
                    logger.info("[ConversationMode] %s 模式：工具裁剪至 %d 个",
                                _mode, len(self.enabled_tools))
            except Exception:
                logger.debug("[ConversationMode] 应用失败，回退全量", exc_info=True)
            
        self.memory = MemoryManager(
            session_id=session_id, 
            sessions_dir=self.config.get("sessions_dir", "sessions"),
            sliding_window_size=self.config.get("sliding_window_size", 10)
        )

        # 压缩失败连击计数：达到阈值层数上限前，失败只升级阈值、不硬截断
        self._compress_fail_streak = 0
        # 本会话已成功压缩的轮数（实例内累加，用于 90%→95%→97% 渐进）。
        # 不能用 history 里的摘要条数代替：每轮成功都会删旧摘要插新摘要，
        # 该数恒为 1，渐进逻辑会永远卡在第二层。
        self._compress_round = 0
        
        # 将 MemoryManager 确定的 session_dir 传递给 RAGService
        self.rag = RAGService(
            session_dir=self.memory.session_dir,
            openai_api_key=self.api_key,
            openai_base_url=self.base_url,
            embedding_api_key=self.config.get("embedding_api_key"),
            embedding_base_url=self.config.get("embedding_base_url"),
            embedding_model_name=self.config.get("embedding_model_name"),
            small_model_name=self.config.get("small_model_name"),
            rag_retrieval_count=self.config.get("rag_retrieval_count", 10),
            rag_final_count=self.config.get("rag_final_count", 5),
            default_headers=self.default_headers
        )

        # 全局的知识库 RAG Service
        if self.use_external_kb:
            self.global_rag = ExternalRAGService(
                enable_rag_judgment=self.config.get("enable_rag_judgment", True),
                default_headers=self.default_headers
            )
        else:
            self.global_rag = Mem0Service(
                agent_id=self.config.get("global_kb_agent_id", "ai-agent"),
                rag_final_count=self.config.get("rag_final_count", 5),
                mem0_api_url=self.config.get("mem0_api_url", "http://localhost:8000")
            )
        
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers,
            max_retries=self.config.get("max_retries", 5),
            # 不设 timeout 时 SDK 默认 600s：压缩等辅助调用一旦挂住会拖垮整个请求
            timeout=self.config.get("api_timeout", 120),
        )
        
        # 初始化 MCP 客户端（幂等，仅加载一次）
        self._mcp_loaded = False
        self._enable_mcp = self.config.get("enable_mcp", True)
        
        if self._enable_mcp:
            try:
                # 只检查是否已加载，不在此处触发连接（防阻塞事件循环 504 超时）
                from chat_client.mcp_client import get_mcp_client
                mcp_client = get_mcp_client()
                if not mcp_client.is_loaded():
                    logger.debug("MCP 尚未加载完成（预加载线程处理中），跳过")
                else:
                    self._mcp_loaded = True
                    if not self.strict_tools:
                        for schema in mcp_client.get_tool_schemas():
                            tid = schema["function"]["name"]
                            if tid not in self.enabled_tools:
                                self.enabled_tools.append(tid)
                    logger.info(f"MCP 客户端已加载，{len(mcp_client.get_tool_schemas())} 个工具可用")
            except Exception as e:
                logger.warning(f"MCP 客户端初始化失败（不影响核心功能）: {e}")
        
    def _get_environment_info(self) -> str:
        """Constructs environment information string to append to system prompt."""
        cwd = self.current_dir
        if os.path.exists(cwd):
            is_git = os.path.isdir(os.path.join(cwd, ".git"))
        else:
            is_git = False
        plat = platform.system().lower()
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        
        # Note: model info is usually handled by the caller/config, 
        # but we can try to include what we have.
        # The prompt template requested:
        # You are powered by the model named ${model.api.id}. The exact model ID is ${model.providerID}/${model.api.id}
        
        env_info = f"""

You are powered by the model named {self.model_name}.

Here is some useful information about the environment you are running in:
<env>
  Working directory: {cwd}
  Is directory a git repo: {"yes" if is_git else "no"}
  Platform: {plat}
  Today's date: {today}
</env>
<directories>
</directories>
"""
        return env_info

    def _load_agents_md(self) -> str:
        """加载 AGENTS.md 层级指令（非集团模式不加载全局 ~/.codex/AGENTS.md）"""
        try:
            from .agents_md import agents_md_manager
            mode = self.config.get("mode", "group")
            
            if mode == "codex_x":
                return ""
            else:
                cwd = self.current_dir if hasattr(self, 'current_dir') else os.getcwd()
                return agents_md_manager.load(cwd, load_global=False)
        except Exception as e:
            logger.debug(f"加载 AGENTS.md 失败: {e}")
            return ""

    def _filter_agents_md_for_single(self, content: str) -> str:
        """单 Agent 模式下过滤 AGENTS.md 中的集团模式相关内容，减少 token 消耗"""
        import re
        # 按 ## 标题拆分，移除包含集团/经理/调度/RunCrew 等关键词的段落
        _GROUP_KEYWORDS = {"集团", "经理", "调度", "RunCrew", "编排", "分派", "部门", "成员代理", "Boss 汇报"}
        sections = re.split(r'(?=^## )', content, flags=re.MULTILINE)
        filtered = []
        for section in sections:
            # 标题行（## 开头）检查关键词
            header = section.split('\n', 1)[0] if '\n' in section else section
            if any(kw in header for kw in _GROUP_KEYWORDS):
                continue
            # 内容段也检查（防止关键词在正文中但标题不含关键词的情况）
            if any(kw in section for kw in _GROUP_KEYWORDS):
                continue
            filtered.append(section)
        return ''.join(filtered).strip()

    def _is_binary_file(self, file_path: str) -> bool:
        """检查文件是否为二进制文件"""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in BINARY_EXTENSIONS:
            return True
        
        try:
            with open(file_path, 'rb') as f:
                chunk = f.read(8192)
                if b'\x00' in chunk:
                    return True
        except Exception as e:
            # L-9: 明确记录异常类型，便于排查读文件失败原因
            logger.debug("判断二进制文件失败 %s: %s", file_path, e)
            return False
        return False

    def _process_file_reference(self, file_path: str) -> tuple:
        """
        处理单个文件引用，返回 (call_prompt, result) 元组
        """
        if not os.path.exists(file_path):
            return (
                f'Called the Read tool with the following input: {{"filePath":"{file_path}"}}',
                f'ERROR: 文件路径不存在: {file_path}'
            )
        
        if os.path.isdir(file_path):
            call_prompt = f'Called the LS tool with the following input: {{"path":"{file_path}"}}'
            try:
                from .tools.agent_tools import LS
                result = LS(path=file_path)
            except Exception as e:
                result = f'ERROR: 读取文件夹失败: {e!s}'
            return (call_prompt, result)
        
        if self._is_binary_file(file_path):
            return (
                f'Called the Read tool with the following input: {{"filePath":"{file_path}"}}',
                f'ERROR: 当前是二进制文件还不支持读取: {file_path}'
            )
        
        call_prompt = f'Called the Read tool with the following input: {{"filePath":"{file_path}"}}'
        try:
            from .tools.agent_tools import Read
            result = Read(file_path=file_path)
        except Exception as e:
            result = f'ERROR: 读取文件失败: {e!s}'
        return (call_prompt, result)

    def _process_user_input_files(self, user_input: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """
        处理用户输入中的文件引用，将文件内容追加到 content 列表中
        """
        if isinstance(user_input, str):
            return user_input
        
        if not isinstance(user_input, list):
            return user_input
        
        file_refs = [item for item in user_input if isinstance(item, dict) and item.get("type") == "file"]
        
        if not file_refs:
            return user_input
        
        new_content = list(user_input)
        
        for file_ref in file_refs:
            file_path = file_ref.get("path", "")
            if not file_path:
                continue
            
            call_prompt, result = self._process_file_reference(file_path)
            
            new_content.append({
                "type": "text",
                "text": call_prompt
            })
            new_content.append({
                "type": "text",
                "text": result
            })
        
        return new_content

    def close(self):
        """
        关闭 Agent，释放资源。
        """
        self.rag.close()
        self.global_rag.close()
        self.client.close()

    def add_global_knowledge(self, text: str, metadata: dict | None = None):
        """
        向全局知识库添加文档。
        """
        self.global_rag.add_document(text, metadata)

    def _get_doc_kb(self):
        """懒加载文档知识库实例（knowledge/ 目录）。

        仅在开启且首次检索时构造，避免每轮对话都新建 embedding 客户端。
        embedding 未配置时返回 None（检索静默降级，不影响主对话）。
        """
        if self._doc_kb is not None:
            return self._doc_kb
        if not getattr(self, "use_document_kb", False):
            return None
        try:
            from chat_client.knowledge_base import KnowledgeBase, build_embed_fn
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            kb_dir = self.config.get("document_kb_dir") or os.path.join(base_dir, "knowledge")
            emb_api_key = self.config.get("embedding_api_key") or ""
            emb_base_url = self.config.get("embedding_base_url") or ""
            if not emb_api_key or not emb_base_url:
                logger.debug("[KB] embedding 未配置，文档知识库不启用")
                return None
            embed = build_embed_fn(
                emb_api_key, emb_base_url,
                self.config.get("embedding_model_name") or "text-embedding-3-small",
                default_headers=self.default_headers,
            )
            self._doc_kb = KnowledgeBase(kb_dir, embed)
            return self._doc_kb
        except Exception:
            logger.warning("[KB] 文档知识库初始化失败，已降级", exc_info=True)
            return None

    def search_document_kb(self, query: str, top_k: int = 5, score: float = 0.35) -> list[dict]:
        """检索文档知识库，返回带引用的命中列表（失败返回空）。

        阈值 0.35 高于交互式默认的 0.2：自动注入是"顺带"增强，
        宁可漏一点也不要把无关段落塞进上下文干扰回答。
        """
        kb = self._get_doc_kb()
        if kb is None:
            return []
        try:
            res = kb.search(query, top_k=top_k, score=score)
            if not res.get("status"):
                return []
            return res.get("results", []) or []
        except Exception:
            logger.warning("[KB] 文档知识库检索失败，已降级", exc_info=True)
            return []

    @staticmethod
    def _coerce_tool_args(func_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """按 schema 把模型可能传成字符串的参数转回真实类型（如 timeout='60000' -> 60000）。"""
        schema = next((s for s in registry._schemas if s["function"]["name"] == func_name), None)
        if not schema:
            return args
        props = schema["function"].get("parameters", {}).get("properties") or {}
        out = dict(args)
        for k, v in out.items():
            spec = props.get(k)
            if not spec:
                continue
            t = spec.get("type")
            if t == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
                out[k] = int(v.strip())
            elif t == "number" and isinstance(v, str):
                try:
                    out[k] = float(v.strip())
                except ValueError:
                    logger.warning("异常被静默吞掉，已记录", exc_info=True)
            elif t == "boolean" and isinstance(v, str):
                out[k] = v.strip().lower() in ("true", "1", "yes", "on")
        return out

    def _create_completion_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None):
        # 前缀缓存：对 system 消息打 cache_control，使稳定的 system 前缀命中缓存，
        # 避免每轮全量重复计费（兼容 litellm/OpenAI 前缀缓存；不支持的代理会忽略该字段）
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages = [dict(messages[0], cache_control={"type": "ephemeral"})] + messages[1:]

        # 工具定义是稳定的固定税（83 工具 ≈ 21.5K tokens/轮），单独打缓存标记。
        # 只给最后一个工具打 cache_control，使整个 tools 数组构成缓存前缀
        # （Anthropic / 支持前缀缓存的代理会命中；不支持的会忽略该字段）。
        # 注意：这里做浅拷贝，避免污染调用方持有的 tools 列表。
        cached_tools = None
        if tools:
            cached_tools = list(tools)
            if isinstance(cached_tools[-1], dict):
                cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

        params = {
            "model": self.model_name,
            "messages": messages,
            "tools": cached_tools if cached_tools else None,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "top_p": self.top_p,
            "extra_body": {}
        }
        # 只在显式配置时才传 reasoning_effort，避免对所有模型强制最大思考深度
        if self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        # 单次输出硬上限：防止模型退化后无限输出/无限思考
        if self.max_output_tokens and self.max_output_tokens > 0:
            params["max_tokens"] = self.max_output_tokens

        thinking = self.thinking

        model_l = str(self.model_name or "").lower()
        # 仅对 qwen 系模型注入 DashScope 专属扩展参数（原条件 "qwen" or ... 恒真，会污染所有模型）
        if "qwen" in model_l or "default" in model_l:
            params["extra_body"]["enable_thinking"] = thinking

        # 不同 provider 对「思考强度」的字段名/取值不同，按模型家族覆盖请求参数。
        # config: reasoning_overrides = {"<provider关键字>": {...要覆盖的 params 片段...}}
        # 例：{"o1": {"reasoning_effort": "high"}, "deepseek": {"extra_body": {"thinking": {"type": "enabled"}}}}
        try:
            overrides = self.config.get("reasoning_overrides") or {}
            for key, patch in overrides.items():
                if key and key.lower() in model_l and isinstance(patch, dict):
                    for pk, pv in patch.items():
                        if pk == "extra_body" and isinstance(pv, dict):
                            params.setdefault("extra_body", {}).update(pv)
                        else:
                            params[pk] = pv
                    break
        except Exception as _ro_err:
            logger.debug(f"reasoning_overrides 应用失败: {_ro_err!s}")

        # 原实现把 enable_search 绑死在 qwen 系上，导致其它模型（doubao/gpt 等）
        # 即使前端开关打开也静默失效。联网搜索已改为独立的 WebSearch 工具
        # （见 chat_client/tools/websearch.py），与模型无关，此处不再注入该扩展参数。
        
        if "doubao" in str(self.model_name).lower():
            enable_type = "enabled" if thinking else "disabled"
            params['extra_body']["thinking"] = {
                "type": enable_type
            }
            
        # 协议分流：responses 走 /v1/responses；anthropic 走 /v1/messages。
        # 两个适配层都把事件翻成 chat.delta 形状，主循环零改动。
        if getattr(self, "api_protocol", "chat") == "responses":
            from chat_client.responses_client import build_request, iter_chat_chunks

            payload = build_request(
                messages,
                tools,
                self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
                reasoning_effort=self.reasoning_effort or None,
                max_output_tokens=self.max_output_tokens or None,
            )
            return iter_chat_chunks(self.client, payload)

        if getattr(self, "api_protocol", "chat") == "anthropic":
            from chat_client.anthropic_client import (
                build_request as anthropic_build_request,
                iter_anthropic_chunks,
                stream_messages,
            )

            payload = anthropic_build_request(
                messages,
                tools,
                self.model_name,
                temperature=self.temperature,
                top_p=self.top_p,
                reasoning_effort=self.reasoning_effort or None,
                max_output_tokens=self.max_output_tokens or None,
            )
            return iter_anthropic_chunks(stream_messages(
                self.base_url,
                self.api_key,
                payload,
                default_headers=self.default_headers,
                timeout=max(30, self.response_timeout_seconds or 120),
            ))

        return self.client.chat.completions.create(**params)

    def chat(self, user_input: str | list[dict[str, Any]]) -> Generator[dict[str, Any], None, None]:
        """
        主聊天循环，支持流式响应。
        """
        # 成员代理（非严格模式）：执行期放行其自带高风险工具（招聘即授权）
        set_session_allow_high(self.session_id, not self.strict_tools)
        try:
            # 生成 ID
            user_msg_id = str(uuid.uuid4())
            ai_msg_id = str(uuid.uuid4())

            yield {
                "type": "meta_info",
                "user_msg_id": user_msg_id,
                "ai_msg_id": ai_msg_id
            }
            
            # 处理文件引用
            user_input = self._process_user_input_files(user_input)
            
            # 1. 更新记忆 (用户)
            user_msg = self.memory.add_message("user", user_input, id=user_msg_id)
            
            # 2. 检索上下文 (RAG)
            context_str = ""
            
            # 提取纯文本用于检索
            user_text = user_input
            if isinstance(user_input, list):
                # logger.info(user_input)
                text_parts = []
                for item in user_input:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                user_text = "\n".join(text_parts)
                
            # 全局上下文检索（ExternalRAGService 内部会判断是否需要检索）
            # 获取最近的对话历史用于 RAG 判断
            recent_history = self.memory.get_sliding_window()
            if self.use_global_rag:
                global_docs = self.global_rag.search(
                    user_text,
                    scope="global",
                    session_history=recent_history,
                    # enable_rag_judgment=False # 关闭模型检索校验(先让小模型来判断是否需要进行rag),
                )
                if global_docs:
                    context_str += "[Global Knowledge Base(for reference only)]:\n" + "\n".join(global_docs) + "\n\n"
                logger.info("[RAG] Global context injected, docs count: %d", len(global_docs))

            # 文档知识库检索（用户自己入库的文档/资料，带来源引用）
            # 与上面 mem0 记忆的区别：这里是长期沉淀的文档，不是对话记忆
            if getattr(self, "use_document_kb", False) and user_text.strip():
                kb_hits = self.search_document_kb(user_text, top_k=self.config.get("document_kb_top_k", 5))
                if kb_hits:
                    kb_lines = [f"{h['citation']}\n{h['text']}" for h in kb_hits]
                    context_str += ("[Document Knowledge Base(for reference only)]:\n"
                                    + "\n\n".join(kb_lines) + "\n\n")
                    logger.info("[KB] Document context injected, hits count: %d", len(kb_hits))

            # 检查触发条件
            if self.memory.get_total_rounds() > self.rag_trigger_threshold:
                
                # 排除当前滑动窗口中的 ID 以避免重复
                sliding_window = self.memory.get_sliding_window()
                exclude_ids = [m["id"] for m in sliding_window]
                
                # 传递 session_id 进行隔离。
                retrieved_docs = self.rag.search(
                    user_text, 
                    session_id=self.session_id, 
                    scope="session", 
                    exclude_ids=exclude_ids
                )
                if retrieved_docs:
                    if context_str:
                         context_str += "[Session History(for reference only)]:\n"
                    logger.info("[RAG] Session context injected, docs count: %d", len(retrieved_docs))
                    context_str += "\n".join(retrieved_docs)

            # 3. 构建消息
            self._pending_summary = None
            messages = self._build_messages(context_str)

            # 如果有压缩摘要，先推送给前端
            if self._pending_summary:
                yield self._pending_summary
                self._pending_summary = None
            
            # 工具配置：首次迭代只用用户指定工具（避免 GLM 因工具过多混淆）
            def _build_tools(enabled_ids):
                t = registry.get_openai_tools(enabled_ids=enabled_ids)
                # 仅当全量模式（enabled_ids=None）时才注入 MCP 工具
                if self._mcp_loaded and enabled_ids is None:
                    from chat_client.mcp_client import get_mcp_client
                    mcp_client = get_mcp_client()
                    t = [x for x in t if registry._metadata.get(x["function"]["name"], {}).get("category") != "mcp"]
                    mcp_tools = mcp_client.get_tool_schemas()
                    if mcp_tools:
                        t.extend(mcp_tools)
                    # 过滤+合并后仍需去重：内置与 MCP 同名工具可能在过滤后再次共存，
                    # 上游不接受同名顶层工具（400 duplicate tool name → 503）。
                    t = registry.dedup_tools(t)
                return t

            # 构建工具列表：使用 enabled_tools（已排除集团工具）而非 _build_tools(None)（返回全部工具含 RunCrew）
            # strict_tools 模式（集团经理）：按 original_tools 过滤，未指定则用全量
            # 非 strict 模式（单 Agent / Codex-X）：用 enabled_tools（已排除 _GROUP_TOOLS）
            if self.strict_tools:
                all_tools = _build_tools(self.original_tools) if self.original_tools else _build_tools(None)
            else:
                all_tools = _build_tools(self.enabled_tools) if self.enabled_tools else _build_tools(None)
            tools = all_tools
            first_tools = all_tools
            
            # 循环限制，防止无限递归
            iteration_count = 0
            full_response_content = "" # 最终累积响应
            full_reasoning_content = "" # 最终累积思考
            tool_call_chunks = {} # Initialize to ensure scope availability
            
            total_usage = {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0
            }
            last_message_id = ""
            while iteration_count < self.max_tool_iterations:
                iteration_count += 1

                # 停止检查：工具执行完毕后不再发起新一轮模型请求
                if _is_cancelled():
                    logger.info("[Cancel] 任务已停止，结束主循环")
                    return

                # 后台任务结果回灌：工具转后台后，完成通知只推 SSE（前端可见），
                # 不会进入 messages，模型因此永远拿不到结果、只能靠轮询 TaskStatus。
                # 这里每轮迭代取一次"已完成未注入"的结果，追加进上下文，模型下一轮即可读到。
                # 追加到 messages（本轮内存态）：跨迭代可见；不写 memory，避免污染长期历史。
                try:
                    _bg = get_background_manager()
                    _finished = _bg.drain_finished(self.session_id)
                    for _t in _finished:
                        _text = (
                            f"<background_task_done>\n"
                            f"<task_id>{_t['task_id']}</task_id>\n"
                            f"<tool>{_t['tool']}</tool>\n"
                            f"<status>{_t['status']}</status>\n"
                            f"<duration_s>{_t.get('duration_s', '')}</duration_s>\n"
                            f"<result>\n{_t['result']}\n</result>\n"
                            f"</background_task_done>\n"
                            f"以上是你之前转入后台的工具任务的执行结果，现已完成，"
                            f"请据此继续推进任务，不要重复调用该工具。"
                        )
                        messages.append({"role": "system", "content": _text})
                        logger.info("[Background] 注入后台结果 task=%s tool=%s", _t['task_id'], _t['tool'])
                except Exception:
                    logger.warning("[Background] 后台结果注入失败", exc_info=True)

                # Copy messages to avoid polluting history with ephemeral warnings
                request_messages = list(messages)

                if iteration_count > 1:
                    remaining = self.max_tool_iterations - iteration_count + 1
                    iter_msg = f"\n<system-reminder>Action Count: {iteration_count}/{self.max_tool_iterations}. "
                    if remaining <= 2:
                        iter_msg += "WARNING: You are approaching the tool execution limit. If the task is not finished, STOP NOW and ask the user to continue in the next turn to reset the counter. Do NOT try to rush."
                    else:
                        iter_msg += "Proceed efficiently."
                    iter_msg += "</system-reminder>"
                    
                    # Append as a temporary system message for this request only
                    request_messages.append({"role": "system", "content": iter_msg})
                
                # 首次迭代（model 第一次回复）只传递用户指定工具；后续迭代使用全量工具
                iter_tools = first_tools if iteration_count == 1 else tools

                # ---- 带自动重试的流式请求：断流 / 429 / 5xx 后重新请求 ----
                # OpenAI SDK 的 max_retries 只覆盖建连阶段，不覆盖流式中途断开；
                # 这里在应用层重试：每次尝试独立，无可见输出时完全重新请求（不携带任何已输出正文）；
                # 已有输出时断流直接结束本轮，不续写。
                api_max_retry = max(0, int(self.config.get("api_max_retry", 3)))
                api_retry_base_wait = float(self.config.get("api_retry_base_wait", 1))
                api_retry_max_wait = float(self.config.get("api_retry_max_wait", 10))

                attempt = 0
                yielded_content = ""  # 本轮所有尝试中已实时推送给前端、不可撤回的正文
                # reasoning 退化重试独立计数：只统计因重复退化触发的重试，
                # 不被网络异常/空回复的 attempt 消耗。
                reasoning_repeat_retry_count = 0
                while True:
                    attempt += 1
                    tool_call_chunks = {}
                    current_response_content = ""
                    current_reasoning_content = ""
                    # 已补发给前端的 reasoning 长度；reasoning 先缓冲，
                    # 在正文/工具调用开始前或流结束时校验通过后一次性补发。
                    reasoning_yielded_len = 0
                    reasoning_checked_len = 0
                    reported_tool_indices = set()
                    try:
                        # 防御：清理可能含非法 JSON 的 tool_calls
                        request_messages = self._sanitize_messages_for_api(request_messages)
                        _resp_started = time.monotonic()
                        response_stream = self._create_completion_stream(request_messages, iter_tools)

                        for chunk in response_stream:

                            # 停止检查：chat_stop 置位后立即断开，不再消费流
                            if _is_cancelled():
                                _close_stream(response_stream)
                                return

                            # 单次响应墙钟上限：防止模型"边想边流"永远不结束
                            if (self.response_timeout_seconds > 0
                                    and (time.monotonic() - _resp_started) > self.response_timeout_seconds):
                                _close_stream(response_stream)
                                logger.error(
                                    "[AI-Timeout] 单次响应超过 %.0fs，强制结束本轮（已输出 %d 字符）",
                                    self.response_timeout_seconds, len(current_response_content),
                                )
                                if current_response_content:
                                    try:
                                        self.memory.add_message(
                                            "assistant", current_response_content, id=ai_msg_id
                                        )
                                    except Exception:
                                        logger.warning("[AI-Timeout] 保存部分输出失败", exc_info=True)
                                yield {
                                    "type": "warning",
                                    "data": (
                                        f"模型单次响应超过 {int(self.response_timeout_seconds)} 秒，"
                                        f"已强制结束本轮以避免无限思考/输出。"
                                    ),
                                }
                                return

                            # 结束判断
                            if not chunk.choices:
                                if chunk.usage:
                                     total_usage["total_tokens"] += chunk.usage.total_tokens
                                     total_usage["input_tokens"] += chunk.usage.prompt_tokens
                                     total_usage["output_tokens"] += chunk.usage.completion_tokens
                                     last_message_id = chunk.id
                                continue

                            delta = chunk.choices[0].delta

                            # 处理推理内容：先缓冲，不实时推送。每增长约 2000 字符
                            # 做一次退化检测，命中后立即断流并重新请求，避免等整段
                            # 退化思考生成完；校验通过的内容在正文之前补发，保证
                            # 思维链顺序，也避免把大段重复思考刷给前端。
                            if getattr(delta, "reasoning_content", None):
                                current_reasoning_content += delta.reasoning_content
                                if (len(current_reasoning_content) - reasoning_checked_len
                                        >= 2000):
                                    reasoning_checked_len = len(current_reasoning_content)
                                    if self._reasoning_has_repeat(
                                            current_reasoning_content, self.reasoning_repeat_limit):
                                        _close_stream(response_stream)
                                        break

                            # 处理正文内容
                            if delta.content:
                                # 正文开始前补发已校验的思考内容（思考在前、正文在后）
                                if len(current_reasoning_content) > reasoning_yielded_len:
                                    if self._reasoning_has_repeat(
                                            current_reasoning_content, self.reasoning_repeat_limit):
                                        _close_stream(response_stream)
                                        break
                                    yield {
                                        "type": "reasoning",
                                        "response": current_reasoning_content[reasoning_yielded_len:]
                                    }
                                    reasoning_yielded_len = len(current_reasoning_content)
                                current_response_content += delta.content
                                yielded_content += delta.content
                                yield {
                                    "type": "content",
                                    "response": delta.content
                                }

                            # 处理工具调用
                            if delta.tool_calls:
                                # 工具调用占位事件之前同样先补发思考内容
                                if len(current_reasoning_content) > reasoning_yielded_len:
                                    if self._reasoning_has_repeat(
                                            current_reasoning_content, self.reasoning_repeat_limit):
                                        _close_stream(response_stream)
                                        break
                                    yield {
                                        "type": "reasoning",
                                        "response": current_reasoning_content[reasoning_yielded_len:]
                                    }
                                    reasoning_yielded_len = len(current_reasoning_content)
                                for tc in delta.tool_calls:
                                    index = tc.index
                                    if index not in tool_call_chunks:
                                        tool_call_chunks[index] = {
                                            "id": tc.id,
                                            "function": {"name": "", "arguments": ""}
                                        }

                                    if tc.id:
                                        tool_call_chunks[index]["id"] = tc.id
                                    if tc.function.name:
                                        tool_call_chunks[index]["function"]["name"] += tc.function.name
                                    if tc.function.arguments:
                                        tool_call_chunks[index]["function"]["arguments"] += tc.function.arguments

                                # 尽早告知前端"即将调用工具"（占位事件，args 为空；
                                # 前端按 id 替换语义，不会与最终真实参数重复拼接）
                                idx0 = tc.index
                                _name = tool_call_chunks[idx0]["function"]["name"]
                                if (idx0 not in reported_tool_indices and _name
                                        and tool_call_chunks[idx0]["id"]):
                                    reported_tool_indices.add(idx0)
                                    yield {
                                        "type": "tool_call",
                                        "tool": _name,
                                        "args": "",
                                        "id": tool_call_chunks[idx0]["id"],
                                    }

                        # 本轮成功结束：每次尝试结果独立，不再拼接/续写已推送内容
                        # （断流有输出 → 直接结束本轮；无输出 → 完全重新请求，不携带任何正文）

                        # reasoning 退化重复检测：同一句重复过多 → 丢弃本次响应并重新请求。
                        # 命中时不结束任务，直接发起下一次尝试（不携带本次思考/正文）。
                        if self._reasoning_has_repeat(current_reasoning_content, self.reasoning_repeat_limit):
                            if reasoning_repeat_retry_count < self.reasoning_repeat_retries:
                                reasoning_repeat_retry_count += 1
                                logger.warning(
                                    "[AI-Retry] reasoning 重复退化（第 %d/%d 次重试，句子重复超过 %d 次），"
                                    "丢弃本次响应并重新请求: %d chars",
                                    reasoning_repeat_retry_count, self.reasoning_repeat_retries,
                                    self.reasoning_repeat_limit, len(current_reasoning_content),
                                )
                                # 清空本次已累积的工具调用，避免残留导致下一轮误判
                                tool_call_chunks = {}
                                reported_tool_indices = set()
                                yield {
                                    "type": "stream_reset",
                                    "data": "reasoning_repeat",
                                }
                                if _sleep_interruptible(api_retry_base_wait):
                                    return
                                continue
                            logger.warning(
                                "[AI-Retry] reasoning 重复退化超过重试上限（已重试 %d 次），"
                                "丢弃退化思考并结束本轮，避免污染历史: %d chars",
                                reasoning_repeat_retry_count, len(current_reasoning_content),
                            )
                            # 退化思考不写入历史，也不继续执行，直接结束本轮并提示用户
                            yield {
                                "type": "warning",
                                "data": "检测到模型思维链持续重复退化，已停止本轮以避免无限循环。请重试或换一个更简单的指令。",
                            }
                            return

                        # 空回复检测：API 返回 200 但无内容且无工具调用 → 重试
                        if not current_response_content and not tool_call_chunks:
                            if attempt <= api_max_retry:
                                logger.warning(
                                    f"[AI-Retry] 空回复（第 {attempt}/{api_max_retry + 1} 次），{api_retry_base_wait}s 后重试..."
                                )
                                if _sleep_interruptible(api_retry_base_wait):
                                    return  # 等待期间被 chat_stop 请求停止
                                continue  # 重试本轮
                            else:
                                logger.warning("[AI-Retry] 多次空回复，放弃重试")
                                break

                        # 尾部残余思考（纯思考无正文/工具的轮次已在上面按空回复处理）
                        if (len(current_reasoning_content) > reasoning_yielded_len):
                            yield {
                                "type": "reasoning",
                                "response": current_reasoning_content[reasoning_yielded_len:]
                            }
                            reasoning_yielded_len = len(current_reasoning_content)

                        break

                    except Exception as api_err:
                        # 取消优先：停止请求导致的连接中断不触发重试，立即退出
                        if _is_cancelled():
                            logger.info("[AI-Retry] 任务已停止，跳过重试")
                            return
                        # Responses 协议网关的确定性 400（reasoning_content + tools 组合
                        # 无法反序列化）：原样重试必然再 400，剥离后立即重试并全进程记忆
                        if (self._is_response_input_400(api_err)
                                and not Agent._upstream_rejects_reasoning):
                            Agent._upstream_rejects_reasoning = True
                            logger.warning(
                                "[AI-Retry] 上游拒绝 reasoning_content（Responses 协议 400），"
                                "已全进程剥离，本次剥离后立即重试"
                            )
                            request_messages = [
                                {k: v for k, v in m.items() if k != "reasoning_content"}
                                for m in request_messages
                            ]
                            continue
                        if not is_retryable_api_err(api_err) or attempt > api_max_retry:
                            raise
                        wait_s = min(api_retry_base_wait * (2 ** (attempt - 1)), api_retry_max_wait)
                        logger.warning(
                            f"[AI-Retry] 接口流异常 {type(api_err).__name__}，{wait_s}s 后第 {attempt}/{api_max_retry} 次重试: {str(api_err)[:200]}"
                        )
                        if _sleep_interruptible(wait_s):
                            logger.info("[AI-Retry] 等待重试期间任务被停止")
                            return
                        # 断流/异常一律重新请求：不续写、不结束本轮，直到成功或达重试上限。
                        # 重新请求不携带任何已输出正文，因此不会重复，也不丢结果。
                        continue
                # 如果这一轮有内容，累加到最终响应
                if current_response_content:
                    full_response_content = current_response_content
                if current_reasoning_content:
                    full_reasoning_content = current_reasoning_content

                # 重复内容熔断：有工具调用的轮次里连续输出完全相同的内容 → 幻觉死循环
                _content_sig = (current_response_content or "").strip()
                if tool_call_chunks and _content_sig and len(_content_sig) >= 80:
                    if _content_sig == self._repeat_content:
                        self._repeat_content_streak += 1
                    else:
                        self._repeat_content = _content_sig
                        self._repeat_content_streak = 1
                    if self._repeat_content_streak >= self.repeat_limit:
                        logger.error(
                            "[AI-Loop] 连续 %d 轮输出完全相同内容，熔断结束本轮",
                            self._repeat_content_streak,
                        )
                        yield {
                            "type": "warning",
                            "data": "检测到模型连续多轮重复相同内容，已停止以避免无限循环。",
                        }
                        return
                else:
                    self._repeat_content = None
                    self._repeat_content_streak = 0

                # 兜底解析（2026-10-18 恢复）：模型未走标准 function_call，而是把工具
                # 调用写成 <invoke>/<skill name> 文本（Claude Code 风格）时，解析为真实
                # 工具调用执行，避免"只回文本不调工具"导致任务卡死。同时剥离正文中的
                # 伪调用块，防止历史污染。
                if not tool_call_chunks and current_response_content:
                    try:
                        _invoke_tcs = self._parse_invoke_tool_calls(current_response_content)
                    except Exception:
                        _invoke_tcs = []
                    if _invoke_tcs:
                        logger.warning(
                            "[ToolInvoke] 检测到 %d 个 <invoke>/<skill> 文本工具调用，已转标准工具执行: %s",
                            len(_invoke_tcs), [t[0] for t in _invoke_tcs],
                        )
                        current_response_content = self._strip_invoke_blocks(current_response_content)
                        full_response_content = current_response_content
                        tool_call_chunks = {}
                        for _i, (_nm, _args) in enumerate(_invoke_tcs):
                            tool_call_chunks[_i] = {
                                "id": f"invoke_{_i}",
                                "function": {
                                    "name": _nm,
                                    "arguments": json.dumps(_args, ensure_ascii=False),
                                },
                            }

                # 如果没有工具调用，结束循环
                if not tool_call_chunks:
                    # 结束前等后台任务：工具转后台后结果靠主循环 drain_finished 注入
                    # 上下文。旧逻辑用 _bg_wait_used + background_wait_seconds=0 做轮询，
                    # 后台任务未跑完时主循环就 yield stop 终止会话，导致结果只推 SSE、
                    # 永远进不了对话正文（子代理/普通工具超时转后台后尤为明显）。
                    # 新逻辑：pending>0 时不终止，挂起等后台完成回调 notify_done 唤醒
                    # （set wake event），唤醒后下一轮 drain_finished 自然注入结果，
                    # 模型据此收尾。chat_stop 会置 _is_cancelled，唤醒后直接退出，
                    # 此时结果已落盘可查、不丢。
                    try:
                        _bg = get_background_manager()
                        if _bg.pending_count(self.session_id) > 0:
                            # 等待上限：0/负 = 不设硬上限，仅由 cancel 中断（前端解耦、
                            # 挂起不占资源、等不影响）。可配置 background_wait_seconds 兜底。
                            _wait_budget = float(self.config.get("background_wait_seconds", 0))
                            _wake = _bg.get_wake_event(self.session_id)
                            _waited = 0.0
                            _poll_step = 30.0
                            while (_bg.pending_count(self.session_id) > 0
                                   and not _is_cancelled()):
                                if _wait_budget > 0 and _waited >= _wait_budget:
                                    break
                                # 挂起等唤醒；超时兜底轮询，避免 wake event 丢失导致死等
                                _wake.wait(timeout=_poll_step)
                                _waited += _poll_step
                                # 唤醒即可能已有完成结果，立刻注入让模型下一轮读到
                                for _t in _bg.drain_finished(self.session_id):
                                    _text = (
                                        f"<background_task_done>\n"
                                        f"<task_id>{_t['task_id']}</task_id>\n"
                                        f"<tool>{_t['tool']}</tool>\n"
                                        f"<status>{_t['status']}</status>\n"
                                        f"<duration_s>{_t.get('duration_s', '')}</duration_s>\n"
                                        f"<result>\n{_t['result']}\n</result>\n"
                                        f"</background_task_done>\n"
                                        f"以上是你之前转入后台的工具任务的执行结果，现已完成，"
                                        f"请据此继续推进任务，不要重复调用该工具。"
                                    )
                                    messages.append({"role": "system", "content": _text})
                                    logger.info("[Background] 挂起唤醒后注入结果 task=%s", _t['task_id'])
                            # 唤醒后 drain 一次（防 wake 先于 drain 竞态）
                            for _t in _bg.drain_finished(self.session_id):
                                _text = (
                                    f"<background_task_done>\n"
                                    f"<task_id>{_t['task_id']}</task_id>\n"
                                    f"<tool>{_t['tool']}</tool>\n"
                                    f"<status>{_t['status']}</status>\n"
                                    f"<duration_s>{_t.get('duration_s', '')}</duration_s>\n"
                                    f"<result>\n{_t['result']}\n</result>\n"
                                    f"</background_task_done>\n"
                                    f"以上是你之前转入后台的工具任务的执行结果，现已完成，"
                                    f"请据此继续推进任务，不要重复调用该工具。"
                                )
                                messages.append({"role": "system", "content": _text})
                                logger.info("[Background] 收尾前注入结果 task=%s", _t['task_id'])
                            # 还有未完成的（被 cancel 或超出等待上限）：如实告知，
                            # 让模型带"未完成"信息再走一轮，而非假装完成直接结束
                            if _bg.pending_count(self.session_id) > 0:
                                _n_pending = _bg.pending_count(self.session_id)
                                if _wait_budget > 0 and _waited >= _wait_budget:
                                    _pending_detail = (
                                        f"仍有 {_n_pending} 个后台任务在运行且已超过等待上限"
                                        f"（{_wait_budget:g} 秒）。"
                                    )
                                else:
                                    _pending_detail = (
                                        f"仍有 {_n_pending} 个后台任务在后台运行。"
                                        f"完成后结果会自动回灌到对话上下文。"
                                    )
                                messages.append({
                                    "role": "system",
                                    "content": (
                                        f"<background_task_pending>\n"
                                        f"{_pending_detail}\n"
                                        f"请如实告知用户任务仍在进行、稍后会自动返回结果，"
                                        f"不要声称任务已完成，也不要重复发起同一工具调用。\n"
                                        f"</background_task_pending>"
                                    ),
                                })
                                continue
                    except Exception:
                        logger.warning("[Background] 等待后台任务失败", exc_info=True)

                    # 用量统计：把本轮 token 用量追加到 usage ledger（失败静默）
                    try:
                        from platform_ext.usage import record_usage  # noqa: PLC0415
                        record_usage(
                            session_id=self.session_id or "",
                            model=getattr(self, "model", "") or "",
                            input_tokens=int(total_usage.get("input_tokens") or 0),
                            output_tokens=int(total_usage.get("output_tokens") or 0),
                            total_tokens=int(total_usage.get("total_tokens") or 0),
                            source="chat",
                        )
                    except Exception:  # noqa: BLE001
                        pass

                    yield {
                        "type": "stop",
                        "usage": total_usage,
                        "message_id": last_message_id
                    }
                    break

                # 处理工具调用逻辑
                assistant_msg_kwargs = {"tool_calls": []}
                for idx in sorted(tool_call_chunks.keys()):
                    tc = tool_call_chunks[idx]
                    assistant_msg_kwargs["tool_calls"].append({
                        "id": tc["id"],
                        "type": "function",
                        "function": tc["function"]
                    })
                
                # 重复工具调用熔断：连续多轮发起完全相同的调用 → 幻觉死循环
                _tool_sig = tuple(sorted(
                    (tc["function"].get("name", ""), tc["function"].get("arguments", ""))
                    for tc in assistant_msg_kwargs["tool_calls"]
                ))
                _tool_names = {tc["function"].get("name", "") for tc in assistant_msg_kwargs["tool_calls"]}
                if _tool_names & self._repeat_exempt_tools:
                    _tool_sig = ()
                if _tool_sig and _tool_sig == self._repeat_tool_sig:
                    self._repeat_tool_streak += 1
                else:
                    self._repeat_tool_sig = _tool_sig or None
                    self._repeat_tool_streak = 1 if _tool_sig else 0
                if _tool_sig and self._repeat_tool_streak >= self.repeat_limit:
                    logger.error(
                        "[AI-Loop] 连续 %d 轮完全相同的工具调用，熔断结束本轮: %s",
                        self._repeat_tool_streak,
                        ", ".join(tc["function"].get("name", "") for tc in assistant_msg_kwargs["tool_calls"]),
                    )
                    yield {
                        "type": "warning",
                        "data": "检测到模型连续多轮发起完全相同的工具调用，已停止以避免无限循环。",
                    }
                    return

                # 保存助手工具调用消息
                if current_reasoning_content:
                    assistant_msg_kwargs["reasoning_content"] = current_reasoning_content

                # 源头防污染（2026-10-18 恢复）：保存历史前剥离 <invoke>/<skill> 伪工具
                # 调用文本。否则历史里残留伪调用块，后续请求会持续诱导模型模仿。
                _saved_content = Agent._strip_invoke_blocks(current_response_content)
                self.memory.add_message("assistant", _saved_content, id=ai_msg_id, **assistant_msg_kwargs)

                messages.append({
                    "role": "assistant",
                    "content": current_response_content,
                    "tool_calls": assistant_msg_kwargs["tool_calls"]
                })
                # 关键修改：确保 reasoning_content 也被带到消息列表中
                if "reasoning_content" in assistant_msg_kwargs:
                    messages[-1]["reasoning_content"] = assistant_msg_kwargs["reasoning_content"]

                # 执行工具
                # ── 工具执行：预处理(顺序) → 执行(并行) → 回填(顺序) ──
                # 2026-10-15 修复（boss 反馈）：此前是串行 for 循环，同一条消息里的多个
                # 工具调用（尤其多个 Task 子代理）被逐个执行，前一个卡满前台等待上限
                # 才轮到下一个，与 Task 工具 docstring 承诺的「并发启动多个 agent」不符。
                # 现改为并行执行同一轮的所有工具调用（config: tool_parallel_enabled，
                # 默认 True），yield 事件顺序与落库顺序保持不变。
                _tool_calls = assistant_msg_kwargs["tool_calls"]
                _plans: list[dict[str, Any]] = []
                _abort = False

                for tc in _tool_calls:
                    # 停止检查：已请求停止则不再执行后续工具（含 Bash 等高危工具）
                    if _is_cancelled():
                        self._mark_unexecuted_tools(_tool_calls, messages, ai_msg_id)
                        return
                    func_name = tc["function"]["name"]
                    args_str = tc["function"]["arguments"]
                    call_id = tc["id"]

                    tool_exists = registry.tool_exists(func_name)
                    tool_enabled = registry.is_tool_enabled(func_name, self.enabled_tools) if tool_exists else False

                    # 处理不存在的工具
                    if not tool_exists:
                        _plans.append({
                            "call_id": call_id, "func_name": func_name,
                            "yield_call": False, "yield_result": False, "write_memory": True,
                            "immediate": inject_tool_name(
                                _xml_response("error", f"Error: Tool '{func_name}' does not exist."),
                                func_name),
                        })
                        continue

                    # 处理未启用的工具
                    if not tool_enabled:
                        tool_id = registry.get_tool_id(func_name)
                        _plans.append({
                            "call_id": call_id, "func_name": func_name,
                            "yield_call": False, "yield_result": False, "write_memory": True,
                            "immediate": inject_tool_name(
                                _xml_response("error", f"Error: Tool '{func_name}' (ID: {tool_id}) is not enabled. You do not have permission to use this tool."),
                                func_name),
                        })
                        continue

                    # 通过存在/启用校验后，先 yield tool_call（与原行为一致：在参数解析前）
                    yield {
                        "type": "tool_call",
                        "tool": func_name,
                        "args": args_str,
                        "id": call_id
                    }

                    # 参数解析
                    try:
                        if args_str and args_str.strip():
                            args = json.loads(args_str)
                            self._bad_args_streak = 0  # 解析成功即重置熔断计数
                        else:
                            args = {}
                            self._bad_args_streak = 0
                    except json.JSONDecodeError:
                        # 非法 JSON 回灌明确错误 + 连续多次熔断（逼模型改分段写入）
                        self._bad_args_streak = self.next_bad_args_streak(self._bad_args_streak)
                        logger.warning(
                            "[Tool-Args] 工具 %s 的参数不是合法 JSON（连续第 %d 次）: %r",
                            func_name, self._bad_args_streak, (args_str or "")[:200]
                        )
                        _plans.append({
                            "call_id": call_id, "func_name": func_name, "args_str": args_str,
                            "yield_call": True, "yield_result": False, "write_memory": True,
                            "immediate": bad_args_hint(func_name),
                        })
                        if self._bad_args_streak >= self.BAD_ARGS_MAX_STREAK:
                            logger.error("[Tool-Args] 连续 %d 次非法参数，熔断结束本轮", self._bad_args_streak)
                            _abort = True
                            break
                        continue

                    # 类型矫正：按 schema 把字符串化的数字/布尔参数转回真实类型
                    args = self._coerce_tool_args(func_name, args)

                    # 参数注入（config/session/cwd）与卡密安全校验
                    try:
                        if func_name in ("Task", "RunCrew", "ConsultPeer"):
                            agent_config = self.config.copy()
                            agent_config.pop("system_prompt", None)
                            agent_config.pop("tools", None)
                            args["parent_config"] = agent_config
                            args["parent_session_id"] = self.session_id

                        if func_name in ["TodoWrite", "TodoRead", "TaskSummary"]:
                            args["session_id"] = self.session_id
                            args["sessions_dir"] = self.config.get("sessions_dir", "sessions")

                        # 注意：CheckCommandStatus/StopCommand 不接收 cwd 参数，仅 Bash 需要
                        if func_name == "Bash":
                            cwd = self.current_dir if hasattr(self, 'current_dir') and self.current_dir else None
                            if cwd and "cwd" not in args:
                                args["cwd"] = cwd

                        # 【安全】卡密用户：限制 Bash 命令，禁止危险操作
                        if self._is_card_user and func_name == "Bash":
                            cmd = args.get("command", "")
                            dangerous_patterns = ["cat /etc/", "passwd", "/proc/", "chmod 777", "rm -rf /", "sudo", "su "]
                            if any(p in cmd for p in dangerous_patterns) or cmd.startswith("cat /"):
                                _plans.append({
                                    "call_id": call_id, "func_name": func_name, "args_str": args_str,
                                    "yield_call": True, "yield_result": True, "write_memory": False,
                                    "immediate": inject_tool_name(
                                        _xml_response("error", f"安全限制：卡密用户禁止执行此命令（包含敏感路径或高危操作）。"),
                                        func_name),
                                })
                                continue

                        # 【安全】卡密用户：限制 Write 工具路径
                        if self._is_card_user and func_name == "Write":
                            fp = args.get("file_path", "")
                            logger.info(f"[安全] Card user Write: fp={fp}, workspace={self._user_workspace}, starts={fp.startswith(self._user_workspace) if fp else False}")
                            if fp and not fp.startswith(self._user_workspace):
                                _plans.append({
                                    "call_id": call_id, "func_name": func_name, "args_str": args_str,
                                    "yield_call": True, "yield_result": True, "write_memory": False,
                                    "immediate": inject_tool_name(
                                        _xml_response("error", f"安全限制：卡密用户只能在隔离工作目录内操作文件（{_user_workspace}）"),
                                        func_name),
                                })
                                continue
                    except Exception as e:
                        _plans.append({
                            "call_id": call_id, "func_name": func_name, "args_str": args_str,
                            "yield_call": True, "yield_result": True, "write_memory": True,
                            "immediate": inject_tool_name(
                                _xml_response("error", f"Error preparing tool: {e!s}"), func_name),
                        })
                        continue

                    func = registry.get_tool_func(func_name)
                    if func:
                        _plans.append({
                            "call_id": call_id, "func_name": func_name, "args_str": args_str,
                            "yield_call": True, "yield_result": True, "write_memory": True,
                            "func": func, "args": args,
                            "_res_key": self._tool_resource_key(func_name, args),
                        })
                    else:
                        _plans.append({
                            "call_id": call_id, "func_name": func_name, "args_str": args_str,
                            "yield_call": True, "yield_result": True, "write_memory": True,
                            "immediate": inject_tool_name(
                                _xml_response("error", f"Error: Tool {func_name} not found."), func_name),
                        })

                # 阶段2：执行待执行工具（按资源冲突分组并行，2026-10-15）
                # 对齐 Claude Code / Cursor：不按工具名一刀切，而是看资源冲突——
                # 改不同文件的 Edit 可并行，改同一文件的串行；只读/子代理自由并行；
                # Bash/TodoWrite/MCP 等共享全局状态的独占串行。
                _exec_plans = [p for p in _plans if p.get("func") is not None]
                if _exec_plans:
                    _parallel_on = bool(self.config.get("tool_parallel_enabled", True))
                    _ctx_job = _get_current_job()

                    def _run_plan(p):
                        p["result"] = self._run_one_tool(
                            p["func"], p["args"], p["func_name"], _ctx_job)

                    if _parallel_on:
                        # 贪心分组：同组无资源冲突 → 组内并行，组间串行
                        _groups = self._plan_parallel_groups(_exec_plans)
                    else:
                        _groups = [[p] for p in _exec_plans]  # 关闭并行：逐个成组（等价串行）

                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    _mw = int(self.config.get("tool_parallel_max_workers", 8) or 8)
                    _mw = max(1, min(_mw, 16))
                    for _grp in _groups:
                        if len(_grp) == 1:
                            _run_plan(_grp[0])
                        else:
                            with _TPE(max_workers=min(len(_grp), _mw),
                                      thread_name_prefix="tool-par") as _pool:
                                _futs = [(_pool.submit(_run_plan, p), p) for p in _grp]
                                for _f, _p in _futs:
                                    _f.result()

                # 阶段3：按原顺序回填（yield tool_result + P6 归档 + 落库）
                for p in _plans:
                    call_id = p["call_id"]
                    func_name = p["func_name"]
                    result_str = p.get("result")
                    if result_str is None:
                        result_str = p.get("immediate", "") or ""
                    if p["yield_result"]:
                        yield {
                            "type": "tool_result",
                            "tool": func_name,
                            "result": result_str,
                            "id": call_id
                        }
                    if not p["write_memory"]:
                        continue

                    # P6（2026-10-15）：工具结果精简+归档——大结果保留头尾+折叠标记，
                    # 完整内容落盘 tool_archives/，模型可用 ReadToolArchive 按需召回。
                    # 取代旧的 50K 硬截断（截断即丢信息）。阈值可配、可禁用。
                    try:
                        from .tool_archive import archive_result
                        _base = self.config.get("base_dir") or PROJECT_ROOT
                        result_str, _arch_meta = archive_result(
                            result_str,
                            base_dir=_base,
                            session_id=self.session_id,
                            tool_name=func_name,
                            config=self.config,
                        )
                    except Exception as _arch_err:
                        logger.debug(f"工具结果归档失败（按原样返回）: {_arch_err!s}")

                    content_structure = [{
                        "type": "text",
                        "text": self._tool_content_for_model(result_str),
                    }]
                    self.memory.add_message("tool", content_structure, tool_call_id=call_id, id=ai_msg_id)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content_structure
                    })

                # 【单轮内上下文闸门】工具结果回填后检查一次 messages 体积。
                # 轮间压缩只在 _build_messages 触发，而一次请求可能连跑上百个工具调用，
                # 中途 messages 会一路膨胀撞上模型上下文上限。这里对最旧的工具结果
                # 做「归档式精简」（头+尾+折叠标记，完整内容落盘可召回），
                # 最近若干条保持原样，避免模型正在用的结果被削。
                try:
                    _inflight_budget = int(self.context_window_kb * 1024)
                    if _inflight_budget > 0 and self.config.get("inflight_compress", True):
                        self._shrink_inflight_context(messages, _inflight_budget)
                except Exception:
                    logger.warning("[Inflight] 单轮内压缩异常（不影响主流程）", exc_info=True)

                if _abort:
                    yield {
                        "type": "warning",
                        "data": "工具参数连续多次非法（内容可能过长被截断），已停止重试。"
                                "请换用更小的实现或分段写入。"
                    }
                    self._mark_unexecuted_tools(_tool_calls, messages, ai_msg_id)
                    return
            # 6. 最终记忆更新 (助手响应)
            if not tool_call_chunks and full_response_content:
                 kwargs = {}
                 if full_reasoning_content:
                     kwargs["reasoning_content"] = full_reasoning_content
                 
                 # 使用预生成的 ai_msg_id
                 # 源头防污染：保存历史前剥离 <invoke>/<skill> 文本（同上）
                 ai_msg = self.memory.add_message(
                     "assistant",
                     Agent._strip_invoke_blocks(full_response_content),
                     id=ai_msg_id,
                     **kwargs,
                 )
                 
                 # 发送 meta_info 包含 ID
                 yield {
                    "type": "meta_info",
                    "user_msg_id": user_msg_id,
                    "ai_msg_id": ai_msg_id
                 }
                 
                 # 7. 异步向量化
                 t = threading.Thread(
                     target=self.rag.add_memory,
                     args=(user_msg, ai_msg, self.session_id),
                     daemon=True,
                 )
                 t.start()
                
            if iteration_count >= self.max_tool_iterations:
                yield {"type": "warning", "data": "已达到最大工具调用次数限制，建议继续新对话 error_code:max_tool_iterations"}
                 
        except openai.AuthenticationError as e:
            yield {"type": "error", "data": f"API密钥错误或无效，请检查密钥是否正确:{e}", "error_code": 401}
        except openai.RateLimitError as e:
            yield {"type": "error", "data": f"接口调用频率超限，请稍后再试或提升配额:{e}", "error_code": 429}
        except openai.APIConnectionError as e:
            yield {"type": "error", "data": f"无法连接到API服务器（{self.base_url}），请检查网络或地址是否正确:{e}", "error_code": getattr(e, 'status_code', 502)}
        except openai.APIError as e:
            yield {"type": "error", "data": f"API返回错误：{e!s}", "error_code": getattr(e, 'status_code', 500)}
        except Exception as e:
            logger.error(f"Unexpected error in Agent.chat: {traceback.format_exc()}")
            yield {"type": "error", "data": f"调用AI接口时发生未知错误：{e!s}"}
        finally:
            set_session_allow_high(self.session_id, False)
    
    @staticmethod
    def _run_one_tool(func: Callable, args: dict, func_name: str, ctx_job) -> str:
        """在（可能并行的）worker 线程中执行单个工具。

        关键：必须恢复 job 上下文（_restore_job_ctx），否则多会话并发时
        全局 job 引用会串台（工具内部 emit_progress / is_allow_high /
        _current_session_id 读错会话）。func 本身已含黑名单/风险/超时/
        审计守卫（registry 包装的 _guarded_execute），这里只兜底异常。
        """
        from .tools import _restore_job_ctx
        if ctx_job is not None:
            _restore_job_ctx(ctx_job)
        try:
            return func(**args)
        except Exception as e:
            return inject_tool_name(
                _xml_response("error", f"Error executing tool: {e!s}"),
                func_name)

    # ── <invoke>/<skill name> 文本工具调用兜底解析（2026-10-18 恢复）─────────
    # 模型（尤其 GLM/自研系）在长会话中会输出 Claude Code 风格的 <invoke> 文本
    # 工具调用，还会退化成 <skill name="X"> / <parameter name="..."> 形式。若不
    # 解析，模型以为自己在调工具、实际只发了文本 → 任务卡死/中断；同时这类
    # 文本会残留在历史里持续诱导模型模仿，形成污染循环。因此：
    #   1. _strip_invoke_blocks：从发送/保存的历史中剥离伪工具调用块
    #   2. _parse_invoke_tool_calls：把正文中的伪工具调用转成标准 tool_calls 执行
    # 兼容闭合/不闭合/畸形标签前缀（标签名前可穿插特殊 token，如 <｜U2｜parameter>）。
    _TAG_PREFIX = r"(?:\｜[^\s\>]*?)?"
    # 开标签兼容两种实测形态：<invoke name="X"> 与退化形式 <skill name="X">
    # （模型把 <invoke> 误写成 <skill>，后跟同样的 <parameter> 参数块）。
    _INVOKE_OPEN_TAG = r"<(?:invoke|skill)\b[^>]*\bname\s*=\s*\"([^\"]+)\"[^>]*>"
    _INVOKE_RE = re.compile(
        r"<(?:invoke|skill)\b[^>]*>.*?</(?:invoke|skill)" + _TAG_PREFIX + r">",
        re.DOTALL,
    )
    _INVOKE_OPEN_RE = re.compile(
        r'<(?:invoke|skill)\b[^>]*>(?:(?!<(?:invoke|skill)\b).)*',
        re.DOTALL,
    )
    _INVOKE_NAME_RE = re.compile(_INVOKE_OPEN_TAG, re.DOTALL)
    _PARAM_RE = re.compile(
        r'<parameter' + _TAG_PREFIX + r'\b[^>]*\bname\s*=\s*"([^"]+)"[^>]*>(.*?)</parameter' + _TAG_PREFIX + r'>',
        re.DOTALL,
    )
    _PARAM_OPEN_RE = re.compile(
        r'<parameter' + _TAG_PREFIX + r'\b[^>]*\bname\s*=\s*"([^"]+)"[^>]*>(?:(?!</parameter|<parameter\b|<(?:invoke|skill)\b).)*',
        re.DOTALL,
    )

    @staticmethod
    def _strip_invoke_blocks(content: Any) -> Any:
        """从文本/消息 content 中剥离 <invoke>/<skill name> 伪工具调用块。

        只处理字符串 content 与 list[text 块]；其余原样返回。剥离后保留正文，
        避免模型在历史里继续看到/模仿伪工具调用格式。

        兼容不闭合的 <invoke> 块（模型模仿退化漏掉 </invoke>）：闭合块优先，
        未闭合的用 _INVOKE_OPEN_RE 匹配到「下一个 <invoke / 文本结尾」。
        """
        def _strip(text: str) -> str:
            out = Agent._INVOKE_RE.sub("", text)
            out = Agent._INVOKE_OPEN_RE.sub("", out)
            return out

        if isinstance(content, str):
            return _strip(content)
        if isinstance(content, list):
            out = []
            for p in content:
                if isinstance(p, dict):
                    p = dict(p)
                    if p.get("type") in ("text", "output_text", "input_text") and isinstance(p.get("text"), str):
                        p["text"] = _strip(p["text"])
                out.append(p)
            return out
        return content

    @staticmethod
    def _tool_content_for_model(content: Any) -> Any:
        """把 role=tool 的 content 归一化为纯文本（兼容字符串与 text 块列表）。

        工具结果内部用 <tool> XML 封装；回灌给模型时去掉外壳，避免模型把
        工具结果格式误当成工具调用格式。归档/展示不受影响。
        """
        if isinstance(content, str):
            return tool_result_for_model(content)
        if isinstance(content, list):
            out = []
            for p in content:
                if isinstance(p, dict):
                    p = dict(p)
                    if p.get("type") in ("text", "input_text", "output_text") and isinstance(p.get("text"), str):
                        p["text"] = tool_result_for_model(p["text"])
                out.append(p)
            return out
        return content

    @staticmethod
    def _normalize_reasoning_sentence(sentence: str) -> str:
        """归一化句子：去首尾空白/标点、折叠空白、转小写，用于重复检测。"""
        import re as _re
        s = sentence.strip().lower()
        s = _re.sub(r"[。．.!！?？;；,，:：\s]+", " ", s)
        s = _re.sub(r"[\s]+", " ", s)
        return s.strip()

    @classmethod
    def _reasoning_has_repeat(cls, reasoning: str, limit: int) -> bool:
        """检测 reasoning 里是否存在同一句重复过多（退化死循环）。

        按常见句子分隔符切句，归一化后计数；出现同一句 >= limit 次即判定退化。
        为避免长任务中"短退化"（总长不足 2000 但同一句连续复读）漏检，
        额外检测：任意一句归一化后连续出现 >= 4 次也判定退化。
        门槛设在 600 字符，兼顾短退化与误伤平衡。
        """
        if not reasoning or len(reasoning) < 600:
            return False
        import re as _re
        # 同时按中英文句末标点和换行切句；英文句号只在后面跟空白时切，
        # 避免把 chat_client/hotreload.py 这类点号标识符切碎造成误判。
        parts = _re.split(r"(?<=[。！？!?；;])\s*|\n+|(?<=\.)\s+", reasoning)
        counts: dict[str, int] = {}
        # 连续相同计数：专门抓"同一句连续复读"的短退化
        streak = 0
        prev_norm = None
        for part in parts:
            norm = cls._normalize_reasoning_sentence(part)
            if len(norm) < 6:
                continue
            counts[norm] = counts.get(norm, 0) + 1
            if counts[norm] >= limit:
                return True
            # 连续复读检测：相邻两句归一化完全相同
            if prev_norm is not None and norm == prev_norm:
                streak += 1
                if streak >= 3:  # 连续 4 句相同
                    return True
            else:
                streak = 0
            prev_norm = norm
        return False

    @staticmethod
    def _parse_invoke_tool_calls(content: str) -> list[tuple[str, dict[str, Any]]]:
        """解析正文中混入的 <invoke name="X"> / <skill name="X"> 文本工具调用。

        返回 [(工具名, 参数字典), ...]；无伪调用时返回空列表。
        参数值会做 HTML 实体反转义（模型可能输出 &quot; &amp; 等）。

        兼容「不闭合」的 <invoke> 块（模型模仿历史时漏掉 </invoke>）：
        - <parameter name="x">v</parameter> 标准闭合形式
        - <parameter name="x">v 后直接结束/遇到下一个标签（不闭合退化形式）
        """
        import html as _html
        parsed: list[tuple[str, dict[str, Any]]] = []
        seen_ranges: list[tuple[int, int]] = []

        def _parse_block(blk: str) -> None:
            name_m = Agent._INVOKE_NAME_RE.search(blk)
            if not name_m:
                return
            name = _html.unescape(name_m.group(1).strip())
            if not name:
                return
            args: dict[str, Any] = {}
            for pm in Agent._PARAM_RE.finditer(blk):
                pname = _html.unescape(pm.group(1).strip())
                pval = _html.unescape(pm.group(2).strip())
                if pname:
                    args[pname] = pval
            if not args:
                for pm in Agent._PARAM_OPEN_RE.finditer(blk):
                    pname = _html.unescape(pm.group(1).strip())
                    pval = _html.unescape(pm.group(2).strip())
                    if pname:
                        args[pname] = pval
            parsed.append((name, args))

        for m in Agent._INVOKE_RE.finditer(content or ""):
            _parse_block(m.group(0))
            seen_ranges.append(m.span())

        for m in Agent._INVOKE_OPEN_RE.finditer(content or ""):
            start, end = m.span()
            if any(s <= start and end <= e for s, e in seen_ranges):
                continue
            if any(start < e and s < end for s, e in seen_ranges):
                continue
            _parse_block(m.group(0))
            seen_ranges.append((start, end))

        return parsed

    def _mark_unexecuted_tools(self, tool_calls: list[dict], messages: list[dict], ai_msg_id: str):
        """任务被停止时，为未执行/未回填结果的 tool_call 补齐"未执行"结果。

        原因：assistant 消息（含全部 tool_calls）在流式结束后已先落库；若此时直接
        退出，会留下"声明 N 个工具、只回 k 个结果"的历史，下一轮请求会被 API
        以 400 拒绝（Missing tool response for tool_call_id）。补齐后历史自洽。
        """
        replied = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        for tc in tool_calls:
            call_id = tc.get("id")
            if not call_id or call_id in replied:
                continue
            content_structure = [{
                "type": "text",
                "text": self._tool_content_for_model(
                    _xml_response("error", "任务被用户停止，该工具未执行。")),
            }]
            self.memory.add_message("tool", content_structure, tool_call_id=call_id, id=ai_msg_id)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": content_structure,
            })


    @staticmethod
    def _sanitize_messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清理消息列表中非法的 tool_calls，防止 API 返回 400 错误。

        处理两类问题：
        1. 非法 JSON（含为空）的 tool call arguments —— 截断/空参数会导致
           "Assistant tool call arguments must be valid JSON" 400 错误。
        2. assistant 已声明 tool_call 但无配对 tool 结果（悬空声明）——
           进程被杀/崩溃/续写中断时，"声明先落库、结果来不及回填"会在历史里
           留下悬空记录，DeepSeek 等严格校验的网关直接以
           "tool calls and tool results do not match" 拒绝请求。
        """
        # 预扫描：收集所有原始 assistant tool_call 的 ID，用于匹配 tool 结果
        all_tc_ids: set[str] = set()
        for m in messages:
            for tc in m.get("tool_calls", []):
                tid = tc.get("id", "")
                if tid:
                    all_tc_ids.add(tid)

        valid_tool_call_ids: set[str] = set()
        sanitized = []

        for msg in messages:
            msg = dict(msg)

            # 发送前剥离历史中混入的 <invoke>/<skill> 伪工具调用块（2026-10-18 恢复），
            # 阻断模型在历史中持续自模仿的污染循环。
            if msg.get("content") is not None:
                msg["content"] = Agent._strip_invoke_blocks(msg["content"])

            if "tool_calls" in msg:
                clean_tcs = []
                for tc in msg["tool_calls"]:
                    fn = (tc.get("function") or {})
                    args_str = (fn.get("arguments") or "").strip()
                    name = (fn.get("name") or "").strip()
                    tc_id = tc.get("id", "")

                    if not name:
                        logger.warning("[Sanitize] 跳过无名称的 tool_call: %s", tc_id)
                        continue

                    if not args_str:
                        # 空参数 → 替换为合法空 JSON，保留 tool_call
                        tc = dict(tc)
                        tc["function"] = dict(fn, arguments="{}")
                        clean_tcs.append(tc)
                        if tc_id:
                            valid_tool_call_ids.add(tc_id)
                        continue

                    try:
                        json.loads(args_str)
                        clean_tcs.append(tc)
                        if tc_id:
                            valid_tool_call_ids.add(tc_id)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "[Sanitize] 丢弃非法 JSON 的 tool_call: name=%s id=%s args=%r",
                            name, tc_id, args_str[:100]
                        )

                if clean_tcs:
                    msg["tool_calls"] = clean_tcs
                else:
                    del msg["tool_calls"]

            # tool 消息：如果对应的 tool_call 已被清理掉，也跳过
            if msg.get("role") == "tool":
                msg["content"] = Agent._tool_content_for_model(msg.get("content"))
                tcid = msg.get("tool_call_id", "")
                if tcid and tcid not in valid_tool_call_ids:
                    if tcid not in all_tc_ids:
                        # 原始数据中就没有对应的 tool_call，跳过
                        continue
                    # 原始数据中有但被清理了（非法 JSON），跳过 tool 结果
                    logger.warning(
                        "[Sanitize] 跳过对应 tool_call 已被清理的 tool 消息: %s", tcid
                    )
                    continue

            sanitized.append(msg)

        # ---- 全局配对修复：保证「assistant 声明 → 紧跟其全部结果」的不变量 ----
        # 旧实现只收集"紧随其后的 tool 块"，一旦序列被打乱就断链：
        #   形态 A：连续两条 assistant 都带 tool_calls（同会话并行 job 交错写历史）
        #   形态 B：tool 结果散落在声明之后很远处（中间隔着别的 assistant/tool）
        # 这两种形态下旧逻辑 30 处违规只修掉 1 处，网关仍返回
        # "tool calls and tool results do not match"(11148)。
        # 改为按 tool_call_id 全局重挂：结果无论身处何处都被搬到其声明之后，
        # 位置不对的 tool 消息直接丢弃（而不是原样保留）。
        result_by_id: dict[str, dict] = {}
        for m in sanitized:
            if m.get("role") != "tool":
                continue
            tid = m.get("tool_call_id", "")
            # 只认仍然存活的声明；重复结果取第一条
            if tid and tid in valid_tool_call_ids and tid not in result_by_id:
                result_by_id[tid] = m

        def _synthesize(tid: str) -> dict:
            return {
                "role": "tool",
                "tool_call_id": tid,
                "content": [{
                    "type": "text",
                    "text": Agent._tool_content_for_model(_xml_response(
                        "error",
                        "工具未执行：该调用在上一次执行中断（进程退出或服务重启），无可用结果。",
                    )),
                }],
            }

        # 声明有结果 vs 无结果，必须在重建（会 pop）之前统计，否则计数不准
        missing = valid_tool_call_ids - set(result_by_id)
        if missing:
            logger.warning(
                "[Sanitize] 补齐 %d 个无结果的 tool_call（工具执行中断残留）: %s",
                len(missing), sorted(missing)[:5],
            )

        repaired: list[dict[str, Any]] = []
        for msg in sanitized:
            if msg.get("role") == "tool":
                # 结果一律由声明处挂载；此处跳过（原地保留会破坏不变量）
                continue
            repaired.append(msg)
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tid = tc.get("id")
                    if not tid:
                        continue
                    repaired.append(result_by_id.pop(tid, None) or _synthesize(tid))

        if result_by_id:
            # 声明已被清理（如非法 JSON）但结果还在 → 结果无归属，必须丢弃
            logger.warning(
                "[Sanitize] 丢弃 %d 个无法归属的 tool 结果: %s",
                len(result_by_id), sorted(result_by_id)[:5],
            )
        return repaired

    def _filter_file_blocks(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """
        归一内容块：
        - text / image_url 原样保留
        - 图片 file 块 → 转成标准 image_url（否则模型收不到图，见模块顶部说明）
        - 其余 file 块（路径引用等）丢弃：对上游无意义且可能撑爆请求
        """
        if isinstance(content, str):
            return content

        if not isinstance(content, list):
            return content

        out = []
        for item in content:
            if not isinstance(item, dict):
                out.append(item)
                continue
            if item.get("type") == "file":
                img = _file_block_to_image_url(item)
                if img is not None:
                    out.append(img)
                continue
            out.append(item)
        return out

    def _shrink_inflight_context(self, messages: list[dict[str, Any]],
                                 budget_tokens: int) -> int:
        """工具循环内的压缩闸门（单轮内自保）。

        背景：轮间压缩只在 _build_messages 里触发。但一次用户请求若连续跑几十上百个
        工具调用（max_tool_iterations 默认 300），messages 会在这条 while 循环里一路膨胀，
        直到本轮结束、下一轮才压缩 —— 中途极易撞上模型上下文上限（400/413/500）。

        策略（保守、可逆、不丢信息）：
        - 只处理 role == "tool" 的消息（它们是体积大头，且可重新归档）；
        - 从最旧的开始，超过阈值的正文走 archive_result 精简（头+尾+折叠标记），
          完整内容落盘 tool_archives/，模型仍可用 ReadToolArchive 按需召回；
        - 最近 keep_recent 条 tool 结果不动（模型当前正在用）；
        - 估算已降到预算 70% 以下即停止，避免过度精简。

        返回被精简的消息条数（0 表示未触发或无需处理）。
        """
        if budget_tokens <= 0 or not messages:
            return 0
        try:
            current = sum(
                estimate_tokens(json.dumps(m, ensure_ascii=False))
                for m in messages
            )
        except Exception:
            return 0
        if current <= budget_tokens:
            return 0

        # 目标：降到预算的 70%，给后续若干轮留出余量
        target = int(budget_tokens * 0.7)
        keep_recent = max(0, int(self.config.get("inflight_keep_recent_tools", 6)))

        # 收集可精简的 tool 消息下标（最旧优先），排除最近 N 条
        tool_idx = [i for i, m in enumerate(messages)
                    if isinstance(m, dict) and m.get("role") == "tool"]
        if keep_recent:
            tool_idx = tool_idx[:-keep_recent] if len(tool_idx) > keep_recent else []
        if not tool_idx:
            return 0

        from .tool_archive import archive_result
        _base = self.config.get("base_dir") or PROJECT_ROOT
        # 循环内闸门用更激进的阈值：超过 4000 字符就折叠
        _cfg = dict(self.config)
        _cfg.setdefault("tool_result_max_chars", 4000)
        _cfg["tool_result_max_chars"] = min(
            int(_cfg.get("tool_result_max_chars") or 4000), 4000
        )

        shrunk = 0
        for i in tool_idx:
            if current <= target:
                break
            m = messages[i]
            content = m.get("content")
            # content 可能是 str 或 [{type:text,text:...}]（本项目统一用后者）
            if isinstance(content, list):
                text = "".join(
                    str(p.get("text") or "") for p in content if isinstance(p, dict)
                )
                wrapper = content
            elif isinstance(content, str):
                text = content
                wrapper = None
            else:
                continue
            if not text:
                continue
            try:
                slim, _meta = archive_result(
                    text,
                    base_dir=_base,
                    session_id=self.session_id,
                    tool_name=m.get("name") or "tool",
                    config=_cfg,
                )
            except Exception:
                logger.debug("[Inflight] 压缩工具结果失败，跳过", exc_info=True)
                continue
            if not slim or slim == text:
                continue

            before = estimate_tokens(text)
            after = estimate_tokens(slim)
            if after >= before:
                continue

            if wrapper is not None:
                new_content = []
                for p in wrapper:
                    if isinstance(p, dict) and p.get("type") == "text":
                        new_content.append({**p, "text": slim})
                        # 只替换第一段文本，其余段落保持
                        break
                    new_content.append(p)
                if len(new_content) < len(wrapper):
                    new_content.extend(wrapper[len(new_content):])
                messages[i] = {**m, "content": new_content}
            else:
                messages[i] = {**m, "content": slim}

            current -= (before - after)
            shrunk += 1

        if shrunk:
            logger.info(
                "[Inflight] 单轮内上下文压缩：精简 %d 条工具结果，估算 %d → %d tokens（预算 %d）",
                shrunk, current + 0, current, budget_tokens,
            )
        return shrunk

    def _build_messages(self, context_str: str) -> list[dict[str, Any]]:
        """构建包含系统指令、上下文和滑动窗口的 Prompt。"""

        # 使用临时变量，避免重复累加 system_prompt
        system_prompt = self.system_prompt

        # 限制单条消息的最大长度，防止上下文爆炸(413 错误)
        MAX_CONTENT_LEN = 50000  # 单条消息最大字符数
        TOOL_OUTPUT_MAX_LEN = 2000  # 工具输出最大字符数（参考 opencode）

        def _truncate_content(content):
            """截断过长的消息内容，避免 413 Payload Too Large"""
            if isinstance(content, str):
                if len(content) > MAX_CONTENT_LEN:
                    return content[:MAX_CONTENT_LEN] + f"\n\n[...内容过长，已截断，共 {len(content)} 字符 ...]"
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and "text" in item and isinstance(item["text"], str):
                        if len(item["text"]) > MAX_CONTENT_LEN:
                            item["text"] = item["text"][:MAX_CONTENT_LEN] + f"\n\n[...内容过长，已截断，共 {len(item['text'])} 字符 ...]"
            return content

        def _truncate_tool_output(content):
            """截断工具输出，避免上下文爆炸"""
            if isinstance(content, str) and len(content) > TOOL_OUTPUT_MAX_LEN:
                return content[:TOOL_OUTPUT_MAX_LEN] + f"\n\n[...工具输出已截断，共 {len(content)} 字符 ...]"
            return content

        def _estimate_tokens(text: str) -> int:
            """委托到模块级 estimate_tokens（工具循环内的压缩闸门共用同一实现）。"""
            return estimate_tokens(text)

        if context_str:
            system_prompt += f"\n\n[History Context (Time-Ordered)]:\n{context_str}"

        window = self.memory.get_sliding_window()
        # 获取完整历史用于压缩（参考 opencode：压缩完整历史而非滑动窗口）
        full_history = self.memory.get_full_history()

        # 剥离历史中的 <invoke>/<skill> 伪工具调用块（2026-10-18 恢复）：
        # 窗口/全文统一清洗，避免模型在上下文中继续看到伪调用格式而自模仿。
        window = [dict(m, content=Agent._strip_invoke_blocks(m.get("content"))) for m in window]
        full_history = [dict(m, content=Agent._strip_invoke_blocks(m.get("content"))) for m in full_history]

        messages = [{"role": "system", "content": system_prompt}]
        
        # 自动上下文压缩：当滑动窗口超过预算时，执行 AI 摘要压缩。
        # 【重要】必须用 window 而非 full_history 计量：滑动窗口才是真正发给模型的量。
        # 旧代码用 full_history 计量，而压缩只截断窗口不删历史 → full_history 单调递增，
        # 导致压缩成功后阈值仍天天被突破，每轮请求都白白多打一次摘要 API（实测 794K→1071K）。
        # 语义：1KB = 1024 tokens（256 KB → 262144 tokens）
        budget_tokens = self.context_window_kb * 1024
        if budget_tokens > 0:
            def _msg_tokens(m):
                # 图片的 base64 载荷不能按字符估算（一张图会被误算成十几万 token，
                # 直接误触发压缩）。这里先把图片替换成占位符再计量。
                mm = dict(m)
                mm["content"] = _content_for_token_estimate(mm.get("content"))
                return _estimate_tokens(json.dumps(mm, ensure_ascii=False))

            window_tokens = sum(_msg_tokens(m) for m in window)
            full_history_tokens = sum(_msg_tokens(m) for m in full_history)
            
            # 渐进式压缩：一轮 90% → 二轮 95% → 三轮 97%
            # 轮次取实例内累加计数（成功才 +1）；history 中摘要条数恒为 1，不能用作轮次。
            COMPRESS_THRESHOLDS = [0.90, 0.95, 0.97]
            threshold = COMPRESS_THRESHOLDS[min(self._compress_round, len(COMPRESS_THRESHOLDS) - 1)]
            
            if window_tokens > budget_tokens * threshold:
                try:
                    logger.info(
                        f"滑动窗口超过 {threshold*100:.0f}% 阈值 "
                        f"(window {window_tokens} / budget {budget_tokens} tokens，"
                        f"完整历史 {full_history_tokens}，第 {self._compress_round + 1} 轮)，执行自动压缩..."
                    )
                    # 切割点 = 当前 user 消息之前。
                    # 当前 user 消息是本轮问题，必须保留给模型；其余历史全部进摘要。
                    cut_idx = 0
                    for i in range(len(full_history) - 1, -1, -1):
                        if full_history[i].get("role") == "user":
                            cut_idx = i
                            break
                    if cut_idx > 0:
                        # 核心修正：压缩起点 = 最后一个摘要之后，而不是历史开头！
                        # 旧代码 full_history[:cut_idx] 会把已压缩过的旧部分
                        # （连同旧摘要本身）反复塞进新一轮摘要输入，
                        # 8000 字截断时最新对话（如"打开百度"那轮）被挤出。
                        # 正确做法：只压缩【上次摘要之后新产生的消息】。
                        last_summary_idx = -1
                        for i in range(cut_idx):
                            m = full_history[i]
                            if m.get("is_summary") or (
                                isinstance(m.get("content"), str)
                                and m["content"].startswith("[自动压缩的历史摘要]")
                            ):
                                last_summary_idx = i
                        start_idx = last_summary_idx + 1 if last_summary_idx >= 0 else 0
                        early_msgs = full_history[start_idx:cut_idx]
                        # 保留区（recent）不需要在这里切出来：窗口由
                        # memory.get_sliding_window() 统一负责，此处只需 early 段做摘要。
                        early_ids = [m.get("id") for m in early_msgs if m.get("id")]
                        if not early_msgs:
                            # 摘要紧邻 cut 点、且保留区内没有新的 user 轮次 → 无事可压。
                            # 这是正常状态（不是失败），但必须记日志，否则日志里只见"触发"不见结果，
                            # 排查时无法区分"压缩了但没记"和"压根没压"。
                            logger.info(
                                f"跳过压缩：无可压缩增量（start_idx={start_idx} == cut_idx={cut_idx}），"
                                f"当前 user 消息之前没有新内容"
                            )
                        else:
                            # 构建摘要输入（工具输出截断，总量限制）
                            # 重要：如果已有旧摘要，最新摘要必须能承接旧摘要（旧摘要随后被替换/隐藏），
                            # 因此把旧摘要内容作为"上轮摘要"前置，新摘要命中"增量+承接"。
                            prev_summary_text = ""
                            prev_summary_ids: list[str] = []
                            for i in range(start_idx - 1, -1, -1):
                                m = full_history[i]
                                if m.get("is_summary") or (
                                    isinstance(m.get("content"), str)
                                    and m["content"].startswith("[自动压缩的历史摘要]")
                                ):
                                    prev_summary_text = m.get("content", "")
                                    prev_summary_ids = [m.get("id")] if m.get("id") else []
                                    break
                            if prev_summary_text:
                                prev_summary_text = prev_summary_text.replace("[自动压缩的历史摘要]", "").strip()

                            # 摘要只保留"对话主线"：user 输入 + assistant 最终答复/结论。
                            # 思考链（reasoning_content）与工具原始输出（tool 结果）都不进摘要：
                            # - 思考是达到结果的手段，压缩后无需保留，且 content 常为空、全在思考里，
                            #   塞进去只会让摘要输入膨胀、摘要质量下降；
                            # - 工具结果已由 _truncate_tool_output 截断仍可能很长（实测最长 39K），
                            #   其价值已体现在后续轮次，原始输出仍保留在 sessions.json 可查。
                            early_text = []
                            for m in early_msgs:
                                role = m.get("role", "unknown")
                                if role == "tool":
                                    continue  # 工具原始结果不进摘要
                                content = m.get("content", "")
                                if isinstance(content, list):
                                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                                    content = " ".join(text_parts)
                                content = _truncate_tool_output(content)
                                # thinking 模型的 assistant 消息 content 常为空，推理全在
                                # reasoning_content。若该轮有工具调用，用"调用工具名"补上主线；
                                # 真正答案出现在后续有 content 的轮次。
                                if role == "assistant" and not (content or "").strip():
                                    tc_names = [
                                        ((tc or {}).get("function") or {}).get("name", "")
                                        for tc in (m.get("tool_calls") or [])
                                        if isinstance(tc, dict)
                                    ]
                                    tc_names = [n for n in tc_names if n]
                                    if tc_names:
                                        content = f"[调用工具: {', '.join(tc_names)}]"
                                    else:
                                        continue  # 无内容无工具的空轮次，直接跳过
                                early_text.append(f"{role}: {content}")

                            joined_text = chr(10).join(early_text)

                            def _build_summary_prompt(src_text: str) -> str:
                                return (
                                    "请基于【上一轮摘要】与【本轮新增对话】，生成一份合并后的最新摘要（承接旧摘要，融合新进展），帮助继续后续对话。\n"
                                    "重点关注以下信息：\n"
                                    "- 已完成的工作（含上轮摘要中尚未完成、现已完成的）\n"
                                    "- 当前正在进行的工作（**必须包含最新对话中正在做的事**）\n"
                                    "- 涉及的文件和代码\n"
                                    "- 下一步计划\n"
                                    "- 已确认的事实和决策\n"
                                    "- 未完成的任务\n\n"
                                    "注意：只需返回合并后的最新摘要内容，不要任何寒暄或前缀；若没有上轮摘要就只概括本轮对话。\n\n"
                                    f"{('【上轮摘要】\n' + prev_summary_text + '\n\n') if prev_summary_text else ''}"
                                    f"【本轮新增对话】\n{src_text}"
                                )

                            def _request_summary(src_text: str):
                                return self.client.chat.completions.create(
                                    model=self.model_name,
                                    messages=[{"role": "user", "content": _build_summary_prompt(src_text)}],
                                    temperature=0.3,
                                    max_tokens=1000,
                                    # 全量输入可能较大，给足时间；可用 compress_timeout 覆盖
                                    timeout=float(self.config.get("compress_timeout", 60))
                                )

                            def _is_context_overflow(err: Exception) -> bool:
                                text = str(err).lower()
                                return any(k in text for k in (
                                    "context_length_exceeded", "maximum context length",
                                    "prompt is too long", "input tokens exceed",
                                    "too many tokens", "error code: 413",
                                ))

                            try:
                                # 全量喂入：增量是上一轮滑动窗口的子集，上一轮主请求已成功，
                                # 说明它必然在模型上下文内。仅当上游仍报超限时才降级截断重试。
                                summary_resp = _request_summary(joined_text)
                                _msg = summary_resp.choices[0].message
                                summary_content = (_msg.content or "").strip()
                                # thinking 模型常把全部输出放进 reasoning_content，content 为 None。
                                # 只读 content 会把这类成功响应误判成"返回空"（实测连击 6 次），
                                # 故回退取 reasoning_content。
                                if not summary_content:
                                    summary_content = (
                                        getattr(_msg, "reasoning_content", None) or ""
                                    ).strip()
                            except Exception as e:
                                if _is_context_overflow(e) and len(joined_text) > 8000:
                                    # 异常降级：上游仍报上下文超限时，保尾截断后重试一次。
                                    # 正常路径不截断，避免丢信息。
                                    logger.warning(
                                        f"AI 摘要输入超上下文（{len(joined_text)} 字符），"
                                        f"降级截断后重试一次"
                                    )
                                    joined_keep = (
                                        joined_text[:1200]
                                        + f"\n\n……[中间 {len(joined_text) - 8000} 字符已省略，优先保留最新对话]……\n\n"
                                        + joined_text[-(8000 - 1200):]
                                    )
                                    try:
                                        summary_resp = _request_summary(joined_keep)
                                        _msg = summary_resp.choices[0].message
                                        summary_content = (_msg.content or "").strip()
                                        if not summary_content:
                                            summary_content = (
                                                getattr(_msg, "reasoning_content", None) or ""
                                            ).strip()
                                    except Exception as e2:
                                        logger.warning(f"AI 摘要降级请求仍失败: {e2}")
                                        summary_content = ""
                                else:
                                    logger.warning(f"AI 摘要请求异常: {e}")
                                    summary_content = ""
                            
                            # 失败时直接跳过，不插入任何摘要标签
                            if not summary_content.strip():
                                self._compress_fail_streak += 1
                                logger.warning(
                                    f"AI 压缩摘要第 {self._compress_round + 1} 轮失败（返回空，连击 "
                                    f"{self._compress_fail_streak}），跳过压缩"
                                    + ("；已到失败上限，本轮起允许硬截断兜底" if self._compress_fail_streak >= 3 else "")
                                )
                            else:
                                self._compress_fail_streak = 0
                                try:
                                    summary_msg_id = self.memory.insert_compressed_summary(summary_content, after_ids=early_ids)
                                    self._pending_summary = {
                                        "type": "compact_summary",
                                        "msg_id": summary_msg_id,
                                        "content": f"[自动压缩的历史摘要]\n{summary_content}",
                                        "timestamp": time.time(),
                                    }
                                    # 新摘要已承接旧摘要 → 先物理删除旧摘要（及中间残留旧消息由窗口隐藏），
                                    # 否则 get_sliding_window() 找到"最新摘要"还是旧的那条。
                                    if prev_summary_ids:
                                        self.memory.remove_messages(prev_summary_ids)
                                    # 只有成功落盘并重建窗口后，才认定这一轮压缩完成并递进阈值层
                                    self._compress_round += 1
                                    window = self.memory.get_sliding_window()
                                    window_tokens = sum(_msg_tokens(m) for m in window)
                                    logger.info(f"第 {self._compress_round} 轮压缩成功，窗口 tokens: {window_tokens}")
                                except Exception as e:
                                    logger.warning(f"写入压缩摘要失败: {e}")
                except Exception as e:
                    logger.warning(f"AI 自动压缩失败: {e}")

            # 硬截断兜底：仅当压缩已连续失败 3 次（90/95/97 三层都试过）且仍超预算时才执行。
            # 正常路径压缩失败只是升级阈值层，下一轮再试，绝不因一次失败就丢消息。
            if window_tokens > budget_tokens and self._compress_fail_streak >= 3:
                kept = []
                current_tokens = 0
                for msg in reversed(window):
                    msg_tokens = _msg_tokens(msg)
                    if current_tokens + msg_tokens > budget_tokens and kept:
                        break
                    kept.insert(0, msg)
                    current_tokens += msg_tokens
                
                if not kept:
                    kept = [window[-1]]
                
                dropped_count = len(window) - len(kept)
                if dropped_count > 0:
                    messages.append({
                        "role": "system",
                        "content": f"[硬截断：早期 {dropped_count} 条消息因长度限制已移除，请查阅历史记录]"
                    })
                window = kept

        # 找出最新一条 user 消息下标：只保留它的图片，历史轮次的图降级为占位文本
        # （图片以 base64 存在历史里，每轮重发既膨胀上下文又重复计费）。
        _last_user_idx = -1
        for _i, _m in enumerate(window):
            if _m.get("role") == "user":
                _last_user_idx = _i

        for _i_idx, msg in enumerate(window):
            content = msg.get("content")

            # --- 需要保留 reasoning_content 以支持 thinking 模式
            # 确保我们传递完整的消息结构；_build_messages 内部会进行安全清理
            reconstructed = {
                "role": msg["role"],
                "content": content,
            }
            if "tool_calls" in msg:
                reconstructed["tool_calls"] = msg["tool_calls"]
            if "tool_call_id" in msg:
                reconstructed["tool_call_id"] = msg["tool_call_id"]
            # 如果希望 thinking 模式与 tool_calls 一起运作，务必保留 reasoning_content
            if "reasoning_content" in msg:
                reconstructed["reasoning_content"] = msg["reasoning_content"]
            # 压缩摘要（参考 opencode: 伪装成 assistant 消息 + SummaryMessageID 截断，旧消息逻辑隐藏）
            # 不剥离前缀，保留 is_summary 标记供前端识别，但模型侧视为普通 assistant 上下文
            if msg.get("is_summary"):
                reconstructed["is_summary"] = True

            content = self._filter_file_blocks(content)
            # 图片只保留最新一轮 user 的；历史图片降级为占位文本。
            # 注意：必须在 _filter_file_blocks 之后做，因为 file 块要先转成 image_url
            # 才能被识别为图片，否则这里根本看不到图、也就无从降级。
            if _i_idx != _last_user_idx:
                content = _drop_image_blocks(content)
            content = _truncate_content(content)
            if reconstructed["role"] == "tool":
                # 兼容旧会话：历史里可能已存有 <tool> XML 结果，发送前统一归一化。
                content = self._tool_content_for_model(content)

            m = {"role": reconstructed["role"], "content": content}
            if "tool_calls" in reconstructed:
                # 只要求有函数名即可：无参数的工具（如 get_system_resources）arguments
                # 可能为空串，此处若一并丢弃，会导致 assistant 声明与其后的 tool 结果
                # 双双消失，模型看不到"自己调用过该工具"。空参数由
                # _sanitize_messages_for_api 统一替换为合法空 JSON "{}"。
                tc = [
                    c for c in reconstructed["tool_calls"]
                    if (((c or {}).get("function") or {}).get("name") or "").strip()
                ]
                if tc:
                    m["tool_calls"] = tc
            if "tool_call_id" in reconstructed:
                m["tool_call_id"] = reconstructed["tool_call_id"]
            if "reasoning_content" in reconstructed and not Agent._upstream_rejects_reasoning:
                # thinking 模式允许时保留
                m["reasoning_content"] = reconstructed["reasoning_content"]
            messages.append(m)

        # 清理历史消息中非法 JSON 的 tool_calls，防止 API 400 错误
        return self._sanitize_messages_for_api(messages)
