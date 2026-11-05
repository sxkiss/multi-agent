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

from channel_utils import (
    _is_miniprogram, _TERMINAL_CHANNELS, _client_channel, _is_terminal_channel,
    HIDDEN_EVENTS_FOR_TERMINAL, _TERMINAL_CHANNEL_GUARD, _MINIPROGRAM_CHANNEL_GUARD,
    _TERMINAL_SINGLE_PERSONA, _MINIPROGRAM_SINGLE_PERSONA, _append_miniprogram_guard,
    _KNOWN_SSE_EVENTS, _map_agent_chunk,
)


# ── 后台聊天任务（已抽离到 chat_jobs.py）────────────────────────────
import chat_jobs as _chat_jobs_mod
from chat_jobs import ChatJob, ChatJobManager  # 兼容旧调用与测试
from chat_jobs import chat_jobs  # 模块级单例（与 agent_chat_flow 共享同一实例）
# provider 在 web_server 模块内定义 → 调用时读取本模块 BASE_DIR（测试 monkeypatch 生效）
_chat_jobs_mod._BASE_DIR = BASE_DIR
_chat_jobs_mod._BASE_DIR_PROVIDER = lambda: BASE_DIR

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

from agent_prompt import PromptMixin
from agent_chat_flow import ChatFlowMixin


class AgentMain(PromptMixin, ChatFlowMixin, ConfigMixin, SessionMixin, CliConfigMixin, OrgSkillMixin, WorkspaceMixin):
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
