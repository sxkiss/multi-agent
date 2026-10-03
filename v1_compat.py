"""
OpenAI 兼容层（/v1/models + /v1/chat/completions）

设计要点
--------
1. 模型列表 = 提示词模板
   `GET /v1/models` 返回所有可用提示词模板，三类来源：
     - 内置模板：chat_client.opencode_templates.OPENCODE_TEMPLATES
       （default / gpt5.5 / unrestricted_jeli / hermes）
     - 自定义模板：config.json 的 opencode.templates / single.templates
       （用户在设置面板添加，如 xianyu）
     - 文件模板：prompts/*.md|txt（文件名去扩展名，如 agent_shell）
   这样外部 OpenAI SDK 里 `model="hermes"` 或 `model="agent_shell"`
   即等价于"用该提示词模板"。

2. 鉴权 = 现有登录 token
   复用 auth.verify_token（HS256）。与全局中间件保持一致：
   - auth.enabled=true  时严格校验 token；
   - auth.enabled=false 时中间件不放行拦截，这里同样只做"非空存在性"校验，
     避免新接口凭空收紧既有部署行为。

3. 对话流程
   model(模板) + messages → Agent(single 模式, cwd=HERMES 默认目录) → 标准 OpenAI 响应。
   single 模式 = Agent 直接使用全部工具、不做多 Agent 编排（见 web_server._build_chat_agent）。

4. 流式 / 非流式都支持
   直接消费 Agent.chat() 生成器（不经 ChatJob 队列——那是给浏览器 SSE 轮询用的），
   把内部 chunk（content / tool_call / tool_result / stop / error）映射为
   OpenAI chat.completion / chat.completion.chunk，结尾补 finish_reason 与 usage。

5. 标准协议约束
   请求体只接受标准 OpenAI 参数（model/messages/stream/temperature/top_p/max_tokens）。
   base_url / api_key / cwd / session_id 均不来自请求体：
   - base_url / api_key：由服务端 config.json 注入（get 级 > 模板 frontmatter），
     避免模板里失效的旧 base_url 把对话打错地址；
   - cwd：固定 HERMES_DIR；
   - session_id：按 model 派生稳定会话（同一模板复用上下文）。
"""

import json
import logging
import os
import secrets
import time
import uuid

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# 默认工作目录：单 agent 模式的 cwd
HERMES_DIR = "/home/sxkiss/.hermes"

# 扫描提示词模板时的合法扩展名（与 web_server._load_prompt_config 保持一致）
_PROMPT_EXTS = (".md", ".txt")


def _prompts_dir(plugin_path: str) -> str:
    return os.path.join(plugin_path, "prompts")


def list_prompt_templates(plugin_path: str) -> list[dict]:
    """列出全部提示词模板（即 /v1/models 的模型列表）。

    模板来源三类（与 web_server._resolve_template_system_prompt 的解析顺序一致）：
      1. 内置模板：chat_client.opencode_templates.OPENCODE_TEMPLATES
         （default / gpt5.5 / unrestricted_jeli / hermes）
      2. 自定义模板：config.json 的 opencode.templates / single.templates（如 xianyu）
      3. 文件模板：prompts/*.md|txt（文件名去扩展名即 id）

    返回 OpenAI /v1/models 结构：
        {"object": "list", "data": [{"id": ..., "object": "model", ...}]}
    """
    import time as _time
    now = int(_time.time())
    data = []
    seen = set()

    # 1) 内置模板
    try:
        from chat_client.opencode_templates import OPENCODE_TEMPLATES
        for name in sorted(OPENCODE_TEMPLATES.keys()):
            if name in seen:
                continue
            seen.add(name)
            data.append({
                "id": name,
                "object": "model",
                "created": now,
                "owned_by": "bt-agent",
            })
    except Exception:
        logger.warning("[v1] 内置模板加载失败，已记录", exc_info=True)

    # 2) 自定义模板：config.json 的 opencode.templates / single.templates（如 xianyu）
    #    优先级高于内置模板，与 web_server._resolve_template_system_prompt 一致。
    _cfgv = {}
    try:
        _cfg_path = os.path.join(plugin_path, "config.json")
        if os.path.isfile(_cfg_path):
            with open(_cfg_path, "r", encoding="utf-8") as f:
                _cfgv = json.load(f) or {}
    except Exception:
        logger.warning("[v1] 读取 config.json 失败，跳过自定义模板", exc_info=True)
    for cfg_key in ("opencode", "single"):
        seg = (_cfgv.get(cfg_key) or {}) if isinstance(_cfgv, dict) else {}
        if not isinstance(seg, dict):
            continue
        customs = seg.get("templates") or {}
        if not isinstance(customs, dict):
            continue
        for name in sorted(customs.keys()):
            if name in seen:
                continue
            seen.add(name)
            data.append({
                "id": name,
                "object": "model",
                "created": now,
                "owned_by": "bt-agent",
            })

    # 3) prompts 目录下的文件模板
    prompts_dir = _prompts_dir(plugin_path)
    if not os.path.isdir(prompts_dir):
        logger.warning("[v1] prompts 目录不存在: %s", prompts_dir)
        return data

    for fn in sorted(os.listdir(prompts_dir)):
        name, ext = os.path.splitext(fn)
        if ext.lower() not in _PROMPT_EXTS:
            continue
        # 排除文档类文件（INDEX 等仅作说明、无 frontmatter/无提示词的）
        if name.upper() in ("INDEX", "README", "TEMPLATE"):
            continue
        full = os.path.join(prompts_dir, fn)
        if not os.path.isfile(full):
            continue
        if name in seen:
            continue
        seen.add(name)
        try:
            created = int(os.path.getmtime(full))
        except Exception:
            created = now
        data.append({
            "id": name,
            "object": "model",
            "created": created,
            "owned_by": "bt-agent",
        })
    return data


