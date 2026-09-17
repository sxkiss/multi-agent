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
import contextvars
import datetime
import importlib
import inspect
import json
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
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# suppress verbose logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)

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

logger = logging.getLogger(__name__)

# claude 会话 ID 格式（UUID），用于区分 opencode（ses_ 前缀）/ claude / native 会话
_CLAUDE_SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _is_claude_session_id(session_id: str) -> bool:
    return bool(_CLAUDE_SID_RE.match(str(session_id or "")))

# ============================================================
# Agent Main
# ============================================================

def _map_agent_chunk(chunk):
    """将 agent.chat() 产出的 chunk 映射为 SSE (event, data) 列表"""
    t = chunk.get("type")
    if t == "content":
        return [("message", str(chunk.get("response", "")))]
    if t == "reasoning":
        return [("message_think", str(chunk.get("response", "")))]
    if t == "error":
        return [("error", {"msg": str(chunk.get("data", ""))})]
    if t == "stop":
        return [("usage", {"usage": chunk.get("usage", {})})]
    if t == "meta_info":
        return [("meta_info", {"user_msg_id": chunk.get("user_msg_id"), "ai_msg_id": chunk.get("ai_msg_id")})]
    return [(str(t or "message"), chunk)]


class ChatJob:
    """一个会话的后台聊天任务：与 HTTP 连接解耦，事件缓存在内存供重连回放。
    事件增量落盘到 jobs/<key>.jsonl，服务重启后可恢复为中断/完成状态回放。"""
    TERMINAL_STATUSES = ("done", "error", "stopped")

    def __init__(self, session_id, label="", persist=True, key=""):
        self.session_id = session_id
        self.label = (label or "")[:40]
        self.key = key or f"{session_id}::{uuid.uuid4().hex[:6]}"
        self.events = []            # [{"id": seq, "event": name, "data": ...}]
        self.status = "running"     # running | done | error | stopped
        self.agent = None
        self.created_at = time.time()
        self._lock = threading.Lock()
        # 事件持久化（JSONL 追加）
        self.persist = persist
        self.persist_path = os.path.join(BASE_DIR, "jobs", f"{self.key.replace(chr(47), '_')}.jsonl")
        if persist:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)

    def attach_agent(self, agent):
        self.agent = agent

    def append(self, event, data=None):
        with self._lock:
            self.events.append({"id": len(self.events), "event": event, "data": data})
        # 增量落盘（原子追加）
        if self.persist:
            try:
                with open(self.persist_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str) + "\n")
            except Exception:
                logger.warning("ChatJob.append persist 写入失败", exc_info=True)

    def snapshot_from(self, last_id):
        """返回 (last_id 之后的事件, 当前状态)"""
        with self._lock:
            pending = [e for e in self.events if e["id"] > last_id]
            return pending, self.status

    def finish(self, status):
        with self._lock:
            # P1-5: 已处于终态（done/stopped/error）时不再覆盖，防止 chat_stop
            # 后 agent 线程异常把 "stopped" 写成 "error"
            if self.status in {"done", "stopped", "error"} and self.status != status:
                return
            self.status = status
        if self.persist:
            try:
                with open(self.persist_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"__status__": status}, ensure_ascii=False) + "\n")
            except Exception:
                logger.warning("ChatJob.finish persist 写入失败", exc_info=True)

    @property
    def is_running(self):
        with self._lock:
            return self.status == "running"

    @classmethod
    def recover(cls, key):
        """从 jobs/<key>.jsonl 恢复任务（重启后回放中断/完成状态）"""
        p = os.path.join(BASE_DIR, "jobs", f"{key.replace(chr(47), '_')}.jsonl")
        if not os.path.exists(p):
            return None
        sid = key.split("::")[0]
        job = cls(session_id=sid, label="(恢复)", persist=False, key=key)
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        logger.warning("处理时跳过异常", exc_info=True)
                        continue
                    if "__status__" in rec:
                        job._lock.acquire()
                        try:
                            job.status = rec["__status__"]
                        finally:
                            job._lock.release()
                    else:
                        job._lock.acquire()
                        try:
                            job.events.append({"id": len(job.events),
                                               "event": rec.get("event", "message"),
                                               "data": rec.get("data")})
                        finally:
                            job._lock.release()
        except Exception:
            return None
        job.created_at = int(time.time())
        return job


class ChatJobManager:
    """后台聊天任务管理：同会话支持并行（上限 MAX_PARALLEL），按 job_key 索引"""

    JOB_TTL = 3600
    MAX_PARALLEL = int(os.environ.get("AI_AGENT_MAX_PARALLEL", "3"))

    def __init__(self):
        self._jobs = {}
        self._lock = threading.RLock()  # RLock: 可重入，允许嵌套调用

    @staticmethod
    def make_key(session_id):
        return f"{session_id}::{uuid.uuid4().hex[:6]}"

    def get(self, key):
        with self._lock:
            return self._jobs.get(key)

    def active_for_session(self, session_id):
        with self._lock:
            return [(k, j) for k, j in self._jobs.items() if j.session_id == session_id]

    def count_running(self, session_id):
        with self._lock:
            return len([1 for _, j in self.active_for_session(session_id) if j.is_running])

    def create_if_idle(self, session_id, label=""):
        """并行创建；达到会话并行上限返回 None（原子操作，防竞态）"""
        with self._lock:
            # 原子检查+创建：在同一锁内完成计数判断与任务创建，避免并发窗口
            running_count = len([1 for k, j in self._jobs.items()
                                 if j.session_id == session_id and j.is_running])
            if running_count >= self.MAX_PARALLEL:
                return None
            self._prune_locked()
            key = self.make_key(session_id)
            job = ChatJob(session_id, label)
            job.key = key
            self._jobs[key] = job
            return job

    def discard(self, key):
        with self._lock:
            j = self._jobs.get(key)
            if j and not j.events:
                self._jobs.pop(key, None)

    def stop(self, key=None, session_id=None):
        targets = []
        with self._lock:
            if key:
                j = self._jobs.get(key)
                if j:
                    targets.append(j)
            elif session_id:
                targets = [j for _, j in self.active_for_session(session_id)]
        for j in targets:
            if j.is_running:
                j.finish("stopped")
                try:
                    if j.agent:
                        j.agent.close()
                except Exception:
                    logger.warning("ChatJobManager agent.close 失败，已记录", exc_info=True)
        return targets

    def recover_all(self):
        """启动时从 jobs/ 恢复未完成与近期完成的任务（供续播）"""
        import glob as _glob
        jobs_dir = os.path.join(BASE_DIR, "jobs")
        if not os.path.isdir(jobs_dir):
            return 0
        n = 0
        for p in _glob.glob(os.path.join(jobs_dir, "*.jsonl")):
            key = os.path.basename(p)[:-6]
            # 已存在则不覆盖
            if key in self._jobs:
                continue
            job = ChatJob.recover(key)
            if job:
                with self._lock:
                    self._jobs[key] = job
                n += 1
        log_ = logging
        if n:
            log_.info(f"[ChatJob] 启动恢复 {n} 个任务")
        return n

    def _prune_locked(self):
        now = time.time()
        stale = [k for k, j in self._jobs.items()
                 if not j.is_running and now - j.created_at > self.JOB_TTL]
        for k in stale:
            self._jobs.pop(k, None)


chat_jobs = ChatJobManager()


