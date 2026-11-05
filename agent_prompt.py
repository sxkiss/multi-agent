"""AgentMain 提示词构建职责。

优-1 第十六阶段：从 web_server.py 的 AgentMain 拆出的系统提示词解析、
RAG 上下文注入、图片内容块归一化与聊天 Agent 装配；通过 mixin 复用
self.config / self.plugin_path 及 WorkspaceMixin / OrgSkillMixin 的方法。

注意：方法体内除 self.* 外还引用了渠道判定、小程序人设等模块级辅助符号，
它们统一定义在 channel_utils.py，本模块单向依赖该模块。
"""
from __future__ import annotations

import ast
import datetime
import json
import logging
import os
import sys

import public

from channel_utils import (
    _MINIPROGRAM_SINGLE_PERSONA,
    _append_miniprogram_guard,
    _client_channel,
    _is_terminal_channel,
)

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class PromptMixin:
    _IMAGE_EXT_MIME = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    }
    _MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 单图 8MB：base64 会放大 ~33%，防撑爆请求


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