def _openai_error(message: str, code: int = 400, etype: str = "invalid_request_error"):
    """OpenAI 风格错误体。"""
    return JSONResponse(
        status_code=code,
        content={"error": {"message": message, "type": etype, "code": code}},
    )


def _extract_token(request: Request) -> str:
    """从 Authorization: Bearer <token> 提取令牌。"""
    auth = request.headers.get("authorization", "") or request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (auth or "").strip()


def _verify(token: str, base_dir: str) -> bool:
    """校验 /v1/* 的 api_key。

    接受两种凭据（任一通过即可）：
      1. 管理 key：auth.get_admin_api_key() 生成的固定串（推荐，长期有效）
      2. 登录 token：/api/auth/login 换取的 JWT（与既有登录态兼容）

    与全局鉴权中间件（auth.register_auth）行为对齐：
    未启用鉴权时只要求 token 非空，不引入更严格的策略。
    """
    if not token:
        return False
    try:
        import auth as _auth
        cfg = _auth.load_auth(base_dir)
        if not cfg.get("enabled", False):
            # 鉴权未开启：保持一致宽松（中间件此时也不拦截）
            return True
        # 1) 固定管理 key（常量时间比较，避免时序侧信道）
        try:
            admin_key = _auth.get_admin_api_key(base_dir)
            if admin_key and secrets.compare_digest(token, admin_key):
                return True
        except Exception:
            logger.warning("[v1] 管理 key 校验异常，已记录", exc_info=True)
        # 2) 登录 token
        return _auth.verify_token(base_dir, token) is not None
    except Exception:
        logger.warning("[v1] token 校验异常，已记录", exc_info=True)
        return False


def _split_messages(messages: list) -> tuple[str, list[dict]]:
    """拆出 system 提示词，并把历史消息整理成 agent 可消费的结构。

    OpenAI 入参里 messages 是完整对话历史；本项目的 Agent.chat() 接受
    str 或 OpenAI 风格 messages 列表（见 chat_client/agent.py 的 chat 签名），
    因此这里原样保留列表，仅把最后一条 user 内容提取出来做日志/空值校验。
    """
    system_prompt = ""
    for m in messages or []:
        if isinstance(m, dict) and str(m.get("role", "")) == "system":
            system_prompt += (m.get("content") or "")
    return system_prompt, list(messages or [])


def _last_user_content(messages: list) -> str:
    """取最后一条 user 消息的文本（兼容 content 为多模态块数组的情况）。"""
    for m in reversed(messages or []):
        if not isinstance(m, dict):
            continue
        if str(m.get("role", "")) != "user":
            continue
        c = m.get("content")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            parts = []
            for blk in c:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    parts.append(str(blk.get("text", "")))
            return "\n".join(parts).strip()
    return ""


