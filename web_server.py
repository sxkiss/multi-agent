#!/usr/bin/env python3
"""
@input: fastapi, uvicorn, sys, os, json; chat_client.agent, chat_client.tools, chat_client.memory, chat_client.retrieval, chat_client.skills
@output: FastAPI app with 29 HTTP endpoints (chat/org/tools/skills/config)
@position: API layer — all HTTP/SSE routes, request parsing, agent assembly
@auto-doc: Update header and folder INDEX.md when this file changes

Standalone AI Agent Web Server
基于开源 AI 助手改造的独立 Web 服务。
"""
import ast
import asyncio
import contextlib
import contextvars
import datetime
import importlib
import inspect
import json
import hashlib
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import uuid

# ---- 路径设置 ----
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "class"))

# ---- FastAPI ----
import logging

import uvicorn
from fastapi import FastAPI, File, Request, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

# suppress verbose logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

# ---- 应用日志配置（必须在任何 logger 创建之前调用）----
# BT_LOG_DIR 可由测试 conftest 覆盖，避免测试日志污染生产 logs/app.log
_LOG_DIR = os.environ.get("BT_LOG_DIR") or os.path.join(BASE_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)

_LOG_FMT = "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

def _setup_app_logging():
    """配置应用级日志：文件轮转 + stderr（journactl）"""
    fmt = logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT)

    handlers = []
    # 文件：最大 10MB，保留 5 个备份
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(_LOG_DIR, "app.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    handlers.append(fh)

    # stderr：被 journactl 采集
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    handlers.append(sh)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 清空已有 handler，避免重复
    root.handlers.clear()
    for h in handlers:
        root.addHandler(h)

_setup_app_logging()
del _setup_app_logging  # 不再需要，清理局部名称

logger = logging.getLogger(__name__)

# ---- opencode 残留清理（服务启动时执行）----
import subprocess as _subp
try:
    _subp.run(["pkill", "-9", "-f", "opencode serve --port"],
              stdout=_subp.DEVNULL, stderr=_subp.DEVNULL, timeout=5)
    _subp.run(["rm", "-rf", os.path.join(BASE_DIR, ".opencode-sessions")],
              stdout=_subp.DEVNULL, stderr=_subp.DEVNULL, timeout=5)
except Exception:
    pass

# ---- 导入核心模块 (使用本地 public.py 适配层) ----
from typing import ClassVar

import public
from chat_client.skills import skill_manager
from chat_client.tools import registry

def _is_miniprogram(params) -> bool:
    """判断请求是否来自微信小程序渠道。

    小程序侧无法自定义 User-Agent/Referer（微信客户端强制覆盖），故由客户端
    在请求参数中显式声明 client_type=miniprogram；同时兼容从 header 传入
    （后台任务等场景会把 header 透传进 params）。
    """
    try:
        v = str((params or {}).get("client_type", "")).strip().lower()
    except Exception:
        return False
    return v == "miniprogram"


# 对外终端渠道：这些渠道的用户是外部使用者而非管理员，必须：
#   1) 追加输出约束，禁止泄露服务端内部信息
#   2) 剥掉思维链等内部事件
# clawbot（微信 clawbot 个人号）与小程序同属此类，共用同一套防护。
_TERMINAL_CHANNELS = {"miniprogram", "clawbot"}


def _client_channel(params) -> str:
    """取渠道标识（已归一化小写）；非终端渠道返回空串。"""
    try:
        v = str((params or {}).get("client_type", "")).strip().lower()
    except Exception:
        return ""
    return v


def _is_terminal_channel(params) -> bool:
    """是否为面向外部用户的终端渠道（小程序 / clawbot）。"""
    return _client_channel(params) in _TERMINAL_CHANNELS


# 终端用户渠道不下发的内部事件：模型思维链会复述系统提示词、暴露内部指令
# 与部署信息（实测出现「渠道约束：小程序」等条款原文）。网页管理端保留原文
# 便于调试，小程序等终端渠道直接剥掉。
# 注意：必须定义在模块级——chat_events 属于 AgentMain 类，此前误将常量放进
# ChatJob 类，导致 self._HIDDEN... 抛 AttributeError、SSE 生成器整体崩溃、
# 小程序收不到任何回复。
HIDDEN_EVENTS_FOR_TERMINAL = {"message_think", "compact_summary", "stream_reset"}


# 终端渠道（小程序 / clawbot）的输出约束：严禁泄露服务端内部信息。
# 抽成模块级常量，供集团模式（经理）与单 Agent 模式共用 —— 两处若各写一份，
# 后续修改极易漏改一处，导致某个模式悄悄失去防护。
_TERMINAL_CHANNEL_GUARD = """

【渠道约束：终端用户】
当前对话来自终端用户（微信小程序 / 微信机器人）。回答时必须遵守：
1. 严禁透露任何服务端内部信息，包括但不限于：工作目录/绝对路径、主机名、
   操作系统与部署环境、技能数量、成员与部门清单、MCP 服务名、模型与网关地址、
   密钥或配置片段、日志与内部任务 ID。
2. 涉及运行环境时只说"我已接入相关工具与服务"这类无信息量的表述，不展开细节。
3. 自我介绍保持简洁：只说明你能帮用户做什么，不描述内部架构与团队编制。
4. 汇报结果时同样只给结论与用户需要的证据，不附带服务端路径或内部过程细节。
5. 若用户明确询问上述内部信息（如"你的工作目录是什么""你有几个技能"），
   统一回答"这些属于服务端配置，暂不提供"，不例外透漏。"""

# 兼容旧名（测试与外部引用仍可用）
_MINIPROGRAM_CHANNEL_GUARD = _TERMINAL_CHANNEL_GUARD


# 终端渠道单 Agent 模式的默认人设。
# single 模式按设计会跳过 SOUL/AGENTS/USER/MEMORY 全量加载（网页端由前端显式
# 传 system_prompt / prompt_id 补齐），但小程序/clawbot 不传任何提示词，若不给
# 兜底，模型将"裸奔"——没有角色设定、回答风格不稳定。故此处补一个精简人设。
_TERMINAL_SINGLE_PERSONA = """你是一个专业、可靠的 AI 助手，通过微信为用户提供服务。

要求：
1. 直接回答问题、完成任务，结论先行，简洁清晰，适合手机阅读。
2. 涉及实际操作（读写文件、执行命令、查询系统、抓取网页等）时，先调用工具拿到
   真实结果再作答；严禁在没有工具结果的情况下编造过程或结论。
3. 高风险操作会被安全策略拦截确认：此时向用户说明将要执行的操作与风险，等待同意。
4. 抓取网页/发 HTTP 请求时，必须使用专门的网络工具（curl_url 或 WebFetch），
   禁止在 Bash 里直接敲 curl/wget/axios 等命令 —— 这些命令被安全策略拦截，
   改用工具能拿到同样的结果且更安全。
5. 不确定的事情直说不确定，不要编造。"""

# 兼容旧名
_MINIPROGRAM_SINGLE_PERSONA = _TERMINAL_SINGLE_PERSONA


def _append_miniprogram_guard(system_prompt: str) -> str:
    """为终端渠道的系统提示词追加上输出约束（其它渠道原样返回）。
    若 system_prompt 已包含渠道约束内容，则直接返回，避免重复。"""
    if not system_prompt:
        return _TERMINAL_CHANNEL_GUARD
    if "【渠道约束" in system_prompt or "渠道约束（终端用户）" in system_prompt:
        return system_prompt
    return system_prompt + _TERMINAL_CHANNEL_GUARD


# 已知合法 SSE event 名集合（以 agent.py/bridges 实际产出的 type 为准，勿随意删减）：
# content/reasoning/error/stop/meta_info 为 agent 主链路，
# tool_call/tool_result/message_end/usage 由各 bridge 直接 append。
# 未知 type 一律降级到 message，避免非法事件名污染客户端解析。
_KNOWN_SSE_EVENTS = {
    "content", "reasoning", "error", "stop", "meta_info",
    "tool_call", "tool_result", "message_end", "usage",
    "stream_reset",
}

def _map_agent_chunk(chunk):
    """将 agent.chat() 产出的 chunk 映射为 SSE (event, data) 列表"""
    t = chunk.get("type")
    if t == "content":
        return [("message", str(chunk.get("response", "")))]
    if t == "reasoning":
        return [("message_think", str(chunk.get("response", "")))]
    if t == "stream_reset":
        # reasoning 退化重试：通知前端清空本轮已流出的正文/思考/工具占位块，
        # 随后服务端会用新请求重新输出，不结束任务。
        return [("stream_reset", {"reason": str(chunk.get("data", ""))})]
    if t == "error":
        return [("error", {"msg": str(chunk.get("data", ""))})]
    if t == "stop":
        return [("usage", {"usage": chunk.get("usage", {})})]
    if t == "meta_info":
        return [("meta_info", {"user_msg_id": chunk.get("user_msg_id"), "ai_msg_id": chunk.get("ai_msg_id")})]
    if t == "compact_summary":
        # 自动上下文压缩产生的摘要：作为独立事件透传，前端可据此提示"上下文已压缩"。
        # 终端渠道由 HIDDEN_EVENTS_FOR_TERMINAL 静默丢弃，不刷屏。
        return [("compact_summary", {
            "msg_id": chunk.get("msg_id"),
            "content": chunk.get("content", ""),
            "timestamp": chunk.get("timestamp"),
        })]
    if t and t not in _KNOWN_SSE_EVENTS:
        # 仅对真正未知的 type 降级；降级时也必须保留完整 chunk 作 data，
        # 不可取 chunk.get("response") —— tool_call/tool_result 等 chunk
        # 使用 tool/args/result/id 字段，没有 response，取值会丢成空串。
        logger.warning("_map_agent_chunk 未知 type=%r，已降级为 message", t)
        return [("message", chunk)]
    # 已知但未在上面的分支中特化的事件（tool_call / tool_result / message_end / usage）：
    # 保持原始事件名，并透传完整 chunk 作为 data。
    # 前端按 event 名分发（App.vue case 'tool_call' → JSON.parse(data) 取 tool/args/id），
    # 因此这里绝不能改事件名，也不能只取 response 字段。
    return [(str(t or "message"), chunk)]


# ── 后台聊天任务（已抽离到 chat_jobs.py）────────────────────────────
import chat_jobs as _chat_jobs_mod
from chat_jobs import ChatJob, ChatJobManager  # 兼容旧调用与测试
# provider 在 web_server 模块内定义 → 调用时读取本模块 BASE_DIR（测试 monkeypatch 生效）
_chat_jobs_mod._BASE_DIR = BASE_DIR
_chat_jobs_mod._BASE_DIR_PROVIDER = lambda: BASE_DIR
chat_jobs = ChatJobManager()

# ── AgentMain CLI 配置职责（已抽离到 agent_cli_config.py）────────────
from agent_cli_config import CliConfigMixin
import agent_cli_config as _cli_cfg_mod
_cli_cfg_mod.configure(chat_jobs)



from agent_config import ConfigMixin
from agent_session import (  # 兼容旧调用
    SessionMixin,
    _CLAUDE_SID_RE,
    _SESSION_ID_RE,
    _is_claude_session_id,
    _validate_session_id,
    _session_history_mtime,
    _session_has_history,
    _load_session_history,
)


from agent_cli_config import CliConfigMixin


from agent_org import OrgSkillMixin


from agent_workspace import WorkspaceMixin


class AgentMain(ConfigMixin, SessionMixin, CliConfigMixin, OrgSkillMixin, WorkspaceMixin):
    plugin_path = BASE_DIR
    data_path = os.path.join(plugin_path, 'agents_data')

    # 允许通过 set_config 显式清空的字段（清空后运行时回退默认值）
    CLEARABLE_CONFIG_KEYS: ClassVar[set] = {"workspace", "mcp_config_path"}

    DEFAULT_CONFIG: ClassVar[dict] = {
        "api_usage_url": "",
        "default_headers": {
            "uid": "",
            "access-key": "",
            "appid": "app_001"
        },
        "system_prompt": """
     身份定义：
     你是一个AI助手，一个专业、高效且具备运维专项能力的智能伙伴。你不仅精通Linux运维、服务器安全、网站管理，还具备通用的知识问答与辅助能力。
     
     核心准则：
     1. 工具使用：
        - 你拥有执行工具的能力，但前提是用户必须明确启用相关工具。
        - 当发现用户的需求需要特定工具支持，而当前已有工具不足以完成该功能时，需提示用户当前工具无法完成该功能性需求，需提醒用户开启对应工具（如命令执行工具）。
        - 在拥有数据或上下文的情况下，严禁重复调用同一个工具，避免浪费系统资源。
        - 若用户未提供调用工具所需的必填参数（如服务器 IP端口号等），禁止直接调用工具，需主动追问，直至收集到完整、有效的信息；
        
     2. 安全确认：
        - 执行任何涉及修改系统状态、删除数据、重启服务等危险命令前，必须先与用户进行确认。
        - 确认时，清晰说明将要执行的操作、涉及的对象以及可能带来的风险。
        
     3. 真实性与落地：
        - 只提供真实有效的执行结果，绝不捏造数据或执行过程。
        - 如果无法通过工具完成任务，请给出真实可落地的手动操作方案或建议，而不是编造虚假的成功结果。
        
     4. 交互体验：
        - 保持有人情味的对话风格，既专业又平易近人。
        - 在解决运维问题的同时，也能进行日常闲聊和情感互动。
        
     能力范围：
     - 运维专项：Linux系统管理（进程、日志、网络、磁盘）、服务器安全加固、环境部署（LNMP/LAMP）、故障排查。
     - 通用辅助：代码编写、知识解答、文本处理等。
        """,
        "api_base_url": "http://127.0.0.1:8787/v1",
        "api_key": "--",
        "default_model": "",
        # 模型列表与默认模型一律来自 config.json（用户在设置面板配置），不再内置硬编码
        "models": [],
        "embedding": {
            "embedding_api_key": "--",
            "embedding_base_url": "",
            "embedding_model_name": "text-embedding-v4",
        },
        "rag": {
            "sliding_window_size": 15,
            "rag_trigger_threshold": 10,
            "rag_retrieval_count": 10,
            "rag_final_count": 5
        },
        "agent": {
            "max_tool_iterations": 9999,
            "temperature": 0.9,
            "top_p": 0.8,
            "reasoning_effort": "max",
            # Bash（执行命令/代码）工具的默认前台等待时间（秒）。
            # 模型不传 timeout 时用它兜底，超过即转后台；设置页可调整，改完立即生效。
            "bash_timeout": 30,
        },
        "context_window_kb": 512,
        "enable_mcp": True,
        "mcp_config_path": "",
        # MCP 市场源：注入 mcp_manager（URL 列表，默认 GitHub 官方 MCP 市场）
        "mcp_market": [],
        # 工作空间（Agent 执行命令/代码的默认 cwd），默认项目根目录下的 workspace/ 子目录
        "workspace": os.path.join(BASE_DIR, "workspace"),
        # 技能目录，优先从 config.json 读取，其次环境变量 AI_AGENT_SKILLS_DIR，兜底 ~/.claude/skills
        "skills_dir": "",
        # 命令名黑名单：Bash 工具会拒绝执行这些命令（默认空 = 不拦截）。
        # 例如 ["wget", "nc"]。可在前端「设置 → 工具执行行为」中配置。
        "banned_commands": [],
    }

    def __init__(self):
        if not os.path.exists(self.data_path):
            os.makedirs(self.data_path, exist_ok=True)
        self.config_path = os.path.join(self.plugin_path, 'config.json')
        self.config = json.loads(json.dumps(self.DEFAULT_CONFIG))
        user_config = self._load_config()
        self._merge_config(self.config, user_config)
        # 将 config.json 中的 skills_dir 注入 skill_manager，使技能目录可动态切换
        skill_manager.set_skills_dir(self.config.get('skills_dir', ''))
        # 将 config.json 中的 skills_market 注入 skill_manager，支持多市场源
        skill_manager.set_market_config(self.config.get('skills_market', None))
        # 将 config.json 中的 mcp_market 注入 mcp_manager，支持多市场源（默认 GitHub 官方市场）
        from chat_client.mcp_manager import mcp_manager
        mcp_manager.set_market_config(self.config.get('mcp_market', None))
        # 确保默认工作空间目录存在（workspace/）
        try:
            ws = self._resolve_workspace()
            if ws:
                os.makedirs(ws, exist_ok=True)
        except Exception:
            logger.warning("workspace 创建失败，已记录", exc_info=True)


    def sse_pack(self, event=None, id=None, data=None, retry=None):
        lines = []
        if id is not None:
            lines.append(f"id: {id}")
        if event is not None:
            lines.append(f"event: {event}")
        if retry is not None:
            lines.append(f"retry: {retry}")
        if data is not None:
            if isinstance(data, str):
                data = data.replace('\n', '\\n')
                lines.append(f"data: {data}")
            else:
                lines.append(f"data: {json.dumps(data, ensure_ascii=False)}")
        return "\n".join(lines) + "\n\n"


    def _load_prompt_config(self, prompt_id, system_prompt=None):
        prompt_config = {}
        final_system_prompt = system_prompt
        if prompt_id and not final_system_prompt:
            prompts_dir = os.path.join(self.plugin_path, 'prompts')
            if os.path.exists(prompts_dir):
                for ext in ['.md', '.txt']:
                    prompt_file_path = os.path.join(prompts_dir, f"{prompt_id}{ext}")
                    if os.path.exists(prompt_file_path):
                        try:
                            with open(prompt_file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                            if content.startswith('---'):
                                try:
                                    import re

                                    import yaml
                                    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)', content, re.DOTALL)
                                    if match:
                                        frontmatter_str = match.group(1)
                                        final_system_prompt = match.group(2).strip()
                                        try:
                                            parsed_config = yaml.safe_load(frontmatter_str)
                                            if isinstance(parsed_config, dict):
                                                prompt_config.update(parsed_config)
                                        except Exception:
                                            logger.warning("prompt_config JSON 解析失败，已记录", exc_info=True)
                                    else:
                                        final_system_prompt = content
                                except Exception:
                                    final_system_prompt = content
                            else:
                                final_system_prompt = content
                            break
                        except Exception:
                            logger.warning("system_prompt 文件加载异常", exc_info=True)
        if final_system_prompt:
            try:
                current_time = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
                os_version = public.get_os_version()
                final_system_prompt = final_system_prompt.replace('{{CURRENT_TIME}}', current_time)
                final_system_prompt = final_system_prompt.replace('{{OS_VERSION}}', os_version)
            except Exception:
                logger.warning("system_prompt 时间替换失败，已记录", exc_info=True)
        return final_system_prompt, prompt_config

    def _get_priority_value(self, key, get, prompt_config, default=None):
        val = get.get(key)
        if val is not None and val != '':
            return val
        val = prompt_config.get(key)
        if val is not None:
            return val
        return default

    def _resolve_template_system_prompt(self, get, cfg_key='opencode'):
        """
        解析提示词模板，返回最终 system_prompt（或 None）。
        优先级：直接传 system_prompt → 模板选择（自定义 templates / 内置 OPENCODE_TEMPLATES）→ __none__ 显式清空。
        opencode / claude / single 三种模式共用，保证模板选择行为一致。
        cfg_key 指向 config 中的模板配置段（默认 'opencode'，含 templates / default_template）。
        """
        system_prompt = str(get.get('system_prompt', '')).strip() or None
        if system_prompt:
            return system_prompt
        tpl = str(get.get('template', '')).strip() or str(
            (self.config.get(cfg_key, {}) or {}).get('default_template', '')
        ).strip()
        if not tpl or tpl == '__none__':
            return None if tpl == '__none__' else None
        custom_tpls = (self.config.get(cfg_key, {}) or {}).get('templates', {}) or {}
        if tpl in custom_tpls:
            return custom_tpls[tpl]
        # 内置模板
        try:
            from chat_client.opencode_templates import OPENCODE_TEMPLATES
            return OPENCODE_TEMPLATES.get(tpl) or None
        except Exception:
            return None

    def _inject_rag_context(self, get, system_prompt, user_input):
        """
        为 opencode/claude/codex 模式注入全局 RAG 上下文。
        这些模式走子进程桥接，不经过 agent.py 的 RAG 注入逻辑，
        因此需要在此处手动查询 Mem0Service 并拼接到 system_prompt。
        """
        use_global_rag = self._get_priority_value(
            'use_global_rag', get, {}, self.config.get('use_global_rag', False)
        )
        if not use_global_rag:
            return system_prompt

        try:
            from chat_client.retrieval import Mem0Service
            agent_id = self._get_priority_value(
                'global_kb_agent_id', get, {}, self.config.get('global_kb_agent_id', 'ai-agent')
            )
            mem0_url = self._get_priority_value(
                'mem0_api_url', get, {}, self.config.get('mem0_api_url', 'http://localhost:8000')
            )
            rag_final_count = self.config.get('rag', {}).get('rag_final_count', 5)

            mem0 = Mem0Service(
                agent_id=agent_id,
                rag_final_count=rag_final_count,
                mem0_api_url=mem0_url
            )
            docs = mem0.search(user_input)
            mem0.close()

            if docs:
                context_block = "[Global Knowledge Base(for reference only)]:\n" + "\n".join(docs) + "\n\n"
                if system_prompt:
                    system_prompt = system_prompt + "\n\n" + context_block
                else:
                    system_prompt = context_block
                logger.info("[RAG] Global context injected for %s mode, docs count: %d", "opencode/claude/codex", len(docs))

            # 文档知识库注入（CLI 模式同样生效）
            if self.config.get('use_document_kb', True):
                try:
                    from chat_client.knowledge_base import KnowledgeBase, build_embed_fn
                    emb = self.config.get('embedding', {}) or {}
                    emb_key = emb.get('embedding_api_key') or ''
                    emb_url = emb.get('embedding_base_url') or ''
                    if emb_key and emb_url:
                        embed = build_embed_fn(
                            emb_key, emb_url,
                            emb.get('embedding_model_name') or 'text-embedding-3-small',
                            default_headers=self.config.get('default_headers'),
                        )
                        kb = KnowledgeBase(os.path.join(BASE_DIR, 'knowledge'), embed)
                        res = kb.search(user_input, top_k=5, score=0.35)
                        if res.get('status') and res.get('results'):
                            kb_lines = [f"{h['citation']}\n{h['text']}" for h in res['results']]
                            kb_block = "[Document Knowledge Base(for reference only)]:\n" + "\n\n".join(kb_lines) + "\n\n"
                            system_prompt = (system_prompt + "\n\n" + kb_block) if system_prompt else kb_block
                            logger.info("[KB] Document context injected for %s mode, hits: %d",
                                        "opencode/claude/codex", len(res['results']))
                except Exception as e:
                    logger.debug("文档知识库注入失败（已记录）: %s", e)
        except Exception as e:
            logger.debug("RAG 上下文注入失败（已记录）: %s", e)

        return system_prompt

    _IMAGE_EXT_MIME = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }
    _MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 单图 8MB：base64 会放大 ~33%，防撑爆请求

    def _normalize_image_blocks(self, raw_images):
        """把 images 参数归一为 image_url 内容块列表（上游支持该形状）。

        支持三种来源：
          - data URI（data:image/png;base64,...）→ 直接透传
          - http(s) URL → 直接透传，由上游拉取
          - 服务器本地路径（仅限白名单根内，如 uploads/）→ 读盘转 data URI
        越界/超限/不支持的条目跳过并记日志，不影响其余图片。
        """
        blocks = []
        for item in raw_images:
            if isinstance(item, dict):
                url = str(item.get("url") or item.get("path") or "").strip()
            else:
                url = str(item or "").strip()
            if not url:
                continue

            if url.startswith("data:image/"):
                blocks.append({"type": "image_url", "image_url": {"url": url}})
                continue
            if url.startswith(("http://", "https://")):
                blocks.append({"type": "image_url", "image_url": {"url": url}})
                continue

            # 本地路径：必须落在白名单根内，防目录穿越
            try:
                # realpath 解析软链，防白名单根内的软链指向外部文件
                p = os.path.realpath(url if os.path.isabs(url)
                                     else os.path.join(BASE_DIR, url))
                roots = [os.path.realpath(r) for r in __import__("file_routes").get_serve_roots()]
                if not any(p == r or p.startswith(r + os.sep) for r in roots):
                    logger.warning("[安全] 图片路径越界被拒: %s", url)
                    continue
                mime = self._IMAGE_EXT_MIME.get(os.path.splitext(p)[1].lower())
                if not mime:
                    logger.warning("[图片] 不支持的扩展名，已跳过: %s", url)
                    continue
                if os.path.getsize(p) > self._MAX_IMAGE_BYTES:
                    logger.warning("[图片] 超过 %dMB 上限，已跳过: %s",
                                   self._MAX_IMAGE_BYTES // (1024 * 1024), url)
                    continue
                import base64 as _b64
                with open(p, "rb") as f:
                    b64 = _b64.b64encode(f.read()).decode()
                blocks.append({"type": "image_url",
                               "image_url": {"url": f"data:{mime};base64,{b64}"}})
            except OSError:
                logger.warning("[图片] 读取失败，已跳过: %s", url, exc_info=True)
        return blocks

    def _build_chat_agent(self, get):
        """
        构建聊天 Agent（chat 与后台任务共用）。
        返回 (agent, user_input, error)；成功时 error 为 None，
        失败时 agent/user_input 为 None，error 形如 {"event": "error", "data": {"msg": ...}}。
        """
        if self.plugin_path not in sys.path:
            sys.path.append(self.plugin_path)
        try:
            from chat_client.agent import Agent
        except ImportError:
            if self.plugin_path not in sys.path:
                sys.path.insert(0, self.plugin_path)
            from chat_client.agent import Agent

        user_input = get.get('message', '')
        # message 允许是字符串 / JSON 字符串（list/dict）/ None；非字符串类型直接丢弃避免 json.loads 崩溃
        if isinstance(user_input, str):
            try:
                parsed_input = json.loads(user_input)
                if isinstance(parsed_input, list):
                    user_input = parsed_input
                elif isinstance(parsed_input, dict):
                    # 单个 content part（如 {"type":"text","text":"..."}）→ 包装为标准列表格式
                    user_input = [parsed_input]
            except json.JSONDecodeError:
                logger.warning("user_input JSON 解析失败，已记录", exc_info=True)
        elif user_input is not None:
            logger.warning("user_input 非字符串类型: %r", user_input)
            user_input = ''

        # ── 图片（多模态）：images 参数归一后追加到 user_input ────────────
        # 此前图片无处可进（message 只收文本），且历史里的图片块会被
        # _filter_file_blocks 丢弃 → AG 永远"看不见"图。这里打通入口。
        _raw_images = get.get('images', [])
        if isinstance(_raw_images, str) and _raw_images.strip():
            try:
                _raw_images = json.loads(_raw_images)
            except json.JSONDecodeError:
                _raw_images = [_raw_images]
        if isinstance(_raw_images, dict):
            _raw_images = [_raw_images]
        img_blocks = self._normalize_image_blocks(_raw_images) if isinstance(_raw_images, list) else []
        if img_blocks:
            if isinstance(user_input, str):
                user_input = ([{"type": "text", "text": user_input}] if user_input.strip() else [])
            elif not isinstance(user_input, list):
                user_input = [{"type": "text", "text": str(user_input)}]
            else:
                user_input = list(user_input)
            user_input = user_input + img_blocks
            logger.info("[chat_start] 注入 %d 张图片（多模态）", len(img_blocks))

        session_id = get.get('session_id', 'default_session')
        model = get.get('model', '').strip()
        system_prompt = get.get('system_prompt', '')
        prompt_id = get.get('prompt_id', '')
        raw_tools = get.get('tools', [])
        if isinstance(raw_tools, str):
            try:
                raw_tools = ast.literal_eval(raw_tools)
            except (ValueError, SyntaxError):
                raw_tools = []
        if not isinstance(raw_tools, list):
            raw_tools = []
        tools = []
        for t in raw_tools:
            if isinstance(t, str):
                tools.append(t)
            elif isinstance(t, dict) and t.get('id'):
                tools.append(t['id'])
        thinking = get.get('thinking', 'true').lower() == 'true'
        web_search = str(get.get('web_search', 'true')).lower() == 'true'
        workspace_override = get.get('workspace', '').strip()  # 前端可覆盖工作目录

        if not user_input:
            return None, None, {"event": "error", "data": {"msg": "请输入内容"}}

        final_system_prompt, prompt_config = self._load_prompt_config(prompt_id, system_prompt)
        # 集团模式：使用 SOUL/AGENTS/USER/MEMORY 文件组装的设定
        mode = str(get.get('mode', 'group')).strip().lower()
        # 单 Agent / Codex-X 模式：跳过 SOUL/AGENTS/USER/MEMORY 全量加载（会被模板覆盖，浪费 token）
        if not final_system_prompt and mode not in ('single', 'codex_x'):
            final_system_prompt = self._load_agent_context_prompt() or self.config['system_prompt']
        elif not final_system_prompt:
            final_system_prompt = self.config.get('system_prompt', '')
        if not final_system_prompt:
            return None, None, {"event": "error", "data": {"msg": "未找到对应助手配置"}}

        if not model:
            model = prompt_config.get('model_name') or prompt_config.get('model')
        if not model:
            # 回退配置文件：default_model → 勾选列表第一项
            model = self.config.get('default_model') or (self.config.get('models') or [None])[0]

        headers = self.config['default_headers'].copy()
        appid = self._get_priority_value('appid', get, prompt_config, headers.get('appid', ''))
        if appid:
            headers['appid'] = appid

        sessions_dir = self._get_priority_value('sessions_dir', get, prompt_config, 'sessions')

        # 工作模式
        mode = str(get.get('mode', 'group')).strip().lower()
        code_mode = True
        # 单 Agent 模式（single/codex_x）：Agent 直接干活、不编排，使用全套工具
        if mode in ('single', 'codex_x'):
            strict_mode = False
            # 复用 opencode/claude 的模板选择机制，支持提示词模板
            tpl_sp = self._resolve_template_system_prompt(get, cfg_key='opencode')
            if tpl_sp:
                final_system_prompt = tpl_sp
            # 终端渠道兜底人设：single 模式跳过了 SOUL/AGENTS 全量加载，而小程序
            # 不传 system_prompt，若不补则模型无任何角色设定（"裸奔"），回答风格不稳。
            # 显式传入的提示词优先，仅在为空时填充。
            if not final_system_prompt and _is_terminal_channel(get):
                final_system_prompt = _MINIPROGRAM_SINGLE_PERSONA
            # 工具：优先前端传入，否则留空由 agent（strict_tools=False）补全全套默认工具
            tools = raw_tools if raw_tools else []
            
            # 【安全】卡密用户：限制工具 + 强制注入工作目录限制提示词
            if str(session_id).startswith("card_"):
                strict_mode = True  # 防止自动加载全部工具
                tools = ["Task", "Read", "Write", "Glob", "Grep", "WebFetch", "WebSearch"]
                _ws = self._resolve_user_workspace(session_id)
                _card_sp_lines = [
                    "## 安全限制（卡密用户）",
                    f"你只能在工作目录 {_ws} 内读写文件和执行命令。",
                    "禁止操作：",
                    "1. 任何绝对路径超出 {_ws}/ 的文件读写",
                    "2. 任何命令执行（echo/cd/cat/ls等）",
                    "3. 网络请求（curl/wget/web_fetch）",
                    "4. 读取 /etc/passwd、/proc、系统配置文件等敏感信息",
                    "5. 创建符号链接、修改权限、环境变量",
                    "",
                    "正确做法：",
                    f"- 创建文件：在 {_ws}/ 下创建",
                    f"- 读取文件：使用 Read 工具，路径必须相对于 {_ws}/",
                    "- 需要外部数据：请向用户请求，由用户粘贴内容",
                    "",
                    "违反以上限制将导致服务终止。",
                ]
                _card_sp = "\n".join(_card_sp_lines)
                if final_system_prompt:
                    final_system_prompt = final_system_prompt + "\n\n" + _card_sp
                else:
                    final_system_prompt = _card_sp
        else:
            # 集团模式（group，默认）：经理视角，只保留编排类工具；
            # 忽略前端传入的工具列表（防止旧页面全量选择覆盖经理角色），
            # 具体执行由各成员代理用自己的工具完成（Task/RunCrew 分派）
            strict_mode = True
            tools = ["Task", "RunCrew", "CreateDepartment", "RecruitMember"]

        # 工作空间：多用户必须隔离，否则不同用户的文件会互相覆盖/互读。
        #   - 终端渠道（小程序）：一律用该用户的隔离目录，忽略客户端传参，
        #     杜绝通过 workspace 参数指向任意路径（此前无穿越校验）。
        #   - clawbot：服务间调用（服务密钥 + service:clawbot 令牌），参数来自
        #     服务端 config.json 而非终端用户，可信；但指定 hermes 工作目录是
        #     它的硬需求（模板要读该目录下的 SOUL.md/USER.md/MEMORY.md），
        #     故单独放行，且仍走 _clamp_workspace 收敛到允许根内。
        #   - 管理端（网页/CLI）：保留自定义能力，仍做根目录收敛校验。
        if _client_channel(get) == "clawbot" and workspace_override:
            workspace = self._clamp_workspace(
                os.path.abspath(os.path.expanduser(workspace_override)))
        elif _is_terminal_channel(get) or str(session_id).startswith("card_"):
            # 终端渠道 + 卡密用户：强制使用用户隔离目录
            workspace = self._resolve_user_workspace(session_id)
        elif workspace_override:
            workspace = os.path.abspath(os.path.expanduser(workspace_override))
            workspace = self._clamp_workspace(workspace)
        else:
            workspace = self._resolve_workspace(mode)

        agent_config = {
            "mode": mode,
            "api_key": self._get_priority_value('api_key', get, prompt_config, self.config['api_key']),
            "base_url": self._get_priority_value('base_url', get, prompt_config, self.config['api_base_url']),
            "model_name": model,
            "small_model_name": '',
            "default_headers": headers,
            "embedding_api_key": self.config['embedding'].get('embedding_api_key', ''),
            "embedding_base_url": self.config['embedding'].get('embedding_base_url', ''),
            "embedding_model_name": self.config['embedding'].get('embedding_model_name', ''),
            "sliding_window_size": int(self._get_priority_value('sliding_window_size', get, prompt_config, self.config['rag'].get('sliding_window_size', 10))),
            "rag_trigger_threshold": self.config['rag'].get('rag_trigger_threshold', 10),
            "rag_retrieval_count": self.config['rag'].get('rag_retrieval_count', 10),
            "rag_final_count": self.config['rag'].get('rag_final_count', 5),
            "max_tool_iterations": self._get_priority_value('max_tool_iterations', get, prompt_config, self.config['agent'].get('max_tool_iterations', 9999)),
            "context_window_kb": int(self._get_priority_value('context_window_kb', get, prompt_config, self.config.get('context_window_kb', 512))),
            "enable_mcp": self._get_priority_value('enable_mcp', get, prompt_config, self.config.get('enable_mcp', True)),
            "mcp_config_path": self._get_priority_value('mcp_config_path', get, prompt_config, self.config.get('mcp_config_path', '')),
            "tools": tools,
            "code_mode": code_mode,
            "strict_tools": strict_mode,
            "cwd": workspace,
            "workspace": workspace,
            "base_dir": BASE_DIR,
            "system_prompt": final_system_prompt,
            "temperature": float(self._get_priority_value('temperature', get, prompt_config, self.config['agent'].get('temperature', 1.0))),
            "top_p": float(self._get_priority_value('top_p', get, prompt_config, self.config['agent'].get('top_p', 1.0))),
            "reasoning_effort": str(get.get('reasoning_effort', self.config['agent'].get('reasoning_effort', 'high'))).strip().lower() or 'high',
            "api_protocol": str(self._get_priority_value('api_protocol', get, prompt_config, self.config.get('api_protocol') or self.config['agent'].get('api_protocol', 'chat'))).strip().lower() or 'chat',
            "thinking": thinking,
            "web_search": web_search,
            "sessions_dir": os.path.join(self.plugin_path, sessions_dir),
            "global_kb_dir": os.path.join(self.plugin_path, 'memo'),
            "use_global_rag": self._get_priority_value('use_global_rag', get, prompt_config, self.config.get('use_global_rag', False)),
            "use_external_kb": self._get_priority_value('use_external_kb', get, prompt_config, self.config.get('use_external_kb', False)),
            "use_document_kb": self._get_priority_value('use_document_kb', get, prompt_config, self.config.get('use_document_kb', True)),
            "document_kb_top_k": self.config.get('document_kb_top_k', 5),
            "document_kb_dir": self.config.get('document_kb_dir', ''),
            "global_kb_agent_id": self._get_priority_value('global_kb_agent_id', get, prompt_config, 'ai-agent'),
            "mem0_api_url": self._get_priority_value('mem0_api_url', get, prompt_config, 'http://localhost:8000'),
            "api_max_retry": int(self.config['agent'].get('api_max_retry', 3)),
            "api_retry_base_wait": float(self.config['agent'].get('api_retry_base_wait', 1)),
            "api_retry_max_wait": float(self.config['agent'].get('api_retry_max_wait', 10)),
        }

        # 经理人格注入：让模型知道自己是谁、有什么团队、必须分派而非亲自动手
        if strict_mode:
            from chat_client.tools.task import agent_registry as _ar
            dept_title = {d["name"]: d["title"] for d in getattr(_ar, "departments", [])}
            groups = {}
            for a in _ar.list_agents():
                groups.setdefault(a.department or "general", []).append(a)
            org_lines = []
            for dn, ms in groups.items():
                t = dept_title.get(dn, "综合部" if dn == "general" else dn)
                org_lines.append(f"【{t}】")
                org_lines += [f"- {a.name}：{a.description}" for a in ms]
            # 身份/人格部分优先从 SOUL/AGENTS/USER/MEMORY 加载，动态的组织架构与工具列表保留在代码中
            ctx = self._load_agent_context_prompt()
            base_persona = ctx or "你是「AI 集团」的总经理。对面是用户（会话人）——他下达目标——他只下达目标，不关心内部过程。"
            # 注入技能摘要（与单 Agent 模式同源），让经理在派任务时知道有哪些 skill 可用，
            # 以便在 Task/RunCrew 的 prompt 中指示成员加载对应技能执行
            try:
                from chat_client.skills import skill_manager
                skill_summary = skill_manager.generate_skill_summary()
            except Exception:
                skill_summary = ""
            # P5: 注入本地专家库摘要（自动发现），供经理派任务时选用
            try:
                from chat_client.experts import get_catalog
                expert_summary = get_catalog(BASE_DIR).generate_expert_summary()
            except Exception:
                expert_summary = ""
            # 注入 MCP 工具清单，让经理知道成员可通过 MCP 调用的外部能力
            mcp_lines = []
            try:
                from chat_client.mcp_client import get_mcp_client
                mcp_client = get_mcp_client()
                if mcp_client.is_loaded():
                    for schema in mcp_client.get_tool_schemas():
                        tid = schema["function"]["name"]
                        desc = schema.get("function", {}).get("description", "")[:60]
                        mcp_lines.append(f"- {tid}：{desc}")
                else:
                    # MCP 尚未加载完成：至少列出已配置的 server 名，避免经理完全不知道外部能力
                    from chat_client.mcp_manager import mcp_manager
                    _servers = mcp_manager.list_servers().get('items', []) if hasattr(mcp_manager, 'list_servers') else []
                    if _servers:
                        mcp_lines.append("（MCP 服务器加载中，以下是已配置的外部服务）")
                        for _s in _servers[:20]:
                            _n = _s.get('name', '') if isinstance(_s, dict) else str(_s)
                            mcp_lines.append(f"- {_n}")
            except Exception:
                mcp_lines = []
            manager_sp = base_persona + f"""

【你的团队组织架构】
{chr(10).join(org_lines)}

【你可用的编排工具】
- RunCrew(objective, agents?, max_steps?): 推荐。多部门协作完成一个目标，经理自动拆解计划并分派。
- Task(description, prompt, subagent_type, expected_output?, model?): 把单个任务委派给指定成员。
- CreateDepartment / RecruitMember: 组织扩张（成立新部门/招聘新成员）。

【成员可用的技能与工具】
你本人不直接执行任务，但要知道团队有哪些能力，以便在派任务时指示成员使用：
{skill_summary or '（暂无技能摘要）'}
{f"""{expert_summary}
""" if expert_summary else ""}
{f"""[MCP 外部工具（成员可调用）]
{chr(10).join(mcp_lines)}
""" if mcp_lines else ""}
派任务时，如果某目标需要特定技能，在 Task/RunCrew 的 prompt 中写明「使用 Skills(name='技能名') 加载对应技能」或直接调用对应 MCP 工具。
若目标匹配上方【本地专家库】中的某位专家，可用 InstallExpert(name='专家id') 落地为团队成员，或指示成员调用 GetExpertPrompt 取人设。

【专家自动派发（强制，优先级高于自行处理）】
本地高频映射（命中即派，不得自己或普通成员代劳）：
- 代码审查 / review / 查bug / 安全审查 / 重构评估 → `code-reviewer`
- 运维 / 部署 / 排障 / 服务器 / systemd / nginx / docker / 日志诊断 / 磁盘网络 → `ops-engineer`
- 数据分析 / excel / csv / 统计 / 报表 / 透视 / 可视化 / 表格清洗 → `data-analyst`
- 文案 / 写文章 / 公众号 / 小红书 / 知乎 / SEO 内容 / 标题 / 改写 → `content-writer`
- 美团 / 领券 / 优惠券 / 团购 / 外卖 / 附近美食 / 点餐 → `meituan-living-assistant`

**本表不是全集。** 任务属于其他专业领域（设计/法务/财务/营销/产品/架构/性能/安全/翻译/医疗/教育…）且本地无对应专家时，
必须 `ExpertMarket(keyword='领域词')` 检索 → `InstallExpert(name='命中id')` 落地 → `Task` 派发；
检索为空才可退化用通用成员，并在交付时说明「未找到匹配专家，已用通用成员处理」。

流程：已是团队成员 → 直接 `Task(subagent_type='专家名', ...)`；本地专家库有但未落地 → `InstallExpert` 后派发；本地无 → `ExpertMarket` 搜 → 落地 → 派发。
非专业领域（闲聊/通用问答/纯状态查询）才允许自行处理。交付前自查：属专业领域却没派专家、或本地无专家却没查市场 = 违规。

【技能使用铁律（必须遵守）】
1. 拆解任务的第一步，先在上方技能清单中查找匹配技能（比对"遇到以下情况就使用/适用场景/触发词"段落）。
2. 只要找到匹配技能，Task/RunCrew 的 prompt 中必须写明「先调用 Skills(name='技能名') 加载完整工作流，再按技能流程执行」——禁止让成员用通用手段硬干。
3. 领域-技能映射速查：excel/数据分析→数据分析类；ppt/演示→PPT类；seo/排名→SEO类；浏览器/爬虫→Web类；逆向/安全→安全类；微信/图片/视频→社媒类；运维/部署→DevOps类；写代码→编程语言类；图片识别/OCR→SN技能。
4. 一个任务可匹配多技能时，指示成员加载 1 个主技能 + 必要时 1 个辅助技能。
5. 清单中确实无匹配项，才允许成员用通用工具（Bash/Read/Write）执行。

【市场发现工具（派任务时可指示成员调用）】
- 专家市场：429 个领域专家（含完整人设提示词）。成员需要专业角色/人设时，
  用 `ExpertMarket(keyword=...)` 搜索 + `GetExpertPrompt(name=...)` 取提示词。
- 技能市场：`SkillMarket(keyword=...)` / `SkillCategories()` 探索社区技能。
- 精选场景：`ExpertScenes()`。
- 派任务时在 Task/RunCrew prompt 中写明：「先调用 ExpertMarket/SkillMarket 查找相关专家或技能，再按需加载」。

【工作原则】
1. 凡涉及实际操作——读写文件、执行命令、查询系统、抓取网页、分析日志等——必须通过上述工具**分派给对应成员**执行，
   严禁在没有工具结果的情况下自行编造过程或结论。
2. 简单问候、常识问答可不经分派直接回答。
3. 多环节/跨领域目标优先使用 RunCrew——鼓励跨部门流水线：
   调研部搜集 → 研发部实现 → 测试部验证 → 审核部把关。
   成员之间可通过 ConsultPeer 互相咨询，无需经过你中转。
4. 高风险操作会被安全策略拦截确认：此时向会话人说明将要执行的操作与风险，等待明确同意。
5. 交付时面向会话人汇报：结论先行、要点清晰、附关键证据。"""

            # 小程序渠道：不向用户暴露网关内部信息。
            # 实测发现经理会把工作目录（/home/sxkiss/bt）、技能数量、部门清单、
            # MCP 服务名等原样写进自我介绍，等于把服务端部署细节泄露给终端用户。
            # 这里对 miniprogram 渠道追加一段输出约束（只约束"说什么"，不削弱编排能力）。
            if _is_terminal_channel(get):
                manager_sp = _append_miniprogram_guard(manager_sp)
            agent_config["system_prompt"] = manager_sp

        elif _is_terminal_channel(get):
            # 单 Agent 模式的小程序渠道同样需要输出约束。
            # 此前该约束只加在集团模式分支，切到 single 后泄露防护会静默失效。
            agent_config["system_prompt"] = _append_miniprogram_guard(
                agent_config.get("system_prompt", "")
            )

        try:
            agent = Agent(session_id=session_id, config=agent_config)
        except Exception as e:
            return None, None, {"event": "error", "data": {"msg": f"Agent 初始化失败: {e!s}"}}
        return agent, user_input, None

    def chat(self, get):
        agent, user_input, err = self._build_chat_agent(get)
        if err:
            yield self.sse_pack(event=err["event"], data=err["data"])
            return

        try:
            for chunk in agent.chat(user_input):
                for ev, data in _map_agent_chunk(chunk):
                    yield self.sse_pack(event=ev, data=data)
            yield self.sse_pack(event="message_end")
        except Exception as e:
            yield self.sse_pack(event="error", data={"msg": f"聊天发生错误: {e!s}"})
        finally:
            try:
                agent.close()
            except Exception:
                logger.warning("chat agent.close 失败，已记录", exc_info=True)

    # ---------------- 后台任务式聊天（与前端连接解耦） ----------------

    def chat_start(self, get):
        """
        启动后台聊天任务，立即返回。
        任务在后端线程中运行，前端断开/刷新均不影响；通过 /api/chat/events 订阅事件流。
        """
        session_id = get.get('session_id', 'default_session')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")

        mode = str(get.get('mode', '')).strip().lower()
        user_input = str(get.get('message', '')).strip()
        if not user_input:
            return public.returnMsg(False, "缺少参数 message")

        label = str(user_input)[:36]
        job = chat_jobs.create_if_idle(session_id, label)
        if job is None:
            return public.returnMsg(
                False,
                f"该会话并行任务已满（{chat_jobs.MAX_PARALLEL}），等待任一任务完成后自动继续派发",
            )

        # ── per-thread HITL 审批模式（P1，2026-10-15）──────────────────
        # 请求可携带 hitl（ask/allow_all/allow_tools）+ hitl_tools（allow_tools 白名单）。
        # 会话级覆盖，作用于本次及后续该会话的高风险工具放行判断。
        try:
            _hitl = str(get.get('hitl', '') or '').strip().lower()
            if _hitl in ('ask', 'allow_all', 'allow_tools'):
                from chat_client.tools import set_session_hitl
                _hitl_tools = get.get('hitl_tools') or []
                if isinstance(_hitl_tools, str):
                    _hitl_tools = [t.strip() for t in _hitl_tools.split(',') if t.strip()]
                set_session_hitl(session_id, _hitl, _hitl_tools)
                logger.info("[chat_start] HITL 模式=%s tools=%s session=%s", _hitl, _hitl_tools, session_id)
        except Exception:
            logger.warning("[chat_start] 设置 HITL 模式失败", exc_info=True)

        # ── opencode 模式 ────────────────────────────────────────────
        if mode == 'opencode':
            from chat_client.opencode_bridge import run_opencode_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'

            # 提示词：system_prompt 直接传 → 模板选择 → 配置默认（三模式共用）
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')
            # RAG 上下文注入（全局 Mem0，适用于子进程模式）
            system_prompt = self._inject_rag_context(get, system_prompt, user_input)

            workspace = self._resolve_request_workspace(get, session_id)
            reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
            # 自定义 API 配置（优先前端传入，回退全局配置）
            custom_base_url = str(get.get('base_url', '')).strip() or self.config.get('api_base_url', '')
            custom_api_key = str(get.get('api_key', '')).strip() or self.config.get('api_key', '')
            # 持久化 workspace（CLI 自家 DB 可能不记录前端传入的 workspace）
            try:
                if workspace:
                    _md = os.path.join(self.plugin_path, 'sessions', session_id)
                    os.makedirs(_md, exist_ok=True)
                    _mf = os.path.join(_md, 'meta.json')
                    _m = {}
                    if os.path.exists(_mf):
                        try:
                            with open(_mf, 'r', encoding='utf-8') as _f:
                                _m = json.load(_f) or {}
                        except Exception:
                            _m = {}
                    _m['workspace'] = workspace
                    _m['source'] = 'opencode'
                    with open(_mf, 'w', encoding='utf-8') as _f:
                        json.dump(_m, _f, ensure_ascii=False)
            except Exception:
                logger.warning("opencode 模式持久化 workspace 到 meta.json 失败", exc_info=True)
            t = threading.Thread(
                target=run_opencode_chat,
                args=(session_id, user_input, job, model, system_prompt, workspace, reasoning_effort, custom_base_url, custom_api_key),
                name=f"oc-chat-{job.key}",
                daemon=True,
            )
            t.start()
            logger.info("[chat_start][opencode] tpl=%r sp=%r ws=%r base=%s", get.get('template', ''), (system_prompt or '')[:60], workspace, custom_base_url)
            return public.return_data(True, data={
                "session_id": session_id,
                "job": job.key,
                "started": True,
            })

        # ── claude 模式 ──────────────────────────────────────────────
        if mode == 'claude':
            from chat_client.claude_bridge import run_claude_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'

            # 提示词：system_prompt 直接传 → 模板选择 → 配置默认（三模式共用）
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')
            # RAG 上下文注入（全局 Mem0，适用于子进程模式）
            system_prompt = self._inject_rag_context(get, system_prompt, user_input)

            workspace = self._resolve_request_workspace(get, session_id)
            reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
            # 自定义 API 配置（优先前端传入，回退全局配置）
            custom_base_url = str(get.get('base_url', '')).strip() or self.config.get('api_base_url', '')
            custom_api_key = str(get.get('api_key', '')).strip() or self.config.get('api_key', '')
            try:
                if workspace:
                    _md = os.path.join(self.plugin_path, 'sessions', session_id)
                    os.makedirs(_md, exist_ok=True)
                    _mf = os.path.join(_md, 'meta.json')
                    _m = {}
                    if os.path.exists(_mf):
                        try:
                            with open(_mf, 'r', encoding='utf-8') as _f:
                                _m = json.load(_f) or {}
                        except Exception:
                            _m = {}
                    _m['workspace'] = workspace
                    _m['source'] = 'claude'
                    with open(_mf, 'w', encoding='utf-8') as _f:
                        json.dump(_m, _f, ensure_ascii=False)
            except Exception:
                logger.warning("claude 模式持久化 workspace 到 meta.json 失败", exc_info=True)
            t = threading.Thread(
                target=run_claude_chat,
                args=(session_id, user_input, job, model, system_prompt, workspace, reasoning_effort, custom_base_url, custom_api_key),
                name=f"claude-chat-{job.key}",
                daemon=True,
            )
            t.start()
            logger.info("[chat_start][claude] sp=%r ws=%r base=%s", (system_prompt or '')[:60], workspace, custom_base_url)
            return public.return_data(True, data={
                "session_id": session_id,
                "job": job.key,
                "started": True,
            })

        # ── codex 模式 ──────────────────────────────────────────────
        if mode == 'codex':
            from chat_client.codex_bridge import run_codex_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')
            # RAG 上下文注入（全局 Mem0，适用于子进程模式）
            system_prompt = self._inject_rag_context(get, system_prompt, user_input)
            workspace = self._resolve_request_workspace(get, session_id)
            reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
            custom_base_url = str(get.get('base_url', '')).strip() or self.config.get('api_base_url', '')
            custom_api_key = str(get.get('api_key', '')).strip() or self.config.get('api_key', '')
            try:
                if workspace:
                    _md = os.path.join(self.plugin_path, 'sessions', session_id)
                    os.makedirs(_md, exist_ok=True)
                    _mf = os.path.join(_md, 'meta.json')
                    _m = {}
                    if os.path.exists(_mf):
                        try:
                            with open(_mf, 'r', encoding='utf-8') as _f:
                                _m = json.load(_f) or {}
                        except Exception:
                            _m = {}
                    _m['workspace'] = workspace
                    _m['source'] = 'codex'
                    with open(_mf, 'w', encoding='utf-8') as _f:
                        json.dump(_m, _f, ensure_ascii=False)
            except Exception:
                logger.warning("codex 模式持久化 workspace 到 meta.json 失败", exc_info=True)
            t = threading.Thread(
                target=run_codex_chat,
                args=(session_id, user_input, job, model, system_prompt, workspace, reasoning_effort, custom_base_url, custom_api_key),
                name=f"codex-chat-{job.key}",
                daemon=True,
            )
            t.start()
            logger.info("[chat_start][codex] sp=%r ws=%r base=%s", (system_prompt or '')[:60], workspace, custom_base_url)
            return public.return_data(True, data={
                "session_id": session_id,
                "job": job.key,
                "started": True,
            })

        # ── native 模式（默认） ──────────────────────────────────────
        _t0 = time.time()
        agent, user_input, err = self._build_chat_agent(get)
        logger.info(f"[chat_start] _build_chat_agent took {time.time()-_t0:.2f}s")
        if err:
            chat_jobs.discard(job.key)
            return public.returnMsg(False, err["data"]["msg"])

        # 持久化 workspace 到 sessions/<id>/meta.json，供历史列表按工作目录分组
        try:
            _ws = (agent.config or {}).get('workspace', '') or ''
            if _ws:
                _meta_dir = os.path.join(self.plugin_path, 'sessions', session_id)
                os.makedirs(_meta_dir, exist_ok=True)
                _meta_file = os.path.join(_meta_dir, 'meta.json')
                _meta_data = {}
                if os.path.exists(_meta_file):
                    try:
                        with open(_meta_file, 'r', encoding='utf-8') as _mf:
                            _meta_data = json.load(_mf) or {}
                    except Exception:
                        _meta_data = {}
                _meta_data['workspace'] = _ws
                _meta_data['source'] = 'single'
                with open(_meta_file, 'w', encoding='utf-8') as _mf:
                    json.dump(_meta_data, _mf, ensure_ascii=False)
        except Exception:
            logger.warning("native 模式持久化 workspace 到 meta.json 失败", exc_info=True)

        job.attach_agent(agent)
        t = threading.Thread(
            target=self._run_chat_job,
            args=(job, agent, user_input),
            name=f"chat-job-{job.key}",
            daemon=True,
        )
        t.start()
        # 渠道日志：此前小程序请求在日志里与网页/CLI 完全无法区分，
        # 只能靠 jobs/ 下 wx_ 前缀反推。这里显式记录渠道，便于排查与审计。
        logger.info(
            "[chat_start] channel=%s session=%s mode=%s job=%s",
            _client_channel(get) or "web",
            session_id, get.get("mode", "") or "group", job.key,
        )
        return public.return_data(True, data={
            "session_id": session_id,
            "job": job.key,
            "started": True,
        })

    def _run_chat_job(self, job, agent, user_input):
        """后台线程：运行 agent.chat() 并把 chunk 写入 job 事件缓存"""
        from chat_client.tools import set_current_job
        set_current_job(job)  # 工具内可通过 emit_progress 推送实时进度
        try:
            for chunk in agent.chat(user_input):
                # 工具调用/结果等透传事件需要完整 chunk
                for ev, data in _map_agent_chunk(chunk):
                    job.append(ev, data)
            job.append("message_end", None)
            job.finish("done")
        except Exception as e:
            job.append("error", {"msg": f"聊天发生错误: {e!s}"})
            job.append("message_end", None)
            job.finish("error")
        finally:
            try:
                agent.close()
            except Exception:
                logger.warning("chat_stop agent.close 失败，已记录", exc_info=True)

    async def chat_events(self, get):
        """
        SSE 事件流：先回放 last_id 之后的事件，再实时跟随，直到任务终态。
        前端断开后可带 last_id 重连续传，后端任务不受影响。
        """
        ok, sid = _validate_session_id(get.get('session_id'))
        if not ok:
            yield self.sse_pack(event="error", data={"msg": sid})
            return
        session_id = sid
        job_key = get.get('job', '')
        if job_key:
            job = chat_jobs.get(job_key)
        else:
            # 兼容：取该会话最新的任务
            act = chat_jobs.active_for_session(session_id)
            job = act[-1][1] if act else None
        if not job:
            yield self.sse_pack(event="error", data={"msg": "没有可订阅的任务（不存在或已过期）"})
            return

        # 渠道日志：SSE 订阅同样记录来源，便于小程序/clawbot 断流/续传问题定位
        _mp = _is_terminal_channel(get)
        logger.info(
            "[chat_events] channel=%s session=%s job=%s last_id=%s",
            _client_channel(get) or "web",
            session_id, job.key, get.get('last_id', -1),
        )
        # 小程序（终端用户）剥掉思维链等内部事件；网页管理端保留全部
        drop = HIDDEN_EVENTS_FOR_TERMINAL if _mp else None

        try:
            last_id = int(get.get('last_id', -1))
        except (TypeError, ValueError):
            last_id = -1

        sent_end = False
        idle_ticks = 0  # 空闲 tick 计数，用于 SSE keepalive，防止客户端 sock_read 超时断开
        # P2-38: SSE 超时可配置，默认 30min（原 900s=15min 对长任务不够）
        max_idle_seconds = int(os.environ.get("SSE_IDLE_TIMEOUT", "1800"))
        idle_start = asyncio.get_event_loop().time()
        # P1-75: message_end 后继续挂一小段时间（默认 10s），让后台任务结果
        # 有机会通过孤儿回调/notify_done 注入到同一个 job，前端能一次性收到。
        _ORPHAN_APPEND_WINDOW = float(os.environ.get("AI_AGENT_ORPHAN_APPEND_WINDOW", "10"))
        _post_end_deadline = None
        while True:
            pending, status = job.snapshot_from(last_id, drop)
            for e in pending:
                yield self.sse_pack(event=e["event"], id=e["id"], data=e["data"])
                last_id = e["id"]
                if e["event"] == "message_end":
                    sent_end = True
                    # 记录 message_end 到达时间，后续窗口期以此时为基准
                    _post_end_deadline = asyncio.get_event_loop().time() + _ORPHAN_APPEND_WINDOW
            # 判断是否退出：job 已结束 且 无新事件 且 过了追加窗口
            if status != "running" and not pending:
                if _post_end_deadline is None:
                    # 续传场景：last_id 已越过 message_end，本轮不会再看到该事件，
                    # 窗口计时永远无法启动。若不在此直接退出，循环会一直挂到
                    # max_idle_seconds（900s）才超时——表现为"续传后客户端卡死"。
                    break
                if asyncio.get_event_loop().time() >= _post_end_deadline:
                    break
            await asyncio.sleep(0.15)
            # 空闲 keepalive：任务运行中但暂时无新事件（模型思考/工具执行中），
            # 每 ~10s 发一条 SSE 注释行维持连接，避免客户端 aiohttp sock_read 超时断开
            idle_ticks += 1
            if not pending and idle_ticks >= 66:  # 0.15s * 66 ≈ 10s
                elapsed = asyncio.get_event_loop().time() - idle_start
                if elapsed >= max_idle_seconds:
                    yield self.sse_pack(event="error", data={"msg": f"SSE 订阅超时（{max_idle_seconds}s），请刷新页面重试"})
                    return
                yield ": keepalive\n\n"
                idle_ticks = 0

        # 兜底：确保前端一定收到结束信号
        if not sent_end:
            if status == "stopped":
                yield self.sse_pack(event="error", data={"msg": "任务已停止"})
            yield self.sse_pack(event="message_end", id=last_id + 1)

    def chat_status(self, get):
        ok, sid = _validate_session_id(get.get('session_id'))
        if not ok:
            return public.returnMsg(False, sid)
        session_id = sid
        jobs = chat_jobs.active_for_session(session_id)
        if not jobs:
            return public.return_data(True, data={"running": False, "status": "none",
                                                  "last_id": -1, "elapsed": 0, "jobs": []})
        jobs_list = []
        for key, j in sorted(jobs, key=lambda kv: kv[1].created_at):
            with j._lock:
                # 必须用单调递增的 _seq，不能用 len(events)-1：截断后 len 恒等于
                # _MAX_EVENTS，会把 last_id 永远上报成上限值，前端据此续传即卡死。
                lid = j._seq
                st = j.status
            jobs_list.append({
                "job": key,
                "label": j.label,
                "status": st,
                "running": j.is_running,
                "last_id": lid,
                "started_at": int(j.created_at),
                "elapsed": max(0, int(time.time() - j.created_at)),
            })
        newest = jobs_list[-1]
        # 顶层 elapsed：取仍在运行的任务中耗时最长者；若都已结束则取最新任务
        running_elapsed = [j["elapsed"] for j in jobs_list if j["running"]]
        elapsed_top = max(running_elapsed) if running_elapsed else newest["elapsed"]
        return public.return_data(True, data={
            "running": any(x["running"] for x in jobs_list),
            "status": newest["status"],
            "last_id": newest["last_id"],
            "elapsed": elapsed_top,
            "jobs": jobs_list,
        })

    def chat_stop(self, get):
        ok, sid = _validate_session_id(get.get('session_id'))
        if not ok:
            return public.returnMsg(False, sid)
        session_id = sid
        job_key = str(get.get('job', '') or '').strip()
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        targets = chat_jobs.stop(key=job_key or None, session_id=None if job_key else session_id)
        if not targets:
            return public.returnMsg(False, "没有可停止的任务")
        return public.return_data(True, data={"stopped": len(targets)})

    def set_tool_show_status(self, get):
        """切换工具前端显示状态（按 id 或 category）"""
        tool_id = get.get('tool_id')
        category = get.get('category')
        show = str(get.get('show', 'True')).lower() == 'true'
        if not tool_id and not category:
            return public.return_data(False, '参数错误，缺少 tool_id 或 category')
        res = registry.set_tool_show_status(tool_id=tool_id, show=show, category=category)
        if res:
            return public.return_data(True, '设置成功')
        return public.return_data(False, '设置失败')

    def set_skill_status(self, get):
        """切换技能启用状态"""
        skill_name = (get.get('skill_name') or '').strip()
        enabled_raw = str(get.get('enabled', '')).strip().lower()
        if not skill_name:
            return public.returnMsg(False, '缺少参数 skill_name')
        if enabled_raw not in ['true', 'false', '1', '0']:
            return public.returnMsg(False, '参数 enabled 格式错误，必须是 true/false')
        enabled = enabled_raw in ['true', '1']
        result = skill_manager.set_skill_enabled(skill_name, enabled)
        if not result.get("status"):
            return public.returnMsg(False, result.get("msg", "设置失败"))
        return public.returnMsg(True, result.get("msg", "设置成功"))

    def run_agent(self, get):
        if self.plugin_path not in sys.path:
            sys.path.append(self.plugin_path)
        try:
            from agents.base import BaseAgent
        except ImportError:
            if self.plugin_path not in sys.path:
                sys.path.insert(0, self.plugin_path)
            from agents.base import BaseAgent

        agent_id = get.get('agent_id')
        if not agent_id:
            yield self.sse_pack(event="error", data={"msg": "缺少参数 agent_id"})
            return

        agents_config, error = self._load_agents_config()
        if error:
            yield self.sse_pack(event="error", data={"msg": error})
            return

        target_agent_config = None
        for agent in agents_config:
            if agent.get('id') == agent_id or agent.get('agent') == agent_id:
                target_agent_config = agent
                break

        if not target_agent_config:
            yield self.sse_pack(event="error", data={"msg": f"{agent_id} 助手不存在"})
            return

        agent_name = target_agent_config['agent']
        # P2-25: 正则白名单，禁止 .. / / 等特殊字符，防止 importlib 导入任意模块
        if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]{0,63}$', agent_name):
            yield self.sse_pack(event="error", data={"msg": f"助手名称 '{agent_name}' 包含非法字符"})
            return
        module_name = f"agents.{agent_name}"

        try:
            if module_name in sys.modules:
                module = sys.modules[module_name]
            else:
                module = importlib.import_module(module_name)
        except ImportError as e:
            yield self.sse_pack(event="error", data={"msg": f"无法导入模块 {module_name}: {e!s}"})
            return

        agent_class = None
        for name, obj in inspect.getmembers(module):
            if inspect.isclass(obj) and issubclass(obj, BaseAgent) and obj is not BaseAgent:
                agent_class = obj
                break

        if not agent_class:
            yield self.sse_pack(event="error", data={"msg": f"找不到助手 {module_name}"})
            return

        registered_args_def = target_agent_config.get('args', {})
        agent_args = {}
        for key, info in registered_args_def.items():
            if key in get:
                agent_args[key] = get[key]
            elif isinstance(info, dict) and info.get('required'):
                yield self.sse_pack(event="error", data={"msg": f"缺少助手所需参数: {key}"})
                return

        try:
            agent_instance = agent_class(
                api_key=self.config['api_key'],
                base_url=self.config['api_base_url'],
                model=self.config['default_model'],
                agent_args=agent_args,
                headers=self.config['default_headers']
            )
            generator = agent_instance.run()
        except Exception as e:
            yield self.sse_pack(event="error", data={"msg": f"Failed to initialize agent: {e!s}"})
            return

        result_dir = os.path.join(self.data_path, agent_id)
        os.makedirs(result_dir, exist_ok=True)

        full_content = []
        import time
        try:
            for chunk in generator:
                if chunk["type"] == "content":
                    yield self.sse_pack(event="message", data={"response": chunk.get("response", "")})
                elif chunk["type"] == "reasoning":
                    yield self.sse_pack(event="message_think", data={"response": chunk.get("response", "")})
                elif chunk["type"] == "error":
                    yield self.sse_pack(event="error", data={"msg": chunk.get("data", "")})
                elif chunk["type"] == "stop":
                    yield self.sse_pack(event="usage", data={"usage": chunk.get("usage", {})})
                full_content.append(chunk)

            timestamp = int(time.time())
            save_path = os.path.join(result_dir, f'{timestamp}.json')
            record = {"agent_id": agent_id, "agent_name": agent_name, "timestamp": timestamp, "args": agent_args, "result": full_content}
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            yield self.sse_pack(event="message_end", data={"response": ""})
        except Exception as e:
            yield self.sse_pack(event="error", data={"msg": f"Error during execution: {e!s}"})
        finally:
            agent_instance.close()