class AgentMain:
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

    def _resolve_workspace(self, mode=None):
        """解析工作空间：优先请求参数 > 模式配置 > 全局配置 > 项目根目录"""
        # 1. 尝试从模式配置获取
        if mode:
            workspaces = self.config.get('workspaces', {})
            ws = workspaces.get(mode, '') or ''
            if ws:
                ws = os.path.abspath(os.path.expanduser(ws))
                if os.path.isdir(ws):
                    return ws
        
        # 2. 回退到全局配置
        ws = self.config.get('workspace', '') or ''
        if not ws:
            return BASE_DIR
        ws = os.path.abspath(os.path.expanduser(ws))
        if not os.path.isdir(ws):
            try:
                os.makedirs(ws, exist_ok=True)
            except Exception:
                return BASE_DIR
        return ws

    def _merge_config(self, base, update):
        for k, v in update.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge_config(base[k], v)
            else:
                base[k] = v

    def _load_config(self):
        if not os.path.exists(self.config_path):
            return {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self):
        try:
            # P1-3: 原子写——并发 POST /api/config 时先写 tmp 再替换，避免 JSON 损坏
            tmp = self.config_path + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            os.replace(tmp, self.config_path)
            return True, '保存成功'
        except Exception as e:
            return False, f'保存配置失败: {e!s}'

    def _load_agents_config(self):
        agents_config_file = os.path.join(self.plugin_path, 'agents.json')
        if not os.path.exists(agents_config_file):
            return None, '缺少配置文件 agents.json'
        try:
            with open(agents_config_file, 'r', encoding='utf-8') as f:
                return json.load(f), None
        except Exception as e:
            return None, f'加载配置失败 agents.json: {e!s}'

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

    def get_config(self):
        data = {}
        # In standalone mode, skip remote usage call
        api_key = self.config.get('api_key', '')
        if api_key and api_key != self.DEFAULT_CONFIG['api_key']:
            # Custom API configured, skip usage check
            pass
        else:
            # Using default key - in standalone mode, don't call remote usage API
            # Just return empty quota data
            pass

        questions = [
            {"question": "查看服务器资源使用情况", "tools": ["get_system_resources"]},
            {"question": "查询服务器IP地址", "tools": ["get_server_ip"]},
            {"question": "CPU、磁盘负载过高怎么办?", "tools": ["get_system_resources", "get_top_processes"]},
            {"question": "检查Docker运行状态", "tools": ["get_docker_info", "get_docker_containers"]},
            {"question": "Nginx服务无法启动", "tools": ["get_service_status"]},
        ]
        questions = random.sample(questions, min(5, len(questions)))

        configs = {
            "daily_quota": {
                "used": data.get("used", 0),
                "total": data.get("limit", 0),
                "reset_time": "独立模式",
                "activate": data.get("activate", 50),
            },
            "config": self.config,
            "is_custom_api": bool(api_key and api_key != self.DEFAULT_CONFIG['api_key']),
            "questions": questions,
        }
        return public.return_data(True, data=configs)

    def get_models(self, base_url='', key=''):
        if not base_url or not key:
            return public.returnMsg(False, '缺少参数 base_url 或 key')
        import openai
        client = openai.OpenAI(api_key=key, base_url=base_url, default_headers=self.config['default_headers'])
        try:
            response = client.models.list()
            model_names = [model.id for model in response.data]
            return public.return_data(True, data=model_names)
        except Exception:
            return public.return_data(True, data=[])

    def set_config(self, config_str=''):
        if not config_str:
            return public.returnMsg(False, '缺少配置参数 config')
        try:
            user_config = json.loads(config_str)
        except Exception:
            return public.returnMsg(False, '配置参数格式错误')
        # 从当前用户配置深拷贝，保留已有字段不被 DEFAULT_CONFIG 覆盖
        new_config = json.loads(json.dumps(self.config))
        self._merge_config_with_rules(new_config, user_config)
        self._merge_config_with_rules(self.config, user_config)
        self.config = new_config
        status, msg = self._save_config()
        if status:
            return public.returnMsg(True, '设置成功')
        else:
            return public.returnMsg(False, msg)

    def _merge_config_with_rules(self, base, update):
        for k, v in update.items():
            if isinstance(v, str):
                v = v.strip()
            is_empty = (v is None) or (v == "") or (isinstance(v, list) and len(v) == 0)
            if is_empty:
                # 这些字段允许显式清空（运行时回退默认值，如 workspace → 项目根目录）
                if k in self.CLEARABLE_CONFIG_KEYS:
                    base[k] = [] if isinstance(v, list) else ""
                continue
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge_config_with_rules(base[k], v)
            else:
                base[k] = v

    def agent_list(self):
        agents_config, error = self._load_agents_config()
        if error:
            return public.returnMsg(False, error)
        return public.return_data(True, data=agents_config)

    def get_tool_list(self):
        # 不等待 MCP 预加载（异步加载完成后下次请求自然出现），避免阻塞
        tools = registry.get_all_tools_info()
        # 确保所有工具都显示为可用（前端会过滤 show: False 的工具）
        for tool in tools:
            tool["show"] = True
        return public.return_data(True, data=tools)

    def get_skill_list(self):
        all_skills = skill_manager.get_all_skills_info()
        skills = []
        for skill in all_skills:
            location = skill.get("location", "")
            rel_path = location.replace(skill_manager.skills_dir, "").strip("/")
            path_parts = rel_path.split("/")
            # 仅排除特殊元文件（如 SKILL.md 直接落在 skills 根目录的情况），
            # 其余一律加载，包括嵌套在组织型技能目录下的"链接/关联"子技能
            if len(path_parts) >= 2 and path_parts[-1] == "SKILL.md":
                skills.append(skill)
        enabled_count = len([skill for skill in skills if skill.get("enabled")])
        data = {
            "total": len(skills),
            "enabled": enabled_count,
            "disabled": len(skills) - enabled_count,
            "skills": skills
        }
        return public.return_data(True, data=data)

    def skill_install(self, get):
        """前端上传 ZIP 安装技能。参数: zip_b64(base64 编码的 zip), overwrite(可选 bool)"""
        import base64
        zip_b64 = str(get.get('zip_b64', '') or '').strip()
        if not zip_b64:
            return public.returnMsg(False, '缺少参数 zip_b64')
        overwrite = str(get.get('overwrite', '')).lower() in ('1', 'true', 'yes')

        # 去除 data URI 前缀（data:application/zip;base64,xxxx）
        if ',' in zip_b64 and zip_b64.strip().startswith('data:'):
            zip_b64 = zip_b64.split(',', 1)[1]

        try:
            zip_bytes = base64.b64decode(zip_b64, validate=False)
        except Exception as e:
            return public.returnMsg(False, f'base64 解码失败: {e!s}')

        if not zip_bytes:
            return public.returnMsg(False, '压缩包内容为空')
        if len(zip_bytes) > 30 * 1024 * 1024:
            return public.returnMsg(False, '压缩包超过 30MB 上限')

        result = skill_manager.install_from_zip(zip_bytes, overwrite=overwrite)
        if result.get('status'):
            return public.return_data(True, data={
                'name': result.get('name'),
                'path': result.get('path'),
                'msg': result.get('msg'),
            })
        return public.returnMsg(False, result.get('msg', '安装失败'))

    def skill_uninstall(self, get):
        name = str(get.get('skill_name', '') or '')
        result = skill_manager.uninstall(name)
        return public.returnMsg(bool(result.get('status')), result.get('msg', ''))

    # ---------------- 自定义子代理（CrewAI 风格角色） ----------------

    _CREW_NAME_RE = re.compile(r"^[a-z0-9_\-]{2,40}$")
    _BUILTIN_AGENTS: ClassVar[set] = {"search", "planner", "coder"}

    def _crew_file(self):
        return os.path.join(self.plugin_path, "crew_agents.json")

    _crew_lock = threading.Lock()  # P1-2: 保护 crew JSON 读写，防止并发覆盖

    def _crew_read(self):
        try:
            with self._crew_lock, open(self._crew_file(), "r", encoding="utf-8") as f:
                return json.load(f).get("agents", [])
        except Exception:
            return []

    def _crew_write(self, agents):
        # P1-2: 加锁确保写操作原子性；外层已有 tmp+replace
        with self._crew_lock:
            tmp = self._crew_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"agents": agents}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._crew_file())

    def crew_agents_list(self, get=None):
        from chat_client.tools.task import agent_registry
        customs = {a["name"]: a for a in self._crew_read()}
        infos = []
        for a in agent_registry.list_agents():
            saved = customs.get(a.name, {})
            infos.append({
                "name": a.name,
                "description": a.description,
                "builtin": a.name not in customs,
                "tools": list(a.allowed_tools),
                "department": str(saved.get("department", "")).strip()
                              or getattr(a, "department", ""),
                "backstory": str(saved.get("backstory", "")) if a.name in customs else "",
                "model": str(saved.get("model", "")),
            })
        return public.return_data(True, data={
            "agents": infos,
            "departments": list(getattr(agent_registry, "departments", [])),
        })

    def org_chart(self, get=None):
        """首页集团组织架构：Boss → 经理 → 各部门 → 成员"""
        from chat_client.tools.task import agent_registry
        members = agent_registry.list_agents()
        departments = list(getattr(agent_registry, "departments", []))
        dept_map = {d["name"]: d for d in departments}

        groups = {}
        unassigned = []
        for m in members:
            dep = (m.department or "").strip()
            if dep and dep in dept_map:
                groups.setdefault(dep, []).append(m)
            else:
                unassigned.append(m)

        out_depts = []
        for d in departments:
            ms = groups.get(d["name"], [])
            out_depts.append({
                "name": d["name"],
                "title": d["title"],
                "description": d.get("description", ""),
                "members": [{"name": m.name, "description": m.description,
                             "tools": list(m.allowed_tools)} for m in ms],
            })
        # 综合部成员同样带 tools
        for od in out_depts:
            if od["name"] == "__general__":
                for mm in od["members"]:
                    ag = next((a for a in members if a.name == mm["name"]), None)
                    if ag:
                        mm["tools"] = list(ag.allowed_tools)
        # 未分配成员归入「综合部」
        if unassigned:
            out_depts.append({
                "name": "__general__",
                "title": "综合部",
                "description": "未分配部门的成员",
                "members": [{"name": m.name, "description": m.description} for m in unassigned],
            })
        return public.return_data(True, data={
            "boss": {"name": "Boss", "title": "老板（你）"},
            "manager": {"name": "manager", "title": "经理", "description": "接收目标、拆解计划、分派部门与成员、汇总交付"},
            "departments": out_depts,
        })

    def crew_agent_save(self, get):
        name = str(get.get("name", "")).strip().lower()
        description = str(get.get("description", "")).strip()
        backstory = str(get.get("backstory", "")).strip()
        tools = get.get("tools") or []
        model = str(get.get("model", "")).strip()

        if not self._CREW_NAME_RE.match(name):
            return public.returnMsg(False, "代理名仅允许小写字母/数字/_/-，长度 2~40")
        if name in self._BUILTIN_AGENTS:
            return public.returnMsg(False, "不能覆盖内置代理名")
        if not description:
            return public.returnMsg(False, "缺少 description（角色职责/Goal）")
        if not backstory:
            return public.returnMsg(False, "缺少 backstory（系统提示词）")
        if not isinstance(tools, list) or not tools:
            return public.returnMsg(False, "至少勾选一个工具")

        department = str(get.get("department", "")).strip()
        agents = [a for a in self._crew_read() if a.get("name") != name]
        agents.append({
            "name": name,
            "description": description[:500],
            "backstory": backstory[:8000],
            "tools": [str(t) for t in tools][:60],
            "model": model,
            "department": department,
        })
        try:
            self._crew_write(agents)
        except Exception as e:
            return public.returnMsg(False, f"写入失败: {e!s}")

        from chat_client.tools.task import reload_custom_agents
        names = reload_custom_agents()
        return public.return_data(True, data={"name": name, "all": names})

    def crew_agent_delete(self, get):
        name = str(get.get("name", "")).strip()
        agents = self._crew_read()
        remaining = [a for a in agents if a.get("name") != name]
        if len(remaining) == len(agents):
            return public.returnMsg(False, f"自定义代理不存在: {name}")
        try:
            self._crew_write(remaining)
        except Exception as e:
            return public.returnMsg(False, f"写入失败: {e!s}")
        from chat_client.tools.task import reload_custom_agents
        reload_custom_agents()
        return public.return_data(True, data={"deleted": name})

    def crew_dept_save(self, get):
        name = str(get.get("name", "")).strip().lower()
        title = str(get.get("title", "")).strip()
        description = str(get.get("description", "")).strip()
        if not re.fullmatch(r"[a-z0-9_\-]{2,30}", name):
            return public.returnMsg(False, "部门标识仅允许小写字母/数字/_/-，长度 2~30")
        if not title:
            return public.returnMsg(False, "缺少部门名称 title")
        agents = self._crew_read()
        depts = [d for d in (self.__class__.__dict__ and [])] if False else None
        # 读取现有 departments
        try:
            with open(self._crew_file(), "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded.get("departments"), list):
                    depts = loaded["departments"]
        except Exception:
            logger.warning("crew_dept_save 读取失败，已记录", exc_info=True)
        depts = [d for d in depts if d.get("name") != name]
        depts.append({"name": name, "title": title[:40], "description": description[:200]})
        tmp = self._crew_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"departments": depts, "agents": agents}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._crew_file())
        from chat_client.tools.task import reload_custom_agents
        reload_custom_agents()
        return public.return_data(True, data={"name": name})

    def crew_dept_delete(self, get):
        name = str(get.get("name", "")).strip()
        agents = self._crew_read()
        try:
            with open(self._crew_file(), "r", encoding="utf-8") as f:
                depts = json.load(f).get("departments", [])
        except Exception:
            depts = []
        new_depts = [d for d in depts if d.get("name") != name]
        if len(new_depts) == len(depts):
            return public.returnMsg(False, f"部门不存在: {name}")
        tmp = self._crew_file() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"departments": new_depts, "agents": agents}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._crew_file())
        from chat_client.tools.task import reload_custom_agents
        reload_custom_agents()
        return public.return_data(True, data={"deleted": name})

    def skill_install_url(self, get):
        """从 URL 安装技能。参数: url(直链 zip 或 GitHub 仓库), overwrite(可选 bool)"""
        url = str(get.get('url', '') or '').strip()
        if not url:
            return public.returnMsg(False, '缺少参数 url')
        overwrite = str(get.get('overwrite', '')).lower() in ('1', 'true', 'yes')
        result = skill_manager.install_from_url(url, overwrite=overwrite)
        if result.get('status'):
            return public.return_data(True, data={
                'name': result.get('name'),
                'path': result.get('path'),
                'msg': result.get('msg'),
            })
        return public.returnMsg(False, result.get('msg', '安装失败'))

    def _load_agent_context_prompt(self):
        """
        从 SOUL.md / AGENTS.md / USER.md / MEMORY.md 组装经理（含集团/系统派发模式）的系统设定，
        取代原来写死在 config/代码中的规则。文件不存在时返回 None，由调用方回退到 config['system_prompt']。
        """
        ctx_files = {
            'SOUL.md': '身份与人格（SOUL）',
            'AGENTS.md': '行为准则与能力（AGENTS）',
            'USER.md': '关于用户（USER）',
            'MEMORY.md': '记忆与偏好（MEMORY）',
        }
        parts = []
        for fname, title in ctx_files.items():
            fpath = os.path.join(BASE_DIR, fname)
            if os.path.exists(fpath):
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        content = f.read().strip()
                    if content:
                        parts.append(f"## {title}\n{content}")
                except Exception:
                    logger.warning("system_prompt 模板解析失败，已记录", exc_info=True)
        if not parts:
            return None
        return "你是 AI 运维助手「经理」，以下为你的核心设定，请严格遵守：\n\n" + "\n\n".join(parts)

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
        if isinstance(user_input, str):
            try:
                parsed_input = json.loads(user_input)
                if isinstance(parsed_input, list):
                    user_input = parsed_input
                elif isinstance(parsed_input, dict):
                    # 单个 content part（如 {"type":"text","text":"..."}）→ 包装为标准列表格式
                    user_input = [parsed_input]
            except json.JSONDecodeError:
                logger.warning("simple_chat user_input JSON 解析失败，已记录", exc_info=True)
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
            # 工具：优先前端传入，否则留空由 agent（strict_tools=False）补全全套默认工具
            tools = raw_tools if raw_tools else []
        else:
            # 集团模式（group，默认）：经理视角，只保留编排类工具；
            # 忽略前端传入的工具列表（防止旧页面全量选择覆盖经理角色），
            # 具体执行由各成员代理用自己的工具完成（Task/RunCrew 分派）
            strict_mode = True
            tools = ["Task", "RunCrew", "CreateDepartment", "RecruitMember"]

        # 工作空间：优先使用前端传入的 workspace，否则按模式配置
        if workspace_override:
            workspace = os.path.abspath(os.path.expanduser(workspace_override))
            if not os.path.isdir(workspace):
                try:
                    os.makedirs(workspace, exist_ok=True)
                except Exception:
                    workspace = self._resolve_workspace(mode)
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
            "system_prompt": final_system_prompt,
            "temperature": float(self._get_priority_value('temperature', get, prompt_config, self.config['agent'].get('temperature', 1.0))),
            "top_p": float(self._get_priority_value('top_p', get, prompt_config, self.config['agent'].get('top_p', 1.0))),
            "reasoning_effort": str(get.get('reasoning_effort', self.config['agent'].get('reasoning_effort', 'high'))).strip().lower() or 'high',
            "thinking": thinking,
            "web_search": web_search,
            "sessions_dir": os.path.join(self.plugin_path, sessions_dir),
            "global_kb_dir": os.path.join(self.plugin_path, 'memo'),
            "use_global_rag": self._get_priority_value('use_global_rag', get, prompt_config, self.config.get('use_global_rag', False)),
            "use_external_kb": self._get_priority_value('use_external_kb', get, prompt_config, self.config.get('use_external_kb', False)),
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
            base_persona = ctx or "你是「AI 集团」的总经理。对面是老板（Boss，即用户本人）——他只下达目标，不关心内部过程。"
            # 注入技能摘要（与单 Agent 模式同源），让经理在派任务时知道有哪些 skill 可用，
            # 以便在 Task/RunCrew 的 prompt 中指示成员加载对应技能执行
            try:
                from chat_client.skills import skill_manager
                skill_summary = skill_manager.generate_skill_summary()
            except Exception:
                skill_summary = ""
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
{f"""[MCP 外部工具（成员可调用）]
{chr(10).join(mcp_lines)}
""" if mcp_lines else ""}
派任务时，如果某目标需要特定技能，在 Task/RunCrew 的 prompt 中写明「使用 Skills(name='技能名') 加载对应技能」或直接调用对应 MCP 工具。