def _build_agent(main, model_id: str, messages: list, session_id: str, cwd: str):
    """按 OpenAI 请求构造 single 模式 Agent。

    复用 web_server._build_chat_agent 的既有逻辑（模板解析、模型回退、
    workspace 收敛、工具装配），只覆盖本次接口需要的字段，避免另起一套
    导致行为走样。

    标准协议约定：base_url / api_key / cwd / session_id 均不来自请求体，
    而由服务端 config.json 决定，避免调用方传入非标准参数，也避免被
    模板 frontmatter 里失效的旧 base_url 覆盖。
    """
    system_prompt, _ = _split_messages(messages)

    get = {
        "session_id": session_id,
        "mode": "single",
        "message": _last_user_content(messages),
        # 本接口的 model 语义是提示词模板 id。
        # 模板分两类，解析路径不同（与 web_server 一致）：
        #   - 内置模板（OPENCODE_TEMPLATES）：走 get["template"]
        #     → _resolve_template_system_prompt(cfg_key='opencode')
        #   - 文件模板（prompts/*.md）：走 get["prompt_id"]
        #     → _load_prompt_config
        # 真实模型名由服务端 config.default_model（get 级注入），
        # 防止被模板 frontmatter 里失效的 model_name（如 qwen3.5-plus）
        # 覆盖后打到上游不支持的模型名 → 503。
        "system_prompt": system_prompt,
        "workspace": cwd,
        "code_mode": True,
    }
    try:
        from chat_client.opencode_templates import OPENCODE_TEMPLATES
        # 自定义模板（config.json opencode.templates / single.templates）
        # 优先于内置模板，与 web_server._resolve_template_system_prompt 一致
        _customs = {}
        try:
            _cfgv = getattr(main, "config", None) or {}
            for _ck in ("opencode", "single"):
                _seg = _cfgv.get(_ck) or {}
                if isinstance(_seg, dict):
                    _t = _seg.get("templates") or {}
                    if isinstance(_t, dict):
                        _customs.update(_t)
        except Exception:
            pass
        if model_id in _customs or model_id in OPENCODE_TEMPLATES:
            get["template"] = model_id
        else:
            get["prompt_id"] = model_id
    except Exception:
        get["prompt_id"] = model_id
    # 服务端注入上游（优先级 get > 模板 frontmatter > config 默认），
    # 强制覆盖模板 frontmatter 里的失效地址，保证对话落到正确上游。
    _cfg = getattr(main, "config", None) or {}
    if _cfg.get("api_base_url"):
        get["base_url"] = _cfg["api_base_url"]
    if _cfg.get("api_key"):
        get["api_key"] = _cfg["api_key"]
    _model = (_cfg.get("default_model") or "").strip() or (
        (_cfg.get("models") or [None])[0] or ""
    ).strip()
    if _model:
        get["model"] = _model

    agent, user_input, err = main._build_chat_agent(get)
    if err:
        msg = (err.get("data") or {}).get("msg", "构建 Agent 失败")
        raise RuntimeError(msg)
    return agent, user_input


def _mk_chunk(chat_id: str, model_id: str, created: int, delta: dict,
              finish_reason: str | None = None, usage: dict | None = None):
    """构造 OpenAI chat.completion.chunk。"""
    body = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_id,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    if usage:
        body["usage"] = usage
    return body