# ============================================================
# FastAPI App
# ============================================================

async def _periodic_job_cleanup():
    """周期性回收 jobs/ 下过期文件 + 内存中已终结的僵尸任务。

    服务长期运行会持续写入 jobs/（磁盘）并在 _jobs 字典驻留已终结 job
    （内存）。两者都是同步阻塞 I/O / CPU 操作，统一用 to_thread 丢到线程池。
    """
    # 磁盘清理间隔：由 JOB_FILE_TTL 推导（TTL 的 1/4，1h~24h）
    file_interval = ChatJobManager.JOB_FILE_TTL
    # 内存回收间隔：独立且更短（默认 1 小时），与磁盘 TTL 解耦，
    # 因为内存里"对话后不再活跃"的僵尸任务需要更快被回收，而非等 7 天。
    mem_interval = int(os.environ.get("AI_AGENT_MEMORY_PRUNE_INTERVAL", str(3600)))
    if file_interval > 0:
        file_interval = max(3600, min(24 * 3600, file_interval // 4))
    else:
        # 磁盘清理被禁用时，仍保证内存回收有独立的合理间隔
        file_interval = mem_interval
    # 内存回收单批上限（避免一次性清上千任务卡住）
    mem_batch = ChatJobManager.MEMORY_PRUNE_BATCH
    while True:
        try:
            await asyncio.sleep(mem_interval)
            # 内存回收：释放已终结僵尸 job 的 agent/events，让 GC 回收历史
            await asyncio.to_thread(chat_jobs.prune_memory_jobs, mem_batch)
            # 磁盘回收：与内存回收同频即可（file_interval 已 ≥ mem_interval
            # 的合理值），文件清理不要求精确频率
            if ChatJobManager.JOB_FILE_TTL > 0:
                await asyncio.to_thread(chat_jobs.prune_job_files)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[ChatJob] 周期性清理失败", exc_info=True)


async def _periodic_session_cleanup():
    """周期性回收 sessions/ 下超过 3 天的会话目录。

    prune_sessions 是同步阻塞 I/O（listdir + stat + rmtree），用 to_thread
    丢到线程池，避免卡住事件循环。清理间隔取 TTL 的 1/4（约 18 小时），
    但不少于 1 小时、不超过 24 小时。
    """
    ttl = ChatJobManager.SESSION_TTL
    if ttl <= 0:
        return
    interval = max(3600, min(24 * 3600, ttl // 4))
    while True:
        try:
            await asyncio.sleep(interval)
            await asyncio.to_thread(chat_jobs.prune_sessions)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("[ChatJob] 周期性会话清理失败", exc_info=True)


@contextlib.asynccontextmanager
async def _lifespan(_app):
    """启动/退出钩子。

    服务以 `uvicorn web_server:app` 运行，模块级 `if __name__ == '__main__'`
    不会执行，因此 jobs/ 的磁盘清理必须挂在这里才能生效。
    """
    try:
        chat_jobs.prune_job_files()
    except Exception:
        logger.warning("[startup] jobs/ 清理失败", exc_info=True)
    try:
        chat_jobs.recover_all()
    except Exception:
        logger.warning("[startup] job 恢复失败", exc_info=True)
    # 恢复后台任务（进程重启后未完成的任务）
    try:
        from chat_client.tools import recover_running_tasks
        recovered = recover_running_tasks()
        if recovered:
            logger.info("[startup] 恢复 %d 个未完成的后台任务", len(recovered))
            # 注意：恢复的任务需要重新注册到线程池，但 future 已失效
            # 这里只记录日志，实际恢复逻辑需在工具层处理
    except Exception:
        logger.warning("[startup] 后台任务恢复失败", exc_info=True)
    # 启动时先清一轮内存僵尸任务（recover 会把中断任务置为 stopped，
    # 超过 JOB_TTL 的即可释放 agent/历史，避免上次进程遗留的大对象堆积）
    try:
        chat_jobs.prune_memory_jobs()
    except Exception:
        logger.warning("[startup] 内存僵尸任务清理失败", exc_info=True)
    # 长期运行的服务只靠启动清理不够：jobs/ 持续写入，需要周期性回收
    _cleanup_task = asyncio.create_task(_periodic_job_cleanup())
    # 会话目录同样只增不删，周期清理超过 3 天的旧会话
    _session_cleanup_task = asyncio.create_task(_periodic_session_cleanup())
    # 恢复"手动启动过且未手动停止"的通道（服务重启后自动拉起）
    _gw_restore_task = None
    try:
        from platform_ext.gateway import restore_auto_start_channels

        _gw_restore_task = asyncio.create_task(restore_auto_start_channels())
    except Exception:
        _gw_restore_task = None
        logger.warning("[startup] gateway 自动恢复任务创建失败", exc_info=True)
    try:
        yield
    finally:
        _cleanup_task.cancel()
        _session_cleanup_task.cancel()
        if _gw_restore_task is not None:
            _gw_restore_task.cancel()
        # 进程退出时优雅停止仍在运行的任务，避免 agent 数据丢失
        # （原先只在 __main__ 路径注册 atexit，uvicorn 启动时不会生效）
        running = [j for j in chat_jobs._jobs.values() if j.is_running]
        if running:
            logger.info(f"[Shutdown] 停止 {len(running)} 个运行中任务...")
            for j in running:
                try:
                    j.finish("stopped")
                    if j.agent is not None and hasattr(j.agent, "close"):
                        j.agent.close()
                except Exception:
                    logger.warning("[Shutdown] 停止任务失败，已记录", exc_info=True)


app = FastAPI(title="AI Agent Standalone", version="1.0.0", lifespan=_lifespan)

# ============================================================
# 鉴权：默认关闭（auth.json.enabled=false），保持既有部署行为；
# 开启后 /api/auth/* 与静态资源之外的路径均需 Bearer token。
# 详见 auth.py 模块说明。
from auth import register_auth
register_auth(app, BASE_DIR)

# ============================================================
# CORS：原先为 allow_origins=["*"] + allow_credentials=True，
# 与鉴权并存时等价于"任意站点可携带凭据调用任意接口"，属高危组合。
# 改为显式白名单（config.cors_origins）。
# 注意：CORS 配置需在 agent_main 实例化之后读取（见下方 add_middleware）。

# ============================================================
# MCP 路由：统一由 mcp_routes 模块提供（避免多份重复实现）
from mcp_routes import register_mcp_routes
register_mcp_routes(app)


agent_main = AgentMain()

# ============================================================
# OpenAI 兼容层：/v1/models（=提示词模板）与 /v1/chat/completions
# 独立模块实现，避免本文件进一步膨胀；必须放在 agent_main 之后
# （路由闭包需持有其实例以复用 _build_chat_agent）。
from v1_compat import register_v1_routes
register_v1_routes(app, agent_main, BASE_DIR)

# ============================================================
# CORS：此处才实例化完成 agent_main，故把实际注册放在这之后。
_cors_origins = (agent_main.config or {}).get("cors_origins") or []
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["WWW-Authenticate"],
)
if not _cors_origins:
    logger.warning(
        "[安全] cors_origins 未配置，浏览器跨域请求将被拒绝。"
        "请在 config.json 的 cors_origins 中填入前端域名（如 https://example.com）。"
        "同源直连（Nginx 反代）不受影响。"
    )

# ── 孤儿后台任务回灌（已抽离到 orphan_dispatch.py）──────────────────
import orphan_dispatch as _orphan_dispatch
from orphan_dispatch import (  # 兼容旧调用
    _register_orphan_callback,
    _strip_tool_xml,
    _dispatch_orphan_job,
    _dispatch_orphan_job_after_append,
    _try_append_to_parent_job,
)
_orphan_dispatch.configure(agent_main, chat_jobs, _map_agent_chunk)


# 后台预加载 MCP（避免首次 get_tool_list 阻塞）
def _preload_mcp():
    try:
        cfg = {}
        cfg_file = os.path.join(BASE_DIR, 'config.json')
        if os.path.exists(cfg_file):
            with open(cfg_file, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        if not cfg.get('enable_mcp', True):
            print("[MCP] enable_mcp=false, 跳过预加载")
            return
        from chat_client.mcp_client import ensure_mcp_loaded
        mcp_client = ensure_mcp_loaded()
        if mcp_client.is_loaded():
            print(f"MCP 预加载完成: {len(mcp_client.get_tool_schemas())} 个工具")
    except Exception as e:
        print(f"MCP 预加载（非阻塞）: {e}")

# 注册孤儿回调（在 agent_main 初始化完成后、MCP 预加载之前）
_register_orphan_callback()

_preload_thread = threading.Thread(target=_preload_mcp, name="mcp-preload", daemon=True)
_preload_thread.start()


# ---- 智能体定时任务调度线程 ----
def _scheduled_runner(task: dict):
    """到点执行：以独立会话启动任务。

    为什么固定 single 而非默认 group：group 是经理视角，只保留编排类工具
    （Task/RunCrew/CreateDepartment/RecruitMember），成员子代理拿不到
    ClawbotSendMedia 这类执行工具。定时任务常需要"生成文件并发回微信"，
    走 group 会在最后一步卡死——经理知道要发，但手上没工具。single 模式
    strict_tools=False，补全全套默认工具，任务自己就能干完。
    """
    sid = f"sched_{task.get('id','x')}_{int(time.time())}"
    payload = {
        "session_id": sid,
        "message": str(task.get("objective", "")),
        "model": str(task.get("model", "")),
        # 未显式指定 agents 时走 single：编排层对"无人值守的任务"没有价值，
        # 反而因工具受限挡住发送类操作。
        "mode": "group" if task.get("agents") else "single",
    }
    if task.get("agents"):
        payload["crew_agents"] = task["agents"]
    res = agent_main.chat_start(payload)
    logger.info(f"[Scheduler] 定时任务 {task.get('name')} 启动 -> {res}")
    return res

try:
    from chat_client.tools.scheduler import start_scheduler
    start_scheduler(_scheduled_runner)
except Exception as _e:
    logger.warning(f"[Scheduler] 启动失败: {_e}")


# ---- 热加载中心（工具/配置/团队/MCP 不重启更新） ----
import chat_client.hotreload as chat_client_hotreload

try:
    from chat_client import hotreload
    hotreload.init(lambda: agent_main)
    hotreload.start_watcher(10)
except Exception as _e:
    logger.warning(f"[HotReload] 初始化失败: {_e}")


async def _params(request: Request) -> dict:
    """Extract params from both query and body."""
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                params.update(body)
        except Exception:
            logger.warning("scheduler runner params merge 失败，已记录", exc_info=True)
    return params


# ── 首页与 Agent 入口路由（已抽离到 entry_routes.py）──────────────────
from entry_routes import register_entry_routes as _register_entry_routes
_register_entry_routes(app, BASE_DIR, agent_main)


# ── 系统管理路由（已抽离到 admin_routes.py）──────────────────────────
from admin_routes import register_admin_routes as _register_admin_routes
_register_admin_routes(app, BASE_DIR, agent_main)

# ── 聊天/会话路由（已抽离到 chat_routes.py）──────────────────────────
from chat_routes import register_chat_routes as _register_chat_routes
from chat_routes import _assert_session_owner  # 兼容旧调用
_register_chat_routes(app, BASE_DIR, agent_main)


# ── CLI 配置路由（已抽离到 cli_config_routes.py）──────────────────────
from cli_config_routes import register_cli_config_routes as _register_cli_config_routes
_register_cli_config_routes(app, agent_main)


# ── 安全增强与能力扩展（已抽离到 feature_routes.py）────────────────────
from feature_routes import register_feature_routes as _register_feature_routes
_register_feature_routes(app, BASE_DIR)


# ── 小程序与机器人路由（已抽离到 mp_routes.py）────────────────────────
from mp_routes import register_mp_routes as _register_mp_routes
from mp_routes import _mp_store, _mp_user_key, _mp_guard  # 兼容旧调用
_register_mp_routes(app, BASE_DIR, agent_main)


# ── P4-d: 备份（已抽离到 backup_routes.py）────────────────────────
from backup_routes import register_backup_routes as _register_backup_routes
_register_backup_routes(app, BASE_DIR)


# ── 卡密路由（已抽离到 card_routes.py）──────────────────────────────
from card_routes import register_card_routes as _register_card_routes
_register_card_routes(app, BASE_DIR, agent_main)


# ============================================================
# 文件能力：已抽离到 file_routes.py（优-1 路由拆分第一阶段）
# ============================================================
from file_routes import register_file_routes as _register_file_routes
from file_routes import _safe_resolve, _safe_resolve_serve  # 兼容旧调用与回归测试
_register_file_routes(app, BASE_DIR)


# ==================== 知识库 API（已抽离到 knowledge_routes.py） ====================
from knowledge_routes import register_knowledge_routes as _register_knowledge_routes
from knowledge_routes import _kb_instance  # 兼容旧调用
_register_knowledge_routes(app, BASE_DIR)


# ── 平台扩展层（/api/v2/{teams,channels,storage,usage,gateway,memory}/*，不破坏现有 /api/*）──
try:
    from platform_ext.api import router as _platform_ext_router
    app.include_router(_platform_ext_router)
    from platform_ext.usage import router as _usage_router
    app.include_router(_usage_router, prefix="/api/v2")
    from platform_ext.gateway import router as _gateway_router
    app.include_router(_gateway_router, prefix="/api/v2")
    from platform_ext.card_gateway import router as _card_gateway_router
    app.include_router(_card_gateway_router, prefix="/api/v2")
    logger.info("[platform-ext] 平台扩展路由已挂载: /api/v2/{teams,channels,storage,usage,gateway,card}/*")
except Exception:  # noqa: BLE001
    logger.exception("[platform-ext] 平台扩展路由挂载失败（主服务继续运行）")


# ── 浏览器基础设施（/api/v2/browser/* + WS 画面流，不破坏现有 /api/*）──
try:
    from platform_ext.browser import ensure_vendor_on_path
    ensure_vendor_on_path()
    from platform_ext.browser.api import subrouters as _browser_subrouters
    for _r in _browser_subrouters:
        app.include_router(_r, prefix="/api/v2")
    logger.info("[platform-ext] 浏览器路由已挂载: /api/v2/browser/* + /browser-stream/ws")

    @app.get("/browser")
    async def _browser_page():
        """浏览器控制台（实时画面 + 控制 + 录制回放，免鉴权页面，内部 XHR/WS 走 JWT）。"""
        p = os.path.join(BASE_DIR, "static", "browser.html")
        if os.path.exists(p):
            return FileResponse(p, media_type="text/html")
        return JSONResponse(public.returnMsg(False, "浏览器面板文件缺失"))
except Exception:  # noqa: BLE001
    logger.exception("[platform-ext] 浏览器路由挂载失败（主服务继续运行）")


# ── 连接器中心（/api/v2/connectors/*）──
try:
    from connector_hub.connectors_api import router as _connector_hub_router
    app.include_router(_connector_hub_router)
    logger.info("[connector-hub] 连接器路由已挂载: /api/v2/connectors/*")

    @app.get("/connectors")
    async def _connector_hub_page():
        """连接器管理可视化面板（免鉴权页面，内部 XHR 走 JWT）。"""
        p = os.path.join(BASE_DIR, "static", "connectors.html")
        if os.path.exists(p):
            return FileResponse(p, media_type="text/html")
        return JSONResponse(public.returnMsg(False, "连接器面板文件缺失"))
except Exception:  # noqa: BLE001
    logger.exception("[connector-hub] 连接器路由挂载失败（主服务继续运行）")


# Static files
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# 功能面板等核心页面禁止缓存，避免浏览器继续使用旧版（扫码显示修复等）。
# 用中间件确保无论路由顺序如何，都注入 no-cache 响应头。
# 卡密用户访问管理面板时重定向到卡密登录页，避免泄露管理员功能。
if os.path.exists(static_dir):
    @app.middleware("http")
    async def _no_cache_static_panel(request, call_next):
        response = await call_next(request)
        if request.url.path in ("/static/platform_panel.html", "/static/connectors.html", "/static/browser.html"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            # 卡密用户重定向到卡密登录页
            from auth import extract_token, is_card_token
            token = extract_token(request)
            if token and is_card_token(BASE_DIR, token) and "/static/platform_panel.html" in request.url.path:
                from fastapi.responses import RedirectResponse
                return RedirectResponse(url="/static/card-login.html", status_code=302)
        return response


if __name__ == '__main__':
    import argparse
    import atexit
    parser = argparse.ArgumentParser(description="AI Agent Standalone Web Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9876, help="Listen port (default: 9876)")
    args = parser.parse_args()

    print(f"AI Agent Standalone starting on http://{args.host}:{args.port}")
    # L-5: 进程退出时优雅停止所有仍在运行的聊天任务，避免 agent 数据丢失
    def _graceful_shutdown():
        running = [j for j in chat_jobs._jobs.values() if j.is_running]
        if running:
            logger.info(f"[Shutdown] 停止 {len(running)} 个运行中任务...")
            for j in running:
                j.finish("stopped")
                try:
                    if j.agent is not None and hasattr(j.agent, "close"):
                        j.agent.close()
                except Exception:
                    logger.warning("[Shutdown] 停止任务失败，已记录", exc_info=True)
            logger.info("[Shutdown] 全部任务已停止")
    atexit.register(_graceful_shutdown)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
