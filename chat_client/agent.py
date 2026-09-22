"""
@input: openai, httpx2, chat_client.memory, chat_client.retrieval, chat_client.tools.registry
@output: Agent 主循环（流式/重试/续写）、ChatJob、事件生成
@position: Core agent runtime — 所有对话模式的执行引擎
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import datetime
import json
import logging
import os
import platform
import threading
import time
import traceback
import uuid
from collections.abc import Generator
from typing import Any

import openai

# openai SDK 3.x 基于 httpx2（重命名版），流式中断抛 httpx2.RemoteProtocolError 等
try:
    import httpx2
except ImportError:
    httpx2 = None

from chat_client.api_retry import is_retryable_api_err
from chat_client.memory import MemoryManager
from chat_client.retrieval import ExternalRAGService, RAGService, Mem0Service

from .tools import registry, set_session_allow_high
from .tools.base import _xml_response

logger = logging.getLogger(__name__)

# L-1: tiktoken 编码器缓存在模块级，避免每次调用重复导入
_TIKTOKEN_ENC = None

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


class Agent:
    # 集团协作工具：需要多 Agent 编排，单 Agent / Codex-X 模式不加载
    _GROUP_TOOLS: set[str] = {"RunCrew", "ConsultPeer", "CreateDepartment", "RecruitMember"}

    def __init__(self, session_id: str, config: dict[str, Any] | None = None):
        self.session_id = session_id
        self.config = config or {}
        
        # 提取配置
        self.api_key = self.config.get("api_key")
        self.base_url = self.config.get("base_url")
        self.model_name = self.config.get("model_name") or self.config.get("default_model")
        self.rag_trigger_threshold = self.config.get("rag_trigger_threshold", 10)
        self.max_tool_iterations = self.config.get("max_tool_iterations", 9999)
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
        self.reasoning_effort = self.config.get("reasoning_effort") or _agent_cfg.get("reasoning_effort", "high")
        self.thinking = self.config.get("thinking", _agent_cfg.get("thinking", False))
        self.web_search = self.config.get("web_search", _agent_cfg.get("web_search", False))
        
        # 官网知识库
        self.use_external_kb = self.config.get("use_external_kb", False)
        self.external_kb_appid = self.config.get("external_kb_appid", "app_002")

        # 全局记忆检索开关（mem0 / ExternalRAG）
        self.use_global_rag = self.config.get("use_global_rag", False)
        
        # Code mode configuration
        self.code_mode = self.config.get("code_mode", False)
        # 严格工具模式：完全以 config["tools"] 为准（集团模式下主对话=经理仅编排工具；
        # 成员代理各自定义工具），不追加任何默认/MCP 工具
        self.strict_tools = self.config.get("strict_tools", False)

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
            # 注入 mem0 记忆使用提示
            self.system_prompt += "\n\n## 记忆系统\n- `mem0_search` 查询历史记忆（用户偏好、踩坑记录、已完成任务）\n- `mem0_add` 写入重要信息（用户明确说'记住'、协作规则、技术决策）\n- `mem0_list` 列出所有记忆（按 agent_id 过滤，默认50条）\n- `mem0_delete` 删除指定 ID 的记忆（不可撤销）\n- `mem0_import` 批量导入（多行文本，每行一条）\n- 涉及跨会话上下文、用户偏好、历史决策时主动调用，不要凭空回答"

        if not self.strict_tools:
            # 单 Agent 模式：默认全量工具，排除集团协作工具
            # 集团工具（Task/RunCrew/ConsultPeer/CreateDepartment/RecruitMember）需要多 Agent 编排
            _all_ids = {m["id"] for m in registry._metadata.values()}
            self.enabled_tools.extend(t for t in _all_ids - self._GROUP_TOOLS
                                      if t not in self.enabled_tools)
            
        self.memory = MemoryManager(
            session_id=session_id, 
            sessions_dir=self.config.get("sessions_dir", "sessions"),
            sliding_window_size=self.config.get("sliding_window_size", 10)
        )

        # 压缩失败连击计数：达到阈值层数上限前，失败只升级阈值、不硬截断
        self._compress_fail_streak = 0
        
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
        # 前缀缓存：对 system 消息打 cache_control，使稳定的 system+tools 前缀命中缓存，
        # 避免每轮全量工具定义重复计费（兼容 litellm/OpenAI 前缀缓存；不支持的代理会忽略该字段）
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages = [dict(messages[0], cache_control={"type": "ephemeral"})] + messages[1:]

        params = {
            "model": self.model_name,
            "messages": messages,
            "tools": tools if tools else None,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "top_p": self.top_p,
            "reasoning_effort": self.reasoning_effort,
            "extra_body": {}
        }

        thinking = self.thinking

        model_l = str(self.model_name or "").lower()
        # 仅对 qwen 系模型注入 DashScope 专属扩展参数（原条件 "qwen" or ... 恒真，会污染所有模型）
        if "qwen" in model_l or "default" in model_l:
            params["extra_body"]["enable_thinking"] = thinking

        # 原实现把 enable_search 绑死在 qwen 系上，导致其它模型（doubao/gpt 等）
        # 即使前端开关打开也静默失效。联网搜索已改为独立的 WebSearch 工具
        # （见 chat_client/tools/websearch.py），与模型无关，此处不再注入该扩展参数。
        
        if "doubao" in str(self.model_name).lower():
            enable_type = "enabled" if thinking else "disabled"
            params['extra_body']["thinking"] = {
                "type": enable_type
            }
            
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

                # Copy messages to avoid polluting history with ephemeral warnings
                request_messages = list(messages)

                if iteration_count > 1:
                    remaining = self.max_tool_iterations - iteration_count + 1
                    iter_msg = f"\n<system-reminder>Action Count: {iteration_count}/{self.max_tool_iterations}. "
                    if remaining <= 2:
                        iter_msg += "WARNING: You are approaching the tool execution limit. If the task is not finished, STOP NOW and ask the user to continue in the next turn to reset the counter. Do NOT try to rush."
                    else:
                        iter_msg += "Proceed efficiently."
                    iter_msg += "<system-reminder>"
                    
                    # Append as a temporary system message for this request only
                    request_messages.append({"role": "system", "content": iter_msg})
                
                # 首次迭代（model 第一次回复）只传递用户指定工具；后续迭代使用全量工具
                iter_tools = first_tools if iteration_count == 1 else tools

                # ---- 带自动重试的流式请求：断流 / 429 / 5xx 后重新请求 ----
                # OpenAI SDK 的 max_retries 只覆盖建连阶段，不覆盖流式中途断开；
                # 这里在应用层重试：无可见输出时完全重来；已有输出时切换"续写模式"避免重复内容。
                api_max_retry = max(0, int(self.config.get("api_max_retry", 3)))
                api_retry_base_wait = float(self.config.get("api_retry_base_wait", 1))
                api_retry_max_wait = float(self.config.get("api_retry_max_wait", 10))

                attempt = 0
                yielded_content = ""  # 本轮所有尝试中已实时推送给前端、不可撤回的正文
                while True:
                    attempt += 1
                    tool_call_chunks = {}
                    current_response_content = ""
                    current_reasoning_content = ""
                    reported_tool_indices = set()
                    try:
                        # 防御：清理可能含非法 JSON 的 tool_calls
                        request_messages = self._sanitize_messages_for_api(request_messages)
                        response_stream = self._create_completion_stream(request_messages, iter_tools)

                        for chunk in response_stream:

                            # 停止检查：chat_stop 置位后立即断开，不再消费流
                            if _is_cancelled():
                                _close_stream(response_stream)
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

                            # 处理推理内容
                            if getattr(delta, "reasoning_content", None):
                                current_reasoning_content += delta.reasoning_content
                                yield {
                                    "type": "reasoning",
                                    "response": delta.reasoning_content
                                }

                            # 处理正文内容
                            if delta.content:
                                current_response_content += delta.content
                                yielded_content += delta.content
                                yield {
                                    "type": "content",
                                    "response": delta.content
                                }

                            # 处理工具调用
                            if delta.tool_calls:
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

                        # 本轮成功结束：若发生过重试（续写模式），此前中断尝试的正文已在
                        # yielded_content 中实时推送给前端，最终完整文本即为全部已推送内容
                        if attempt > 1:
                            current_response_content = yielded_content

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

                        break

                    except Exception as api_err:
                        # 取消优先：停止请求导致的连接中断不触发重试，立即退出
                        if _is_cancelled():
                            logger.info("[AI-Retry] 任务已停止，跳过重试")
                            return
                        if not is_retryable_api_err(api_err) or attempt > api_max_retry:
                            raise
                        wait_s = min(api_retry_base_wait * (2 ** (attempt - 1)), api_retry_max_wait)
                        logger.warning(
                            f"[AI-Retry] 接口流异常 {type(api_err).__name__}，{wait_s}s 后第 {attempt}/{api_max_retry} 次重试: {str(api_err)[:200]}"
                        )
                        if _sleep_interruptible(wait_s):
                            logger.info("[AI-Retry] 等待重试期间任务被停止")
                            return
                        if yielded_content:
                            # 续写模式：把已输出内容作为上下文附上，要求模型从中断处继续（前端按序拼接，不会重复）
                            # 移除上一轮重试附加的续写上下文，避免重复堆叠
                            if request_messages and request_messages[-1].get("content", "").startswith("[system-note]"):
                                del request_messages[-2:]
                            request_messages.append({"role": "assistant", "content": yielded_content})
                            request_messages.append({"role": "system", "content":
                                "[system-note] 上一次响应因网络异常被截断。请从上次中断处无缝继续输出剩余内容，"
                                "不要重复任何已输出的文字，保持语气与格式连贯。"})
                        else:
                            # 尚无任何可见输出（或只有未执行的半截工具调用）→ 完全重新请求即可
                            pass
                        # 关键修复：sleep 后必须 continue 回到 while True 重新请求，
                        # 否则会掉出 except 走"空响应->stop"分支，重试从未真正发生
                        continue
                # 如果这一轮有内容，累加到最终响应
                if current_response_content:
                    full_response_content = current_response_content
                if current_reasoning_content:
                    full_reasoning_content = current_reasoning_content

                # 如果没有工具调用，结束循环
                if not tool_call_chunks:
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
                
                # 保存助手工具调用消息
                if current_reasoning_content:
                    assistant_msg_kwargs["reasoning_content"] = current_reasoning_content

                self.memory.add_message("assistant", current_response_content,id=ai_msg_id, **assistant_msg_kwargs)

                messages.append({
                    "role": "assistant",
                    "content": current_response_content,
                    "tool_calls": assistant_msg_kwargs["tool_calls"]
                })
                # 关键修改：确保 reasoning_content 也被带到消息列表中
                if "reasoning_content" in assistant_msg_kwargs:
                    messages[-1]["reasoning_content"] = assistant_msg_kwargs["reasoning_content"]

                # 执行工具
                for tc in assistant_msg_kwargs["tool_calls"]:
                    # 停止检查：已请求停止则不再执行后续工具（含 Bash 等高危工具）
                    if _is_cancelled():
                        self._mark_unexecuted_tools(assistant_msg_kwargs["tool_calls"], messages, ai_msg_id)
                        return
                    func_name = tc["function"]["name"]
                    args_str = tc["function"]["arguments"]
                    call_id = tc["id"]
                    
                    tool_exists = registry.tool_exists(func_name)
                    tool_enabled = registry.is_tool_enabled(func_name, self.enabled_tools) if tool_exists else False
                    
                    #处理不存在的工具
                    if not tool_exists:
                        result_str = _xml_response("error", f"Error: Tool '{func_name}' does not exist.")
                        content_structure = [{"type": "text", "text": result_str}]
                        self.memory.add_message("tool", content_structure, tool_call_id=call_id, id=ai_msg_id)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": content_structure
                        })
                        continue
                    
                    #处理未启用的工具
                    if not tool_enabled:
                        tool_id = registry.get_tool_id(func_name)
                        result_str = _xml_response("error", f"Error: Tool '{func_name}' (ID: {tool_id}) is not enabled. You do not have permission to use this tool.")
                        content_structure = [{"type": "text", "text": result_str}]
                        self.memory.add_message("tool", content_structure, tool_call_id=call_id, id=ai_msg_id)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": content_structure
                        })
                        continue
                    
                    yield {
                        "type": "tool_call",
                        "tool": func_name,
                        "args": args_str,
                        "id": call_id
                    }
                    try:
                        if args_str and args_str.strip():
                            args = json.loads(args_str)
                        else:
                            args = {}
                    except json.JSONDecodeError:
                        logger.warning(
                            "[Tool-Args] 工具 %s 的参数不是合法 JSON，按空参数处理: %r",
                            func_name, (args_str or "")[:200]
                        )
                        args = {}

                    # 类型矫正：按 schema 把字符串化的数字/布尔参数转回真实类型
                    # （模型常把 timeout 等数值传成字符串，导致 _validate_args 报 type error）
                    args = self._coerce_tool_args(func_name, args)

                    # Special handling for Task/RunCrew/ConsultPeer to inherit config
                    try:
                        if func_name in ("Task", "RunCrew", "ConsultPeer"):
                            # Create a copy of config to avoid modification
                            agent_config = self.config.copy()
                            # Remove specific keys that shouldn't be inherited or will be overridden
                            agent_config.pop("system_prompt", None)
                            agent_config.pop("tools", None)

                            # Inject into args
                            args["parent_config"] = agent_config
                            args["parent_session_id"] = self.session_id

                        # Inject session_id for Todo and Summary tools
                        if func_name in ["TodoWrite", "TodoRead", "TaskSummary"]:
                            args["session_id"] = self.session_id
                            args["sessions_dir"] = self.config.get("sessions_dir", "sessions")

                        # Inject cwd for Bash/Terminal tools
                        if func_name in ("Bash", "CheckCommandStatus", "StopCommand"):
                            cwd = self.current_dir if hasattr(self, 'current_dir') and self.current_dir else None
                            if cwd and "cwd" not in args:
                                args["cwd"] = cwd

                        func = registry.get_tool_func(func_name)
                        if func:
                            result_str = func(**args)
                        else:
                            result_str = _xml_response("error", f"Error: Tool {func_name} not found.")
                    except Exception as e:
                        result_str = _xml_response("error", f"Error executing tool: {e!s}")

                    yield {
                        "type": "tool_result",
                        "tool": func_name,
                        "result": result_str,
                        "id": call_id
                    }

                    # 截断过长的工具结果，防止上下文爆炸 (413 错误)
                    MAX_TOOL_RESULT_LEN = 50000
                    if isinstance(result_str, str) and len(result_str) > MAX_TOOL_RESULT_LEN:
                        result_str = result_str[:MAX_TOOL_RESULT_LEN] + f"\n\n[...工具结果过长，已截断，共 {len(result_str)} 字符...]"

                    # Construct content with XML structure
                    content_structure = [
                        {
                            "type": "text",
                            "text": result_str
                        }
                    ]

                    # Add to memory with new structure
                    self.memory.add_message("tool", content_structure, tool_call_id=call_id, id=ai_msg_id)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": content_structure
                    })

            # 6. 最终记忆更新 (助手响应)
            if not tool_call_chunks and full_response_content:
                 kwargs = {}
                 if full_reasoning_content:
                     kwargs["reasoning_content"] = full_reasoning_content
                 
                 # 使用预生成的 ai_msg_id
                 ai_msg = self.memory.add_message("assistant", full_response_content, id=ai_msg_id, **kwargs)
                 
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
                "text": _xml_response("error", "任务被用户停止，该工具未执行。"),
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

        当流式请求中断时，累积的 tool call arguments 可能是截断的 JSON。
        这些消息被保存到历史后，下次请求会发给 API，导致:
        "Assistant tool call arguments must be valid JSON" 400 错误。
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

        return sanitized

    def _filter_file_blocks(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        """
        过滤掉 type="file" 的块，只保留 type="text" 的块
        """
        if isinstance(content, str):
            return content
        
        if not isinstance(content, list):
            return content
        
        return [item for item in content if not (isinstance(item, dict) and item.get("type") == "file")]

    def _build_messages(self, context_str: str) -> list[dict[str, Any]]:
        """构建包含系统指令、上下文和滑动窗口的 Prompt。"""

        # 使用临时变量，避免重复累加 system_prompt
        system_prompt = self.system_prompt

        # 限制单条消息的最大长度，防止上下文爆炸(413 错误)
        MAX_CONTENT_LEN = 50000  # 单条消息最大字符数
        TOOL_OUTPUT_MAX_LEN = 2000  # 工具输出最大字符数（参考 opencode）
        KEEP_RECENT_TURNS = 4  # 保留最近的消息数量

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
            """估算 token 数量（CJK-aware：尽量贴近实际，避免高估/低估）。
            策略：优先尝试 tiktoken；降级到启发式估算。
            - CJK 字符（\u4e00-\u9fff）、全角标点、假名：约 1 字符/token（偏高，防截断过早）
            - ASCII 字母/数字：约 4 字符/token（更接近真实比）
            - 其余标点/空白：1 字符/token
            总体上限与下限均合理，避免英文严重高估、中文严重低估。"""
            try:
                global _TIKTOKEN_ENC
                if _TIKTOKEN_ENC is None:
                    import tiktoken
                    _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
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

        if context_str:
            system_prompt += f"\n\n[History Context (Time-Ordered)]:\n{context_str}"

        window = self.memory.get_sliding_window()
        # 获取完整历史用于压缩（参考 opencode：压缩完整历史而非滑动窗口）
        full_history = self.memory.get_full_history()

        messages = [{"role": "system", "content": system_prompt}]
        
        # 自动上下文压缩：当完整历史超过预算时，执行 AI 摘要压缩
        # 参考 opencode：压缩完整历史记录，而非滑动窗口
        # 语义：1KB = 1024 tokens（256 KB → 262144 tokens）
        budget_tokens = self.context_window_kb * 1024
        if budget_tokens > 0:
            total_tokens = sum(_estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in full_history)
            
            # 渐进式压缩：一轮 90% → 二轮 95% → 三轮 97%
            # 已有摘要数 = round + 1；首次触发 90%，二次 95%，三次 97%
            existing_summaries = sum(
                1 for m in full_history
                if m.get("is_summary") or (
                    isinstance(m.get("content"), str)
                    and m["content"].startswith("[自动压缩的历史摘要]")
                )
            )
            COMPRESS_THRESHOLDS = [0.90, 0.95, 0.97]
            threshold = COMPRESS_THRESHOLDS[min(existing_summaries, len(COMPRESS_THRESHOLDS) - 1)]
            
            if total_tokens > budget_tokens * threshold:
                try:
                    logger.info(f"完整历史超过 {threshold*100:.0f}% 阈值 ({total_tokens}/{budget_tokens} tokens)，执行自动压缩...")
                    # 安全切割：cut 点对齐到 user 轮次边界，保证工具调用序列完整
                    if len(full_history) > KEEP_RECENT_TURNS:
                        safe_cut = len(full_history) - KEEP_RECENT_TURNS
                        cut_idx = 0
                        for i in range(safe_cut, -1, -1):
                            if full_history[i].get("role") == "user":
                                cut_idx = i
                                break
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
                        recent_msgs = full_history[cut_idx:]
                        early_ids = [m.get("id") for m in early_msgs if m.get("id")]
                        if early_msgs:
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

                            early_text = []
                            for m in early_msgs:
                                role = m.get("role", "unknown")
                                content = m.get("content", "")
                                if isinstance(content, list):
                                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                                    content = " ".join(text_parts)
                                content = _truncate_tool_output(content)
                                early_text.append(f"{role}: {content}")

                            joined_text = chr(10).join(early_text)
                            SUMMARY_SRC_LEN = 8000
                            if len(joined_text) > SUMMARY_SRC_LEN:
                                # 最新对话优先：超过上限时保尾（最新），头部保留一点点背景
                                joined_keep = (
                                    joined_text[:1200]
                                    + f"\n\n……[中间 {len(joined_text)-SUMMARY_SRC_LEN} 字符已省略，优先保留最新对话]……\n\n"
                                    + joined_text[-(SUMMARY_SRC_LEN - 1200):]
                                )
                            else:
                                joined_keep = joined_text

                            summary_prompt = (
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
                                f"【本轮新增对话】\n{joined_keep}"
                            )
                            
                            try:
                                summary_resp = self.client.chat.completions.create(
                                    model=self.model_name,
                                    messages=[{"role": "user", "content": summary_prompt}],
                                    temperature=0.3,
                                    max_tokens=1000,
                                    timeout=30  # 压缩是旁路调用：最多等 30s，失败即跳过，不让主请求陪等
                                )
                                summary_content = summary_resp.choices[0].message.content or ""
                            except Exception as e:
                                logger.warning(f"AI 摘要请求异常: {e}")
                                summary_content = ""
                            
                            # 失败时直接跳过，不插入任何摘要标签
                            if not summary_content.strip():
                                self._compress_fail_streak += 1
                                logger.warning(
                                    f"AI 压缩摘要第 {existing_summaries + 1} 轮失败（返回空，连击 "
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
                                    window = self.memory.get_sliding_window()
                                    total_tokens = sum(_estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in window)
                                    logger.info(f"第 {existing_summaries + 1} 轮压缩成功，窗口 tokens: {total_tokens}")
                                except Exception as e:
                                    logger.warning(f"写入压缩摘要失败: {e}")
                except Exception as e:
                    logger.warning(f"AI 自动压缩失败: {e}")

            # 硬截断兜底：仅当压缩已连续失败 3 次（90/95/97 三层都试过）且仍超预算时才执行。
            # 正常路径压缩失败只是升级阈值层，下一轮再试，绝不因一次失败就丢消息。
            if total_tokens > budget_tokens and self._compress_fail_streak >= 3:
                kept = []
                current_tokens = 0
                for msg in reversed(window):
                    msg_tokens = _estimate_tokens(json.dumps(msg, ensure_ascii=False))
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

        for msg in window:
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
            content = _truncate_content(content)

            m = {"role": reconstructed["role"], "content": content}
            if "tool_calls" in reconstructed:
                tc = []
                for c in reconstructed["tool_calls"]:
                    fn = ((c or {}).get("function") or {})
                    name = (fn.get("name") or "").strip()
                    args = (fn.get("arguments") or "").strip()
                    if name and args:
                        tc.append(c)
                if tc:
                    m["tool_calls"] = tc
            if "tool_call_id" in reconstructed:
                m["tool_call_id"] = reconstructed["tool_call_id"]
            if "reasoning_content" in reconstructed:
                # thinking 模式允许时保留
                m["reasoning_content"] = reconstructed["reasoning_content"]
            messages.append(m)

        # 清理历史消息中非法 JSON 的 tool_calls，防止 API 400 错误
        return self._sanitize_messages_for_api(messages)