def _mk_completion(chat_id: str, model_id: str, created: int, content: str,
                   finish_reason: str, usage: dict):
    """构造非流式 OpenAI chat.completion。"""
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": model_id,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "logprobs": None,
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def register_v1_routes(app, main, base_dir: str):
    """挂载 /v1/* 路由。main 为 AgentMain 实例（web_server.agent_main）。"""

    @app.get("/v1/models")
    async def v1_models(request: Request):
        token = _extract_token(request)
        if not _verify(token, base_dir):
            return _openai_error("未授权：请提供有效登录 token", 401, "invalid_api_key")
        models = list_prompt_templates(main.plugin_path)
        return JSONResponse({"object": "list", "data": models})

    @app.post("/v1/chat/completions")
    async def v1_chat_completions(request: Request):
        token = _extract_token(request)
        if not _verify(token, base_dir):
            return _openai_error("未授权：请提供有效登录 token", 401, "invalid_api_key")

        try:
            body = await request.json()
        except Exception:
            return _openai_error("请求体不是合法 JSON", 400)
        if not isinstance(body, dict):
            return _openai_error("请求体必须是 JSON 对象", 400)

        model_id = str(body.get("model", "")).strip()
        if not model_id:
            return _openai_error("缺少参数 model（=提示词模板名）", 400)

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return _openai_error("缺少参数 messages", 400)

        # 模板存在性校验：model 即模板 id，不存在时给出可选列表便于排错
        available = {m["id"] for m in list_prompt_templates(main.plugin_path)}
        if available and model_id not in available:
            return _openai_error(
                f"未知模板 '{model_id}'，可用：{', '.join(sorted(available))}", 404
            )

        stream = bool(body.get("stream", False))
        # OpenAI 协议无 session 概念。会话由服务端按 model 派生稳定会话
        # （同一模板复用上下文），不暴露给调用方。
        session_id = f"v1_{model_id}"
        # 默认工作目录由服务端固定，不从请求体取
        cwd = HERMES_DIR
        # 标准 OpenAI 采样参数透传（可选）
        std_sampling = {}
        if body.get("temperature") is not None:
            std_sampling["temperature"] = float(body["temperature"])
        if body.get("top_p") is not None:
            std_sampling["top_p"] = float(body["top_p"])
        if body.get("max_tokens") is not None:
            std_sampling["max_tokens"] = int(body["max_tokens"])

        chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        try:
            agent, user_input = _build_agent(main, model_id, messages, session_id, cwd)
        except RuntimeError as e:
            return _openai_error(str(e), 400)
        except Exception as e:
            logger.warning("[v1] Agent 构建异常，已记录", exc_info=True)
            return _openai_error(f"Agent 构建失败: {e!s}", 500, "server_error")

        # 标准采样参数注入 agent 配置（仅透传合法值，不影响既有流程）
        try:
            for _k, _v in std_sampling.items():
                if _v is not None:
                    agent.config[_k] = _v
                    if _k == "temperature":
                        agent.temperature = _v
                    elif _k == "top_p":
                        agent.top_p = _v
        except Exception:
            logger.debug("[v1] 采样参数注入失败（忽略）", exc_info=True)

        if not user_input:
            return _openai_error("messages 中未找到 user 消息", 400)

        # ---- 非流式：聚合全部正文后一次性返回 ----
        # 注意：agent.chat() 是同步阻塞生成器（可能触发多轮工具调用），
        # 绝不能直接在 async 路由里 for 循环——会卡死整个事件循环。
        # 必须放入线程池（asyncio.to_thread）执行。
        if not stream:
            import asyncio

            def _collect():
                content_parts = []
                finish_reason = "stop"
                usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
                try:
                    for chunk in agent.chat(user_input):
                        t = chunk.get("type")
                        if t == "content":
                            content_parts.append(str(chunk.get("response", "")))
                        elif t == "error":
                            finish_reason = "stop"
                            content_parts.append(f"\n[error] {chunk.get('data', '')}")
                        elif t == "stop":
                            u = chunk.get("usage") or {}
                            if isinstance(u, dict):
                                usage = {
                                    "prompt_tokens": u.get("input_tokens", 0) or u.get("prompt_tokens", 0) or 0,
                                    "completion_tokens": u.get("output_tokens", 0) or u.get("completion_tokens", 0) or 0,
                                    "total_tokens": u.get("total_tokens", 0) or 0,
                                }
                finally:
                    try:
                        agent.close()
                    except Exception:
                        pass
                return "".join(content_parts), finish_reason, usage

            try:
                content, finish_reason, usage = await asyncio.to_thread(_collect)
            except Exception as e:
                logger.warning("[v1] 非流式对话异常，已记录", exc_info=True)
                return _openai_error(f"对话失败: {e!s}", 500, "server_error")

            return JSONResponse(_mk_completion(
                chat_id, model_id, created, content, finish_reason, usage
            ))

        # ---- 流式：边产出边下发 SSE ----
        def gen():
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            try:
                for chunk in agent.chat(user_input):
                    t = chunk.get("type")
                    if t == "content":
                        text = str(chunk.get("response", ""))
                        if text:
                            yield _sse(_mk_chunk(chat_id, model_id, created,
                                                 {"content": text}))
                    elif t == "tool_call":
                        # 工具调用：以 delta.tool_calls 形式下发（OpenAI 标准形状）
                        yield _sse(_mk_chunk(chat_id, model_id, created, {
                            "tool_calls": [{
                                "index": 0,
                                "id": chunk.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                                "type": "function",
                                "function": {
                                    "name": chunk.get("tool", ""),
                                    "arguments": chunk.get("args", "") or "",
                                },
                            }]
                        }))
                    elif t == "tool_result":
                        # 工具结果：OpenAI 协议无对应 delta，作为 content 片段透出，
                        # 便于调用方观察执行过程（多数 SDK 会忽略，但不丢信息）。
                        res = str(chunk.get("result", ""))
                        if res:
                            yield _sse(_mk_chunk(chat_id, model_id, created,
                                                 {"content": res}))
                    elif t == "error":
                        yield _sse(_mk_chunk(chat_id, model_id, created,
                                             {"content": f"\n[error] {chunk.get('data', '')}"}))
                    elif t == "stop":
                        u = chunk.get("usage") or {}
                        if isinstance(u, dict):
                            usage = {
                                "prompt_tokens": u.get("input_tokens", 0) or u.get("prompt_tokens", 0) or 0,
                                "completion_tokens": u.get("output_tokens", 0) or u.get("completion_tokens", 0) or 0,
                                "total_tokens": u.get("total_tokens", 0) or 0,
                            }
            except Exception as e:
                logger.warning("[v1] 流式对话异常，已记录", exc_info=True)
                yield _sse(_mk_chunk(chat_id, model_id, created,
                                     {"content": f"\n[error] {e!s}"}))
            finally:
                try:
                    agent.close()
                except Exception:
                    pass

            # 结束块：finish_reason + usage，随后 [DONE]
            yield _sse(_mk_chunk(chat_id, model_id, created, {}, finish_reason="stop", usage=usage))
            yield b"data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