【工作原则】
1. 凡涉及实际操作——读写文件、执行命令、查询系统、抓取网页、分析日志等——必须通过上述工具**分派给对应成员**执行，
   严禁在没有工具结果的情况下自行编造过程或结论。
2. 简单问候、常识问答可不经分派直接回答。
3. 多环节/跨领域目标优先使用 RunCrew——鼓励跨部门流水线：
   调研部搜集 → 研发部实现 → 测试部验证 → 审核部把关。
   成员之间可通过 ConsultPeer 互相咨询，无需经过你中转。
4. 高风险操作会被安全策略拦截确认：此时向 Boss 说明将要执行的操作与风险，等待明确同意。
5. 交付时面向 Boss 汇报：结论先行、要点清晰、附关键证据。"""
            agent_config["system_prompt"] = manager_sp

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
            agent.close()

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

        # ── opencode 模式 ────────────────────────────────────────────
        if mode == 'opencode':
            from chat_client.opencode_bridge import run_opencode_chat
            model = str(get.get('model', 'auto')).strip() or 'auto'

            # 提示词：system_prompt 直接传 → 模板选择 → 配置默认（三模式共用）
            system_prompt = self._resolve_template_system_prompt(get, cfg_key='opencode')

            workspace = str(get.get('workspace', '')).strip()
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

            workspace = str(get.get('workspace', '')).strip()
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
            workspace = str(get.get('workspace', '')).strip()
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
        session_id = get.get('session_id', '')
        job_key = get.get('job', '')
        if not session_id:
            yield self.sse_pack(event="error", data={"msg": "缺少参数 session_id"})
            return
        if job_key:
            job = chat_jobs.get(job_key)
        else:
            # 兼容：取该会话最新的任务
            act = chat_jobs.active_for_session(session_id)
            job = act[-1][1] if act else None
        if not job:
            yield self.sse_pack(event="error", data={"msg": "没有可订阅的任务（不存在或已过期）"})
            return

        try:
            last_id = int(get.get('last_id', -1))
        except (TypeError, ValueError):
            last_id = -1

        sent_end = False
        idle_ticks = 0  # 空闲 tick 计数，用于 SSE keepalive，防止客户端 sock_read 超时断开
        max_idle_seconds = 900  # P2-27: 最大空闲 15min，超时向客户端返回结束信号防止无限挂起
        idle_start = asyncio.get_event_loop().time()
        while True:
            pending, status = job.snapshot_from(last_id)
            for e in pending:
                yield self.sse_pack(event=e["event"], id=e["id"], data=e["data"])
                last_id = e["id"]
                if e["event"] == "message_end":
                    sent_end = True
            if status != "running" and not pending:
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
        session_id = get.get('session_id', '')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        jobs = chat_jobs.active_for_session(session_id)
        if not jobs:
            return public.return_data(True, data={"running": False, "status": "none",
                                                  "last_id": -1, "elapsed": 0, "jobs": []})
        jobs_list = []
        for key, j in sorted(jobs, key=lambda kv: kv[1].created_at):
            with j._lock:
                lid = len(j.events) - 1
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
        session_id = get.get('session_id', '')
        job_key = str(get.get('job', '') or '').strip()
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        targets = chat_jobs.stop(key=job_key or None, session_id=None if job_key else session_id)
        if not targets:
            return public.returnMsg(False, "没有可停止的任务")
        return public.return_data(True, data={"stopped": len(targets)})

    def opencode_get_config(self, get=None):
        """获取 opencode 模式配置：内置模板 + 自定义模板"""
        try:
            from chat_client.opencode_templates import OPENCODE_TEMPLATES
        except Exception:
            OPENCODE_TEMPLATES = {}
        oc = self.config.get('opencode', {})
        return public.return_data(True, data={
            "opencode": oc,
            "builtin_templates": list(OPENCODE_TEMPLATES.keys()),
        })

    def opencode_save_config(self, get):
        """保存 opencode 模式配置到 config.json"""
        new_oc = get.get('opencode')
        if not isinstance(new_oc, dict):
            new_oc = get  # 兼容：请求体直接就是 opencode 内容
        if not isinstance(new_oc, dict):
            return public.returnMsg(False, "参数格式错误")
        self.config['opencode'] = new_oc
        path = os.path.join(self.plugin_path, 'config.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            return public.returnMsg(False, f"保存配置失败: {e!s}")
        return public.return_data(True, data=self.config['opencode'])

    def opencode_status(self, get):
        """查询 opencode 模式整体状态：可用性 + 运行中的 opencode 任务"""
        import shutil as _shutil
        from chat_client.opencode_config import _PROJECT_ROOT
        from chat_client.opencode_bridge import OpenCodeProcess
        bin_path = OpenCodeProcess._opencode_path()
        available = False
        if os.path.isabs(bin_path):
            available = os.path.exists(bin_path)
        else:
            available = bool(_shutil.which(bin_path))
        # 运行中的 opencode 任务
        running = []
        for k, j in chat_jobs._jobs.items():
            if j.is_running and isinstance(getattr(j, 'agent', None), OpenCodeProcess):
                running.append(k)
        return public.return_data(True, data={
            "available": available,
            "binary": bin_path,
            "running_jobs": running,
        })

    def opencode_cli_get_config(self, get=None):
        """获取 opencode CLI 全局配置 (~/.config/opencode/opencode.json) 的 gateway provider"""
        import shutil as _shutil
        from chat_client.opencode_config import _GLOBAL_OC_PATH, _GLOBAL_AUTH_PATH
        oc = {}
        try:
            with open(_GLOBAL_OC_PATH, 'r', encoding='utf-8') as f:
                oc = json.load(f)
        except Exception as e:
            logger.warning("读取 opencode.json 失败: %s", e)
        gateway = (oc.get('provider') or {}).get('gateway', {})
        options = gateway.get('options', {})
        models = gateway.get('models', {})
        # 获取第一个 model 的 reasoningEffort
        reasoning_effort = 'max'
        for mname, mconf in models.items():
            reasoning_effort = mconf.get('options', {}).get('reasoningEffort', 'max')
            break
        oc_bin = _shutil.which("opencode") or os.path.expanduser("~/.local/nodejs/node-latest/bin/opencode")
        return public.return_data(True, data={
            "base_url": options.get('baseURL', ''),
            "api_key": options.get('apiKey', ''),
            "model": list(models.keys())[0] if models else '',
            "reasoning_effort": reasoning_effort,
            "opencode_binary": oc_bin,
            "config_path": _GLOBAL_OC_PATH,
        })

    def opencode_cli_save_config(self, get):
        """保存 opencode CLI 全局配置 gateway provider"""
        from chat_client.opencode_config import _GLOBAL_OC_PATH, _GLOBAL_AUTH_PATH
        import stat as _stat
        base_url = str(get.get('base_url', '')).strip()
        api_key = str(get.get('api_key', '')).strip()
        model = str(get.get('model', 'auto')).strip() or 'auto'
        reasoning_effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
        if not base_url:
            return public.returnMsg(False, "base_url 不能为空")
        try:
            existing = {}
            if os.path.exists(_GLOBAL_OC_PATH):
                with open(_GLOBAL_OC_PATH, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            # 只覆盖 gateway provider
            existing.setdefault('provider', {})['gateway'] = {
                "name": "gateway",
                "type": "openai",
                "options": {"baseURL": base_url, "apiKey": api_key},
                "models": {
                    model: {
                        "name": model,
                        "reasoning": True,
                        "options": {"reasoningEffort": reasoning_effort},
                        "limit": {"context": 262144, "output": 32000},
                    }
                },
            }
            os.makedirs(os.path.dirname(_GLOBAL_OC_PATH), exist_ok=True)
            with open(_GLOBAL_OC_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            # auth.json
            existing_auth = {}
            try:
                with open(_GLOBAL_AUTH_PATH, 'r', encoding='utf-8') as f:
                    existing_auth = json.load(f)
            except Exception:
                pass
            existing_auth["gateway"] = {"type": "api", "key": api_key}
            with open(_GLOBAL_AUTH_PATH, 'w', encoding='utf-8') as f:
                json.dump(existing_auth, f, indent=2)
            os.chmod(_GLOBAL_AUTH_PATH, _stat.S_IRUSR | _stat.S_IWUSR)
            return public.return_data(True, data={"saved": True})
        except Exception as e:
            return public.returnMsg(False, f"保存失败: {e!s}")

    def claude_get_config(self, get=None):
        """获取 Claude CLI 配置：effortLevel + env 环境变量"""
        import shutil as _shutil
        claude_dir = os.path.expanduser("~/.claude")
        settings_path = os.path.join(claude_dir, "settings.json")
        settings = {}
        try:
            with open(settings_path, 'r', encoding='utf-8') as f:
                settings = json.load(f)
        except Exception as e:
            logger.warning("读取 Claude 配置失败: %s", e)
        # 查找 claude 二进制路径
        claude_bin = _shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
        return public.return_data(True, data={
            "effortLevel": settings.get("effortLevel", "high"),
            "env": settings.get("env", {}),
            "claude_binary": claude_bin,
            "settings_path": settings_path,
        })

    def claude_save_config(self, get):
        """保存 Claude CLI 配置到 ~/.claude/settings.json"""
        effort_level = str(get.get('effortLevel', 'high')).strip().lower()
        if effort_level not in ('low', 'medium', 'high', 'max'):
            return public.returnMsg(False, "effortLevel 必须是 low/medium/high/max 之一")
        env = get.get('env', {})
        if isinstance(env, str):
            try:
                env = json.loads(env)
            except Exception:
                return public.returnMsg(False, "env 格式错误")
        if not isinstance(env, dict):
            return public.returnMsg(False, "env 必须是 JSON 对象")

        settings_path = os.path.expanduser("~/.claude/settings.json")
        try:
            existing = {}
            if os.path.exists(settings_path):
                with open(settings_path, 'r', encoding='utf-8') as f:
                    existing = json.load(f)
            existing["effortLevel"] = effort_level
            existing["env"] = {**existing.get("env", {}), **env}
            with open(settings_path, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2, ensure_ascii=False)
            return public.return_data(True, data=existing)
        except Exception as e:
            return public.returnMsg(False, f"保存失败: {e!s}")

    # ── codex 管理面板：~/.codex/config.toml 读写 + 状态探测 ────────────────
    _CODEX_CFG_PATH = os.path.expanduser("~/.codex/config.toml")

    def codex_cli_get_config(self, get=None):
        """读取 ~/.codex/config.toml（简化 TOML 解析）+ codex 二进制状态"""
        import shutil as _shutil
        from chat_client.codex_bridge import _codex_path
        bin_path = _codex_path()
        available = bool(bin_path) and (os.path.exists(bin_path) if os.path.isabs(bin_path) else bool(_shutil.which(bin_path)))
        cfg = {"model": "auto", "reasoning_effort": "max", "base_url": "", "api_key": "", "config_path": self._CODEX_CFG_PATH}
        try:
            if os.path.exists(self._CODEX_CFG_PATH):
                with open(self._CODEX_CFG_PATH, 'r', encoding='utf-8') as f:
                    txt = f.read()
                import re as _re
                for m in _re.finditer(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:"([^"]*)"|([^#\n]+))', txt, _re.MULTILINE):
                    k, v_quoted, v_raw = m.group(1), m.group(2), m.group(3)
                    v = v_quoted if v_quoted is not None else (v_raw or '').strip()
                    if k == "model":
                        cfg["model"] = v
                    elif k == "model_reasoning_effort":
                        cfg["reasoning_effort"] = v
                    elif k == "base_url":
                        cfg["base_url"] = v
                    elif k == "api_key":
                        cfg["api_key"] = v
                env_m = _re.search(r'\[env\](.*?)(?=\n\[|$)', txt, _re.DOTALL)
                if env_m:
                    for m in _re.finditer(r'^\s*([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"', env_m.group(1), _re.MULTILINE):
                        if m.group(1) == "OPENAI_BASE_URL" and not cfg["base_url"]:
                            cfg["base_url"] = m.group(2)
                        elif m.group(1) == "OPENAI_API_KEY" and not cfg["api_key"]:
                            cfg["api_key"] = m.group(2)
        except Exception as e:
            logger.warning("读取 codex config.toml 失败: %s", e)
        cfg["binary"] = bin_path
        cfg["available"] = bool(available)
        return public.return_data(True, data=cfg)

    def codex_cli_save_config(self, get):
        """写入 ~/.codex/config.toml"""
        model = str(get.get('model', 'auto')).strip() or 'auto'
        effort = str(get.get('reasoning_effort', 'max')).strip().lower() or 'max'
        if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
            return public.returnMsg(False, "reasoning_effort 非法")
        base_url = str(get.get('base_url', '')).strip()
        api_key = str(get.get('api_key', '')).strip()
        try:
            bak = ""
            if os.path.exists(self._CODEX_CFG_PATH):
                import shutil as _shutil
                bak = self._CODEX_CFG_PATH + f".bak.{int(time.time())}"
                _shutil.copy2(self._CODEX_CFG_PATH, bak)
            os.makedirs(os.path.dirname(self._CODEX_CFG_PATH), exist_ok=True)
            body = (
                f'model = "{model}"\n'
                f'model_reasoning_effort = "{effort}"\n\n'
                f'[env]\n'
                f'OPENAI_BASE_URL = "{base_url}"\n'
                f'OPENAI_API_KEY = "{api_key}"\n'
            )
            with open(self._CODEX_CFG_PATH, 'w', encoding='utf-8') as f:
                f.write(body)
            import stat as _stat
            os.chmod(self._CODEX_CFG_PATH, _stat.S_IRUSR | _stat.S_IWUSR)
            return public.return_data(True, data={"saved": True, "backup": bak})
        except Exception as e:
            return public.returnMsg(False, f"保存失败: {e!s}")

    def codex_get_status(self, get=None):
        """探测 codex 二进制状态"""
        import shutil as _shutil
        from chat_client.codex_bridge import _codex_path
        bin_path = _codex_path()
        available = False
        if bin_path and os.path.isabs(bin_path) and os.path.exists(bin_path):
            available = os.access(bin_path, os.X_OK)
        elif bin_path and _shutil.which(bin_path):
            available = True
        return public.return_data(True, data={"binary": bin_path, "available": bool(available)})

    def codex_test_exec(self, get=None):
        """跑 codex --version 验证二进制可用"""
        import shutil as _shutil
        from chat_client.codex_bridge import _codex_path
        bin_path = _codex_path()
        if not bin_path:
            return public.returnMsg(False, "未定位到 codex 二进制")
        try:
            proc = subprocess.run(
                [bin_path, "--version"], capture_output=True, text=True, timeout=10
            )
            ver = (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr) else ""
            return public.return_data(True, data={"version": ver, "returncode": proc.returncode})
        except Exception as e:
            return public.returnMsg(False, f"codex 测试失败: {e!s}")

    def simple_chat(self, get):
        if self.plugin_path not in sys.path:
            sys.path.append(self.plugin_path)
        try:
            from chat_client.simple_agent import SimpleAgent
        except ImportError:
            if self.plugin_path not in sys.path:
                sys.path.insert(0, self.plugin_path)
            from chat_client.simple_agent import SimpleAgent

        user_input = get.get('message', '')
        if isinstance(user_input, str):
            try:
                parsed_input = json.loads(user_input)
                if isinstance(parsed_input, list):
                    user_input = parsed_input
                elif isinstance(parsed_input, dict):
                    # 单个 content part（如 {"type":"text","text":"..."}）→ 包装为标准列表格式
                    user_input = [parsed_input]
            except json.JSONDecodeError:
                logger.warning("single_chat user_input JSON 解析失败，已记录", exc_info=True)
        session_id = get.get('session_id', 'simple_session')
        model = get.get('model', '').strip()
        system_prompt = get.get('system_prompt', '')
        prompt_id = get.get('prompt_id', '')
        tools = get.get('tools', '[]')
        try:
            tools = ast.literal_eval(tools) if isinstance(tools, str) else tools
        except (ValueError, SyntaxError):
            tools = []
        if not tools:
            tools = []

        final_system_prompt, prompt_config = self._load_prompt_config(prompt_id, system_prompt)
        if not final_system_prompt:
            final_system_prompt = get.get('system_prompt', '')
        if not final_system_prompt:
            yield self.sse_pack(event="error", data={"msg": "未找到对应助手配置"})
            return
        if not user_input:
            yield self.sse_pack(event="error", data={"msg": "请输入内容"})
            return

        if not model:
            model = prompt_config.get('model_name') or prompt_config.get('model')

        headers = self.config['default_headers'].copy()
        appid = self._get_priority_value('appid', get, prompt_config, headers.get('appid', ''))
        if appid:
            headers['appid'] = appid

        sessions_dir = self._get_priority_value('sessions_dir', get, prompt_config, 'simple_sessions')

        agent_config = {
            "api_key": self._get_priority_value('api_key', get, prompt_config, self.DEFAULT_CONFIG['agent'].get('api_key', '')),
            "base_url": self._get_priority_value('base_url', get, prompt_config, self.DEFAULT_CONFIG['agent'].get('base_url', '')),
            "model_name": model,
            "default_headers": headers,
            "system_prompt": final_system_prompt,
            "temperature": float(self._get_priority_value('temperature', get, prompt_config, self.config['agent'].get('temperature', 1.0))),
            "top_p": float(self._get_priority_value('top_p', get, prompt_config, self.config['agent'].get('top_p', 1.0))),
            "sessions_dir": os.path.join(self.plugin_path, sessions_dir),
            "sliding_window_size": int(self._get_priority_value('sliding_window_size', get, prompt_config, 50)),
            "tools": tools,
            "use_global_rag": self._get_priority_value('use_global_rag', get, prompt_config, ''),
            "global_kb_dir": os.path.join(self.plugin_path, 'memo'),
            "global_kb_agent_id": self._get_priority_value('global_kb_agent_id', get, prompt_config, 'ai-agent'),
            "mem0_api_url": self._get_priority_value('mem0_api_url', get, prompt_config, 'http://localhost:8000'),
            "embedding_api_key": self.config['embedding'].get('embedding_api_key', ''),
            "embedding_base_url": self.config['embedding'].get('embedding_base_url', ''),
            "embedding_model_name": self.config['embedding'].get('embedding_model_name', ''),
        }

        try:
            agent = SimpleAgent(session_id=session_id, config=agent_config)
        except Exception as e:
            yield self.sse_pack(event="error", data={"msg": f"SimpleAgent 初始化失败: {e!s}"})
            return

        try:
            for chunk in agent.chat(user_input):
                if chunk.get("type") == "content":
                    yield self.sse_pack(event="message", data=chunk.get("response", ""))
                elif chunk.get("type") == "reasoning":
                    yield self.sse_pack(event="message_think", data=chunk.get("response", ""))
                elif chunk.get("type") == "error":
                    yield self.sse_pack(event="error", data={"msg": chunk.get("data", "")})
                elif chunk.get("type") == "stop":
                    yield self.sse_pack(event="usage", data={"usage": chunk.get("usage", {})})
                elif chunk.get("type") == "meta_info":
                    yield self.sse_pack(event="meta_info", data={"user_msg_id": chunk.get("user_msg_id"), "ai_msg_id": chunk.get("ai_msg_id")})
                else:
                    yield self.sse_pack(event=chunk.get("type"), data=chunk)
            yield self.sse_pack(event="message_end")
        except Exception as e:
            yield self.sse_pack(event="error", data={"msg": f"聊天发生错误: {e!s}"})
        finally:
            agent.close()

    def single_chat(self, get):
        if self.plugin_path not in sys.path:
            sys.path.append(self.plugin_path)
        try:
            from chat_client.single_agent import SingleAgent
        except ImportError:
            if self.plugin_path not in sys.path:
                sys.path.insert(0, self.plugin_path)
            from chat_client.single_agent import SingleAgent

        prompt = get.get('prompt', '')
        input_text = get.get('input_text', '')
        messages_str = get.get('messages', '')
        model = get.get('model', '').strip()
        temperature_str = get.get('temperature', '')
        top_p_str = get.get('top_p', '')
        json_response_str = get.get('json_response', 'false').lower()
        json_schema_str = get.get('json_schema', '')
        max_tokens_str = get.get('max_tokens', '')
        custom_api_key = get.get('api_key', '')
        custom_base_url = get.get('base_url', '')
        appid = get.get('appid', '')

        if not prompt and not messages_str:
            return public.returnMsg(False, "缺少参数 prompt 或 messages")

        messages = None
        if messages_str:
            try:
                messages = json.loads(messages_str)
                if not isinstance(messages, list):
                    return public.returnMsg(False, "参数 messages 必须是数组格式")
            except json.JSONDecodeError as e:
                return public.returnMsg(False, f"参数 messages 格式错误: {e!s}")

        temperature = 0.7
        if temperature_str:
            try:
                temperature = float(temperature_str)
            except ValueError:
                return public.returnMsg(False, "参数 temperature 格式错误")

        top_p = 1.0
        if top_p_str:
            try:
                top_p = float(top_p_str)
            except ValueError:
                return public.returnMsg(False, "参数 top_p 格式错误")

        json_response = json_response_str in ['true', '1', 'yes']
        json_schema = None
        if json_schema_str:
            try:
                json_schema = json.loads(json_schema_str)
            except json.JSONDecodeError:
                logger.warning("single_chat json_schema 解析失败，已记录", exc_info=True)

        kwargs = {}
        if max_tokens_str:
            try:
                kwargs['max_tokens'] = int(max_tokens_str)
            except ValueError:
                logger.warning("single_chat max_tokens 解析失败，已记录", exc_info=True)

        api_key = custom_api_key if custom_api_key else self.config.get('api_key', '')
        base_url = custom_base_url if custom_base_url else self.config.get('api_base_url', '')

        if not api_key or not base_url:
            return public.returnMsg(False, "缺少 API 配置，请先配置 API Key 和 Base URL")

        headers = self.config['default_headers'].copy()
        if appid:
            headers['appid'] = appid

        if not model:
            # 默认模型以配置文件 default_model 为准，其次取勾选列表第一项
            model = self.config.get('default_model') or (self.config.get('models') or [None])[0]
        if not model:
            return public.returnMsg(False, "尚未配置模型：请先在设置面板勾选模型并保存")

        try:
            agent = SingleAgent(
                api_key=api_key, base_url=base_url, model_name=model,
                default_headers=headers, temperature=temperature, top_p=top_p,
                retry_base_wait=float(self.config['agent'].get('api_retry_base_wait', 1)),
                retry_max_wait=float(self.config['agent'].get('api_retry_max_wait', 10))
            )
            result = agent.chat(
                prompt=prompt if prompt else None,
                input_text=input_text if input_text else None,
                messages=messages, json_response=json_response,
                json_schema=json_schema, **kwargs
            )
            agent.close()
            return public.return_data(True, data=result)
        except Exception as e:
            return public.returnMsg(False, f"调用失败: {e!s}")

    def get_chat_historys(self, get):
        workspace_filter = str(get.get('workspace', '')).strip()
        # mode 隔离：每个 mode 只返回自己 source 的历史，互不串
        #   group   → 仅 native（集团对话 + 集团子代理）
        #   single  → 仅 native（单 Agent 主对话）
        #   opencode/claude/codex → 各自 db 全量
        raw_mode = str(get.get('mode', '')).strip().lower() or 'group'
        mode = raw_mode if raw_mode in ('group', 'single', 'opencode', 'claude', 'codex') else 'group'
        # source 参数（旧兼容，保留）
        source_param = str(get.get('source', '')).strip().lower()
        sessions = []

        # ── opencode 全局历史（仅 opencode 模式）─────────────────────────
        if source_param in ('opencode', '') and mode == 'opencode':
            try:
                from chat_client.opencode_bridge import query_opencode_sessions
                oc_sessions = query_opencode_sessions(workspace_filter)
                for s in oc_sessions:
                    title = s.get("title", "")
                    if title.startswith("New session"):
                        title = "opencode 会话"
                    sessions.append({
                        "session_id": s["id"],
                        "title": title,
                        "timestamp": s.get("time_updated", 0),
                        "time_str": datetime.datetime.fromtimestamp(s.get("time_updated", 0) / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S') if s.get("time_updated") else "",
                        "workspace": s.get("directory", ""),
                        "source": "opencode",
                    })
            except Exception:
                logger.warning("opencode 历史查询异常，跳过", exc_info=True)

        # ── claude 全局历史（仅 claude 模式）──────────────────────
        if source_param in ('claude', '') and mode == 'claude':
            try:
                from chat_client.claude_bridge import query_claude_sessions
                cl_sessions = query_claude_sessions(workspace_filter)
                for s in cl_sessions:
                    sessions.append({
                        "session_id": s["id"],
                        "title": s.get("title", "claude 会话"),
                        "timestamp": s.get("time_updated", 0),
                        "time_str": datetime.datetime.fromtimestamp(s.get("time_updated", 0) / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S') if s.get("time_updated") else "",
                        "workspace": s.get("directory", ""),
                        "source": "claude",
                    })
            except Exception:
                logger.warning("claude 历史查询异常，跳过", exc_info=True)

        # ── codex 全局历史（仅 codex 模式）──────────────────────────
        if source_param in ('codex', '') and mode == 'codex':
            try:
                from chat_client.codex_bridge import query_codex_sessions
                cd_sessions = query_codex_sessions(workspace_filter)
                for s in cd_sessions:
                    ts = s.get("time_updated", 0)
                    sessions.append({
                        "session_id": s["id"],
                        "title": s.get("title", "codex 会话"),
                        "timestamp": ts,
                        "time_str": datetime.datetime.fromtimestamp(ts / 1000).astimezone().strftime('%Y-%m-%d %H:%M:%S') if ts else "",
                        "workspace": s.get("directory", ""),
                        "source": "codex",
                    })
            except Exception:
                logger.warning("codex 历史查询异常，跳过", exc_info=True)

        # ── native 历史（group / single 模式各自过滤）─────────────────────
        if mode in ('group', 'single'):
            pass

        # ── native 历史（从 sessions.json 读取，集团模式）─────────────────────
        # 只有 group/single 模式才读本地 native 会话（集团对话 + 单 Agent + 子代理）
        if mode in ('group', 'single'):
            sessions_dir = os.path.join(self.plugin_path, 'sessions')
            # CLI 会话 ID 前缀（这些是 opencode/claude/codex 模式，不应混入 native 历史）
            _cli_prefixes = ('ses_', 'agent-', 'rollout-')

            def _is_crew_session(sid: str, history, session_path) -> bool:
                """严格识别集团会话：必须有 CreateDepartment/RecruitMember（建立部门结构）或 meta.json source=crew"""
                # 1) meta.json source=crew（最可靠）
                mf = os.path.join(session_path, 'meta.json')
                if os.path.exists(mf):
                    try:
                        if json.load(open(mf)).get('source') == 'crew':
                            return True
                    except Exception:
                        pass
                # 2) history 里有 CreateDepartment 或 RecruitMember（建立部门结构的关键工具）
                if history:
                    for m in history:
                        for tc in (m.get('tool_calls') or []):
                            if not isinstance(tc, dict):
                                continue
                            name = (tc.get('function') or {}).get('name', '') or tc.get('name', '')
                            if name in ('CreateDepartment', 'RecruitMember'):
                                return True
                return False

            if os.path.exists(sessions_dir):
                try:
                    dirs = os.listdir(sessions_dir)
                    dirs.sort(key=lambda x: os.path.getmtime(os.path.join(sessions_dir, x)), reverse=True)
                    for session_id in dirs:
                        # 跳过 CLI 会话 ID 的顶层目录（属于 opencode/claude/codex）
                        if session_id.startswith(_cli_prefixes):
                            continue
                        session_path = os.path.join(sessions_dir, session_id)
                        if not os.path.isdir(session_path):
                            continue
                        session_file = os.path.join(session_path, 'sessions.json')
                        if not os.path.exists(session_file):
                            continue
                        try:
                            mtime = os.path.getmtime(session_file)
                            time_str = datetime.datetime.fromtimestamp(mtime).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                            title = session_id
                            with open(session_file, 'r', encoding='utf-8') as f:
                                history = json.load(f)
                                if history:
                                    for msg in history:
                                        if msg.get('role') == 'user':
                                            content = msg.get('content', '')
                                            if isinstance(content, list):
                                                text_item = next(
                                                    (item for item in content
                                                     if isinstance(item, dict) and item.get('type') == 'text'),
                                                    None
                                                )
                                                content = text_item.get('text', '') if text_item else ''
                                            if not isinstance(content, str):
                                                content = ''
                                            title = content[:20] + '...' if len(content) > 20 else content
                                            break
                            # 从 meta.json 读 workspace（chat_start 时持久化）
                            ws = ''
                            meta_file = os.path.join(session_path, 'meta.json')
                            if os.path.exists(meta_file):
                                try:
                                    with open(meta_file, 'r', encoding='utf-8') as mf:
                                        ws = (json.load(mf) or {}).get('workspace', '') or ''
                                except Exception:
                                    ws = ''
                            item = {
                                "session_id": session_id,
                                "title": title,
                                "timestamp": int(mtime),
                                "time_str": time_str,
                                "workspace": ws,
                                "source": "single",
                                "is_group": _is_crew_session(session_id, history, session_path),
                            }
                            sessions.append(item)
                        except Exception:
                            continue
                except Exception:
                    pass

            # ── 旧版 jobs-only 检测已合并到 _is_crew_session，块删除 ──

            # ── 扫描集团模式子代理会话（读 meta.json 判断来源，避免误判）────
            if os.path.exists(sessions_dir):
                jobs_dir = os.path.join(self.plugin_path, 'jobs')
                try:
                    dirs2 = os.listdir(sessions_dir)
                    for session_id in dirs2:
                        parent_path = os.path.join(sessions_dir, session_id)
                        if not os.path.isdir(parent_path):
                            continue
                        sub_dirs = os.listdir(parent_path)
                        for sub_dir in sub_dirs:
                            sub_path = os.path.join(parent_path, sub_dir)
                            if not os.path.isdir(sub_path):
                                continue
                            meta_file = os.path.join(sub_path, 'meta.json')
                            sub_file = os.path.join(sub_path, 'sessions.json')
                            # 子代理必须有 meta.json（明确标记 source/agent/dept），不能仅靠父 jobs crew_plan 推断
                            if not os.path.exists(meta_file):
                                continue
                            try:
                                if os.path.exists(meta_file):
                                    mtime = os.path.getmtime(meta_file)
                                else:
                                    mtime = 0
                                if os.path.exists(sub_file):
                                    mtime = max(mtime, os.path.getmtime(sub_file))
                                time_str = datetime.datetime.fromtimestamp(mtime).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                                if os.path.exists(meta_file):
                                    with open(meta_file, 'r', encoding='utf-8') as f:
                                        meta = json.load(f)
                                else:
                                    meta = {"source": "crew", "parent": session_id, "agent": "", "dept": ""}
                                title = sub_dir[:36]
                                if os.path.exists(sub_file):
                                    with open(sub_file, 'r', encoding='utf-8') as sf:
                                        history = json.load(sf)
                                        if history:
                                            for msg in history:
                                                if msg.get('role') == 'user':
                                                    content = msg.get('content', '')
                                                    if isinstance(content, list):
                                                        text_item = next(
                                                            (item for item in content
                                                             if isinstance(item, dict) and item.get('type') == 'text'),
                                                            None
                                                        )
                                                        content = text_item.get('text', '') if text_item else ''
                                                    if not isinstance(content, str):
                                                        content = ''
                                                    title = content[:20] + '...' if len(content) > 20 else content
                                                    break
                                sessions.append({
                                    "session_id": sub_dir,
                                    "title": title,
                                    "timestamp": int(mtime),
                                    "time_str": time_str,
                                    # 子代理 workspace 优先读 meta.json，没有则继承父会话
                                    "workspace": meta.get("workspace", "") or "",
                                    "source": meta.get("source", "crew"),
                                    "parent": session_id,
                                    "agent": meta.get("agent", ""),
                                    "dept": meta.get("dept", ""),
                                })
                            except Exception:
                                continue
                except Exception:
                    pass

        # 去重       # 去重：同一 session_id 可能同时出现在 native 和 opencode/claude 历史
        # 先建立 session_id → workspace 映射，供 crew 子代理继承父会话 workspace
        _parent_ws = {}
        for _s in sessions:
            _sid = _s.get('session_id', '')
            _ws = _s.get('workspace', '') or ''
            if _sid and _ws:
                _parent_ws[_sid] = _ws
        for _s in sessions:
            if not _s.get('workspace') and _s.get('parent') and _s['parent'] in _parent_ws:
                _s['workspace'] = _parent_ws[_s['parent']]
        # 优先保留有 is_group 标记或 source=crew 的记录
        seen = {}
        for s in sessions:
            sid = s.get("session_id", "")
            if sid not in seen:
                seen[sid] = s
            else:
                # 保留信息更全的记录（有 is_group 或 source=crew 的优先）
                if s.get("is_group") or s.get("source") == "crew":
                    seen[sid] = s
        sessions = list(seen.values())

        # 按 mode 二次过滤：native 模式下按 is_group / source 切分，CLI 模式只留自己 source
        if mode == 'group':
            # group 只要：集团主对话(is_group=True) + 集团子代理(source=crew)
            sessions = [s for s in sessions if (s.get('source') == 'crew') or (s.get('source') == 'single' and s.get('is_group'))]
        elif mode == 'single':
            # single 只要：单 Agent 主对话（source=single 且 !is_group）
            sessions = [s for s in sessions if s.get('source') == 'single' and not s.get('is_group')]
        else:
            # CLI 模式（opencode/claude/codex）：只留对应 source
            sessions = [s for s in sessions if s.get('source') == mode]

        # 按时间正序排序，返回前 200 条（旧在前，新在后，匹配文件夹模式）
        sessions.sort(key=lambda s: s.get("timestamp", 0))
        return public.return_data(True, data=sessions[:200])

    def get_chat(self, get):
        session_id = get.get('session_id')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")

        sessions_dir = os.path.join(self.plugin_path, 'sessions')
        native_file = os.path.join(sessions_dir, session_id, 'sessions.json')
        # 子代理会话：UUID 且不在 sessions/<id>/，可能在 sessions/<parent>/<id>/
        if not os.path.exists(native_file) and _is_claude_session_id(session_id):
            for parent in os.listdir(sessions_dir) if os.path.isdir(sessions_dir) else []:
                cand = os.path.join(sessions_dir, parent, session_id, 'sessions.json')
                if os.path.exists(cand):
                    native_file = cand
                    break

        # ── native 历史（包括集团子代理）：从 sessions.json 读取 ──────────────
        if os.path.exists(native_file):
            try:
                with open(native_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                try:
                    for msg in history:
                        if msg.get('role') == 'tool':
                            content = msg.get('content')
                            if isinstance(content, list) and len(content) > 0:
                                first_item = content[0]
                                if isinstance(first_item, dict) and first_item.get('type') == 'text':
                                    msg['content'] = first_item.get('text', '')
                    return public.return_data(True, data=history)
                except Exception as e:
                    return public.returnMsg(False, f"读取会话记录失败: {e!s}")
            except Exception:
                return public.return_data(True, data=[])

        # ── opencode 全局历史：从 opencode.db 读取 ─────────────────────────
        if session_id.startswith("ses_"):
            try:
                from chat_client.opencode_bridge import query_opencode_history
                history = query_opencode_history(session_id)
                return public.return_data(True, data=history)
            except Exception:
                return public.return_data(True, data=[])

        # ── claude 全局历史：从 ~/.claude/projects/ jsonl 读取 ───────────────
        if _is_claude_session_id(session_id):
            try:
                from chat_client.claude_bridge import query_claude_history
                history = query_claude_history(session_id)
                return public.return_data(True, data=history)
            except Exception:
                return public.return_data(True, data=[])

# ── codex 全局历史：从 ~/.codex/sessions/ rollout jsonl 读取 ─────────
        if session_id.startswith("rollout-"):
            try:
                from chat_client.codex_bridge import query_codex_history
                history = query_codex_history(session_id)
                return public.return_data(True, data=history)
            except Exception:
                return public.return_data(True, data=[])

    def del_chat(self, get):
        session_id = get.get('session_id')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        # P2-28: session_id 必须只含安全字符，防路径穿越
        if not re.match(r'^[a-zA-Z0-9_-]{1,128}$', str(session_id)):
            return public.returnMsg(False, "session_id 格式不合法（仅允许字母数字、下划线、短横线，最长128字符）")

        # ── opencode 会话：从全局 opencode.db 删除 ──────────────────────
        if session_id.startswith("ses_"):
            import sqlite3
            db_path = os.path.expanduser("~/.local/share/opencode/opencode.db")
            if not os.path.exists(db_path):
                return public.returnMsg(False, "opencode 数据库不存在")
            # P2-30: 用 with 语句保证 conn 在任何情况下都被关闭
            try:
                with sqlite3.connect(db_path, timeout=5) as conn:
                    # 外键级联：删 session 会连带删 message/part/todo 等
                    cur = conn.execute("DELETE FROM session WHERE id = ?", (session_id,))
                    conn.commit()
                    if cur.rowcount == 0:
                        return public.returnMsg(False, "会话不存在")
                return public.returnMsg(True, "删除成功")
            except Exception as e:
                return public.returnMsg(False, f"删除失败: {e!s}")

        # ── claude 会话：从 ~/.claude/projects/ 删除对应 jsonl 文件 ──────
        if _is_claude_session_id(session_id):
            import glob as _glob
            hits = _glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
            for p in hits:
                try:
                    os.remove(p)
                    return public.returnMsg(True, "删除成功")
                except Exception as e:
                    return public.returnMsg(False, f"删除失败: {e!s}")
            return public.returnMsg(False, "claude 会话文件不存在")

# ── codex 会话：从 ~/.codex/sessions/ 删除对应 rollout jsonl ──────
        if session_id.startswith("rollout-"):
            import glob as _glob
            hits = _glob.glob(os.path.expanduser(f"~/.codex/sessions/**/{session_id}.jsonl"), recursive=True)
            for p in hits:
                try:
                    os.remove(p)
                    return public.returnMsg(True, "删除成功")
                except Exception as e:
                    return public.returnMsg(False, f"删除失败: {e!s}")
            return public.returnMsg(False, "codex 会话文件不存在")

        # ── native 会话：删除本地 sessions 目录 ──────────────────────────
        import shutil
        custom_sessions_dir = get.get('sessions_dir', '')
        sessions_dir = custom_sessions_dir if custom_sessions_dir else 'sessions'
        session_dir = os.path.join(self.plugin_path, sessions_dir, session_id)
        if not os.path.exists(session_dir):
            return public.returnMsg(False, "会话不存在")
        try:
            shutil.rmtree(session_dir)
            return public.returnMsg(True, "删除成功")
        except Exception as e:
            return public.returnMsg(False, f"删除失败: {e!s}")

    def del_chat_batch(self, get):
        ids = get.get('session_ids')
        if not isinstance(ids, list) or not ids:
            return public.returnMsg(False, "缺少 session_ids")
        deleted, failed = [], []
        for sid in ids:
            sid = str(sid).strip()
            if not sid:
                continue
            res = self.del_chat({"session_id": sid})
            if res.get("status"):
                deleted.append(sid)
            else:
                failed.append(sid)
        return public.return_data(True, data={
            "deleted": deleted,
            "failed": failed,
            "count": len(deleted),
        })

    def del_chat_msg(self, get):
        """删除单个消息（native 会话：从 sessions/<id>/sessions.json 过滤掉该消息）"""
        session_id = get.get('session_id')
        message_id = get.get('id')
        if not session_id:
            return public.returnMsg(False, "缺少参数 session_id")
        if not message_id:
            return public.returnMsg(False, "缺少参数 id")
        # 防路径穿越：session_id 仅允许安全字符
        if not re.match(r'^[a-zA-Z0-9_-]{1,128}$', str(session_id)):
            return public.returnMsg(False, "session_id 格式不合法")
        sessions_dir = get.get('sessions_dir', '') or 'sessions'
        session_file = os.path.join(self.plugin_path, sessions_dir, session_id, 'sessions.json')
        if not os.path.exists(session_file):
            return public.returnMsg(False, "会话记录不存在")
        try:
            with open(session_file, 'r', encoding='utf-8') as f:
                history = json.load(f)
            original_len = len(history)
            history = [m for m in history if m.get('id') != message_id]
            if len(history) == original_len:
                return public.returnMsg(False, "未找到指定消息")
            with open(session_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=4)
            return public.returnMsg(True, "删除成功")
        except Exception as e:
            return public.returnMsg(False, f"删除失败: {e!s}")

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

app = FastAPI(title="AI Agent Standalone", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MCP 路由：统一由 mcp_routes 模块提供（避免多份重复实现）
from mcp_routes import register_mcp_routes
register_mcp_routes(app)


agent_main = AgentMain()

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

_preload_thread = threading.Thread(target=_preload_mcp, name="mcp-preload", daemon=True)
_preload_thread.start()


# ---- 智能体定时任务调度线程 ----
def _scheduled_runner(task: dict):
    """到点执行：以独立会话启动集团任务"""
    sid = f"sched_{task.get('id','x')}_{int(time.time())}"
    payload = {
        "session_id": sid,
        "message": str(task.get("objective", "")),
        "model": str(task.get("model", "")),
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


@app.get("/")
async def index():
    """Serve the main HTML page."""
    index_path = os.path.join(BASE_DIR, "index.html")
    response = FileResponse(index_path, media_type="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/config")
async def api_config():
    return JSONResponse(agent_main.get_config())


@app.post("/api/config")
async def api_set_config(request: Request):
    params = await _params(request)
    config_str = params.get('config', '')
    return JSONResponse(agent_main.set_config(config_str))


@app.get("/api/models")
async def api_models(base_url: str = "", key: str = ""):
    return JSONResponse(agent_main.get_models(base_url=base_url, key=key))


@app.get("/api/agents")
async def api_agents():
    return JSONResponse(agent_main.agent_list())


@app.get("/api/tools")
async def api_tools():
    # get_tool_list 内部可能等待 MCP 预加载线程（同步 join），放入线程池避免阻塞事件循环
    return JSONResponse(await asyncio.to_thread(agent_main.get_tool_list))


@app.get("/api/skills")
async def api_skills():
    return JSONResponse(agent_main.get_skill_list())


@app.post("/api/skills/install")
async def api_skill_install(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.skill_install(params))


@app.post("/api/skills/uninstall")
async def api_skill_uninstall(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.skill_uninstall(params))


@app.post("/api/skills/install_url")
async def api_skill_install_url(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.skill_install_url(params))


@app.post("/api/skills/status")
async def api_skill_status(request: Request):
    """切换技能启用状态"""
    params = await _params(request)
    # 前端字段为 skill_name/enabled，与 agent_main.set_skill_status 一致，原样透传
    return JSONResponse(agent_main.set_skill_status(params))


@app.post("/api/tools/show_status")
async def api_tool_show_status(request: Request):
    """切换工具显示状态"""
    params = await _params(request)
    # 前端传 id（单工具）或 category（分类）；agent_main 用 tool_id，做归一化
    if params.get("id"):
        params["tool_id"] = params.pop("id")
    return JSONResponse(agent_main.set_tool_show_status(params))


# ---- MCP Server 管理（读写 mcp/config.json + 市场）----

@app.get("/api/mcp/servers")
async def api_mcp_servers():
    from chat_client.mcp_manager import mcp_manager
    return JSONResponse(mcp_manager.list_servers())


@app.post("/api/mcp/servers/add")
async def api_mcp_server_add(request: Request):
    params = await _params(request)
    from chat_client.mcp_manager import mcp_manager
    name = str(params.get("name", "") or "").strip()
    config_raw = params.get("config", "")
    overwrite = str(params.get("overwrite", "")).lower() in ("1", "true", "yes")
    if not config_raw:
        return JSONResponse({"status": False, "msg": "缺少参数 config"})
    try:
        config = json.loads(config_raw) if isinstance(config_raw, str) else config_raw
    except Exception:
        return JSONResponse({"status": False, "msg": "config 不是合法 JSON"})
    if not isinstance(config, dict):
        return JSONResponse({"status": False, "msg": "config 必须是 JSON 对象"})
    return JSONResponse(mcp_manager.add_server(name, config, overwrite=overwrite))


@app.post("/api/mcp/servers/remove")
async def api_mcp_server_remove(request: Request):
    params = await _params(request)
    from chat_client.mcp_manager import mcp_manager
    name = str(params.get('name', '') or '').strip()
    return JSONResponse(mcp_manager.remove_server(name))


@app.post("/api/mcp/servers/install")
async def api_mcp_server_install(request: Request):
    """从市场一键安装：按 mcp_id 生成配置并写入 mcp/config.json"""
    params = await _params(request)
    from chat_client.mcp_manager import mcp_manager
    mcp_id = str(params.get('mcp_id', '') or '').strip()
    overwrite = str(params.get("overwrite", "")).lower() in ("1", "true", "yes")
    if not mcp_id:
        return JSONResponse({"status": False, "msg": "缺少参数 mcp_id"})
    return JSONResponse(await asyncio.to_thread(
        mcp_manager.install_from_market, mcp_id, overwrite))


@app.get("/api/mcp/market")
async def api_mcp_market():
    from chat_client.mcp_manager import mcp_manager
    return JSONResponse(await asyncio.to_thread(mcp_manager.fetch_market_list))


@app.get("/api/crew/agents")
async def api_crew_agents():
    return JSONResponse(agent_main.crew_agents_list())


@app.post("/api/crew/save")
async def api_crew_save(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.crew_agent_save(params))


@app.post("/api/crew/delete")
async def api_crew_delete(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.crew_agent_delete(params))


@app.get("/api/org")
async def api_org():
    return JSONResponse(agent_main.org_chart())


@app.post("/api/crew/dept_save")
async def api_crew_dept_save(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.crew_dept_save(params))


@app.post("/api/crew/dept_delete")
async def api_crew_dept_delete(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.crew_dept_delete(params))


@app.post("/api/hotreload")
async def api_hotreload(request: Request):
    params = await _params(request)
    target = str(params.get("target", "all")).strip().lower()
    targets = list(chat_client_hotreload._LOADERS.keys()) if target == "all" else [target]
    results = chat_client_hotreload.reload_targets(targets)
    ok = all(not v.startswith("失败") for v in results.values())
    return JSONResponse({"status": ok, "data": results, "msg": "; ".join(f"{k}: {v}" for k, v in results.items())})


@app.get("/api/chat")
async def api_chat_get(request: Request):
    params = dict(request.query_params)
    return StreamingResponse(
        agent_main.chat(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.post("/api/chat")
async def api_chat_post(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin chat SSE 参数提取异常", exc_info=True)
    return StreamingResponse(
        agent_main.chat(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.get("/api/simple_chat")
async def api_simple_chat_get(request: Request):
    params = dict(request.query_params)
    return StreamingResponse(
        agent_main.simple_chat(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.post("/api/simple_chat")
async def api_simple_chat_post(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin simple_chat 参数提取异常", exc_info=True)
    return StreamingResponse(
        agent_main.simple_chat(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.post("/api/single_chat")
async def api_single_chat(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin single_chat 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.single_chat(params))


@app.get("/api/opencode/config")
async def api_opencode_config_get(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.opencode_get_config(params))


@app.post("/api/opencode/config")
async def api_opencode_config_post(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin opencode_save 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.opencode_save_config(params))


@app.get("/api/claude/config")
async def api_claude_config_get(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.claude_get_config(params))


@app.post("/api/claude/config")
async def api_claude_config_post(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin claude_save 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.claude_save_config(params))


@app.get("/api/opencode/cli-config")
async def api_opencode_cli_config_get(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.opencode_cli_get_config(params))


@app.post("/api/opencode/cli-config")
async def api_opencode_cli_config_post(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin opencode_cli_save 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.opencode_cli_save_config(params))


# ── codex 管理面板路由（与 static/codex_panel.html 对应）─────────────────
@app.get("/api/codex/cli-config")
async def api_codex_cli_config_get(request: Request):
    return JSONResponse(agent_main.codex_cli_get_config({}))


@app.post("/api/codex/cli-config")
async def api_codex_cli_config_post(request: Request):
    params = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            params = body
    except Exception:
        logger.warning("plugin codex_cli_save 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.codex_cli_save_config(params))


@app.get("/api/codex/status")
async def api_codex_status(request: Request):
    return JSONResponse(agent_main.codex_get_status({}))


@app.post("/api/codex/test")
async def api_codex_test(request: Request):
    return JSONResponse(agent_main.codex_test_exec({}))


@app.get("/api/chat/history")
async def api_chat_history(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.get_chat_historys(params))


# ---------------- 后台任务式聊天 ----------------

@app.post("/api/chat/start")
async def api_chat_start(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.chat_start(params))


@app.get("/api/chat/events")
async def api_chat_events(request: Request):
    params = dict(request.query_params)
    return StreamingResponse(
        agent_main.chat_events(params),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.get("/api/chat/status")
async def api_chat_status(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.chat_status(params))


@app.post("/api/chat/stop")
async def api_chat_stop(request: Request):
    params = await _params(request)
    return JSONResponse(agent_main.chat_stop(params))


@app.get("/api/opencode/status")
async def api_opencode_status(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.opencode_status(params))


@app.get("/api/chat/messages")
async def api_chat_messages(request: Request):
    params = dict(request.query_params)
    return JSONResponse(agent_main.get_chat(params))


@app.post("/api/chat/delete")
async def api_chat_delete(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin del_chat 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.del_chat(params))


@app.post("/api/chat/delete_batch")
async def api_chat_delete_batch(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(public.returnMsg(False, "参数错误"))
    if not isinstance(body, dict):
        return JSONResponse(public.returnMsg(False, "参数错误"))
    ids = body.get("session_ids")
    if not isinstance(ids, list) or not ids:
        return JSONResponse(public.returnMsg(False, "缺少 session_ids"))
    deleted, failed = [], []
    for sid in ids:
        sid = str(sid).strip()
        if not sid:
            continue
        res = agent_main.del_chat({"session_id": sid})
        if res.get("status"):
            deleted.append(sid)
        else:
            failed.append(sid)
    return JSONResponse(public.return_data(True, data={
        "deleted": deleted,
        "failed": failed,
        "count": len(deleted),
    }))


@app.post("/api/chat/delete_message")
async def api_chat_delete_message(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("delete_message 参数提取异常", exc_info=True)
    return JSONResponse(agent_main.del_chat_msg(params))


# ============================================================
# 文件能力：浏览器端文件选择 / 本地文件上传
# ============================================================
# 允许浏览的根目录白名单（防目录穿越），用户请求的路径必须落在这些根之下
_FILE_BROWSE_ROOTS = [
    os.path.abspath(os.path.join(BASE_DIR, "workspace")),
    os.path.abspath(os.path.expanduser("~")),
    "/tmp",
]


def _safe_resolve(requested: str):
    """将请求路径约束在白名单根目录内，返回真实绝对路径；越界返回 None"""
    if not requested:
        return _FILE_BROWSE_ROOTS[0]
    req = os.path.abspath(os.path.expanduser(requested))
    for root in _FILE_BROWSE_ROOTS:
        try:
            if req == root or req.startswith(root + os.sep):
                return req
        except Exception:
            logger.warning("处理时跳过异常", exc_info=True)
            continue
    return None


# 文件服务（/api/files/raw）允许访问的根目录：浏览白名单 + 上传目录
_SERVE_ROOTS = _FILE_BROWSE_ROOTS + [os.path.abspath(os.path.join(BASE_DIR, "uploads"))]


def _safe_resolve_serve(requested: str):
    if not requested:
        return None
    req = os.path.abspath(os.path.expanduser(requested))
    for root in _SERVE_ROOTS:
        try:
            if req == root or req.startswith(root + os.sep):
                return req
        except Exception:
            logger.warning("处理时跳过异常", exc_info=True)
            continue
    return None


@app.get("/api/files/raw")
async def api_files_raw(path: str = ""):
    """安全提供文件内容（图片/视频/音频/文档预览下载）。path 必须为白名单内绝对/相对路径。"""
    if not path:
        return JSONResponse(public.returnMsg(False, "缺少 path"))
    # 兼容前端传入相对 BASE_DIR 的路径
    if not os.path.isabs(path):
        cand = os.path.abspath(os.path.join(BASE_DIR, path))
    else:
        cand = path
    real = _safe_resolve_serve(cand)
    if real is None or not os.path.isfile(real):
        return JSONResponse(public.returnMsg(False, "文件不存在或不在允许访问的范围内"))
    try:
        return FileResponse(real)
    except Exception as e:
        return JSONResponse(public.returnMsg(False, f"读取文件失败: {e!s}"))


@app.get("/api/files/browse")
async def api_files_browse(path: str = ""):
    target = _safe_resolve(path)
    if target is None:
        return JSONResponse(public.returnMsg(False, "路径不在允许浏览的范围内"))
    if not os.path.isdir(target):
        # 若是文件，则定位到其父目录并预选该文件
        parent = os.path.dirname(target)
        preselect = os.path.basename(target) if os.path.isfile(target) else ""
        target = parent if os.path.isdir(parent) else _FILE_BROWSE_ROOTS[0]
    else:
        preselect = ""
    try:
        entries = []
        for name in sorted(os.listdir(target)):
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            try:
                st = os.stat(full)
            except Exception:
                logger.warning("处理时跳过异常", exc_info=True)
                continue
            is_dir = os.path.isdir(full)
            entries.append({
                "name": name,
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else st.st_size,
                "mtime": int(st.st_mtime),
            })
        # 目录在前、文件在后
        entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
        parent = os.path.dirname(target)
        return JSONResponse(public.return_data(True, data={
            "path": target,
            "parent": parent if parent != target else "",
            "preselect": preselect,
            "items": entries,
        }))
    except Exception as e:
        return JSONResponse(public.returnMsg(False, f"浏览失败: {e!s}"))


@app.post("/api/files/upload")
async def api_files_upload(file: UploadFile = File(...)):
    MAX_UPLOAD_SIZE = int(os.environ.get("BT_MAX_UPLOAD_SIZE_MB", "100")) * 1024 * 1024  # 默认 100MB
    upload_dir = os.path.join(BASE_DIR, "uploads")
    try:
        os.makedirs(upload_dir, exist_ok=True)
    except Exception:
        return JSONResponse(public.returnMsg(False, "无法创建上传目录"))
    # 仅取基名，杜绝路径穿越
    fname = os.path.basename(file.filename or "upload.bin")
    # 避免重名覆盖
    dest = os.path.join(upload_dir, fname)
    if os.path.exists(dest):
        base, ext = os.path.splitext(fname)
        dest = os.path.join(upload_dir, f"{base}_{int(time.time())}{ext}")
    # P1-4: 校验 Content-Length 头防大文件 OOM（未设限时仍允许但用流式读取）
    content_length = file.size
    if content_length is not None and content_length > MAX_UPLOAD_SIZE:
        return JSONResponse(public.returnMsg(False, f"文件大小超过限制（{MAX_UPLOAD_SIZE // 1024 // 1024}MB）"))
    try:
        # 流式写入，避免一次性读入内存
        size = 0
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(8192)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    os.remove(dest)
                    return JSONResponse(public.returnMsg(False, f"文件大小超过限制（{MAX_UPLOAD_SIZE // 1024 // 1024}MB）"))
                out.write(chunk)
        # 返回相对 BASE_DIR 的路径，供 agent 读取
        rel = os.path.relpath(dest, BASE_DIR)
        return JSONResponse(public.return_data(True, data={
            "name": fname,
            "path": rel,
            "url": "/api/files/raw?path=" + urllib.parse.quote(rel),
            "size": size,
        }))
    except Exception as e:
        return JSONResponse(public.returnMsg(False, f"上传失败: {e!s}"))


@app.get("/api/agent/run")
async def api_agent_run_get(request: Request):
    params = dict(request.query_params)
    return StreamingResponse(
        agent_main.run_agent(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


@app.post("/api/agent/run")
async def api_agent_run_post(request: Request):
    params = dict(request.query_params)
    try:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    except Exception:
        logger.warning("plugin SSE streaming 参数提取异常", exc_info=True)
    return StreamingResponse(
        agent_main.run_agent(params),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
    )


# Static files
static_dir = os.path.join(BASE_DIR, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="AI Agent Standalone Web Server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=9876, help="Listen port (default: 9876)")
    args = parser.parse_args()

    print(f"AI Agent Standalone starting on http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
