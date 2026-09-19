"""
@input: functools, inspect, json, logging, os, re, threading, time, typing
@output: ToolRegistry 单例、register_tool 装饰器、危险命令黑名单、权限上下文
@position: Tools core — 统一注册/校验/护栏/审计，所有工具通过此模块注册
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import functools
import inspect
import json
import logging

logger = logging.getLogger(__name__)
import os
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any, get_type_hints

# 项目根目录（chat_client 的上一级），作为所有工具的默认工作目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
# 会话级工具权限上下文（替代全局 bool）：
# - 键：session_id（来自 ChatJob.session_id）
# - 值：是否放行高风险工具（true 表示非严格/破甲模式成员代理）
# - 无 job/session 上下文时保守返回 False，避免跨会话泄漏
# ============================================================
_session_allow_high: dict[str, bool] = {}
_allow_high_lock = threading.Lock()


def set_session_allow_high(session_id: str, enabled: bool) -> None:
    """设置指定会话的工具权限上下文（在 chat() 进入/退出时由 Agent 调用）"""
    with _allow_high_lock:
        if enabled:
            _session_allow_high[session_id] = True
        else:
            _session_allow_high.pop(session_id, None)


# 兼容旧调用名（agent.py 迁移用），新代码优先调用 set_session_allow_high
set_thread_allow_high = set_session_allow_high


def _current_session_id() -> str | None:
    """从当前任务上下文推导 session_id；无任务上下文返回 None"""
    job = getattr(_current_job_local, "job", None)
    if job is None:
        return None
    return getattr(job, "session_id", None)


def is_allow_high() -> bool:
    """查询当前执行上下文中是否允许高风险工具。

    逻辑：从 _current_job_ref 取 session_id → 查会话字典。
    - 无 job / 无 session_id：保守返回 False（拦截所有高风险命令）
    - 已注册 True：放行；未注册或显式 False：拦截
    """
    sid = _current_session_id()
    if not sid:
        return False
    with _allow_high_lock:
        return _session_allow_high.get(sid, False)


# ============================================================
# 线程安全的 job 上下文（替代原模块级 _current_job_ref）
# - web_server 的 chat 线程通过 set_current_job(job) 设置
# - 工具在线程池执行时，_guarded_execute 提交任务前捕获当前 job，
#   并在 worker 线程显式恢复，避免多会话并发时全局变量互相覆盖
# ============================================================
_current_job_local = threading.local()


def set_current_job(job):
    """在调用线程上设置当前 job（用于 emit_progress / is_allow_high 等）"""
    _current_job_local.job = job


def get_current_job():
    return getattr(_current_job_local, "job", None)


def _restore_job_ctx(job):
    """在 worker 线程中恢复 job 上下文"""
    if job is not None:
        _current_job_local.job = job


# ============================================================
# Token 用量上报：LLM 主循环收到 usage 后调用，随下一条审计记录落盘
# ============================================================
_pending_tokens = None


def report_token_usage(usage):
    """Token 用量上报入口（非阻塞，异常静默不影响主流程）"""
    global _pending_tokens
    try:
        if not usage:
            return
        inc = {"input": int(usage.get("input_tokens", 0) or 0),
               "output": int(usage.get("output_tokens", 0) or 0)}
        if _pending_tokens:
            _pending_tokens["input"] += inc["input"]
            _pending_tokens["output"] += inc["output"]
        else:
            _pending_tokens = inc
    except Exception:
        logger.warning("异常被静默吞掉，已记录", exc_info=True)


def emit_progress(event_name: str, data):
    """工具执行中实时推送进度事件到前端 SSE"""
    job = getattr(_current_job_local, "job", None)
    if job:
        job.append(event_name, data)


# ============================================================
# 危险命令黑名单（执行时强制拦截，所有工具的字符串参数都会被扫描）
# ============================================================
_DANGEROUS_CMD_PATTERNS = [
    r"rm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|$)",   # rm -rf /
    r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?[a-zA-Z]*\s+/[^/\w]",              # rm -r* /<非字母>
    r"mkfs(\.\w+)?\b",                                             # mkfs 格式化文件系统
    r"\bdd\b[^|;&]*\bof=/dev/(?:sd|vd|nvme|hd)",                    # dd 直接写块设备
    r":\(\)\s*\{.*\}\s*;\s*:",                                  # fork bomb
    r"\b(?:shutdown|halt|poweroff|reboot|init\s+[06])\b",           # 关机/重启类
    r">\s*/dev/sd[a-z]",                                            # 覆盖磁盘设备
    r"\bchmod\s+-R\s+777\s+/(?:\s|$)",                            # 递归 777 根目录
    r"\bwipefs\b",
    r"\bhistory\s+-c\b",
]


def _scan_dangerous_payload(kwargs: dict[str, Any]):
    """扫描工具参数中的高危命令特征，命中返回 (True, 描述)"""
    import re as _re
    texts = []
    for v in kwargs.values():
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, (list, tuple)):
            texts.extend(str(i) for i in v if isinstance(i, str))
    payload = "\n".join(texts)
    for pat in _DANGEROUS_CMD_PATTERNS:
        try:
            m = _re.search(pat, payload)
        except _re.error:
            continue
        if m:
            return True, pat
    return False, None


def _risk_denied_response(reason: str) -> str:
    return (
        f"\n<tool>\n<toolcall_status>error</toolcall_status>\n"
        f"<toolcall_result>\n{reason}\n</toolcall_result>\n</tool>\n"
    )


class ToolRegistry:
    # 状态持久化文件路径
    STATE_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 
        "tools_state.json"
    )

    def __init__(self):
        self._tools: dict[str, Callable] = {}
        self._schemas: list[dict[str, Any]] = []
        self._metadata: dict[str, dict[str, Any]] = {}
        self._states: dict[str, dict[str, Any]] = self._load_states()
        # 执行时风险护栏：默认开启，拦截 high 风险工具与危险命令黑名单
        # 可通过环境变量 AI_AGENT_RISK_GUARD=off 关闭严格拦截（黑名单始终生效）
        _env = os.environ.get("AI_AGENT_RISK_GUARD", "").strip().lower()
        # 默认关闭 high 拦截（自我进化需要）；安全由黑名单/白名单/参数校验保障
        # 设置 AI_AGENT_RISK_GUARD=on 可开启严格确认模式
        self.risk_guard_enabled = _env in ("1", "true", "on", "yes")
        # 统一执行超时（秒）：防止单个工具卡死整条任务；可被注册时 timeout 参数覆盖
        _t = os.environ.get("AI_AGENT_TOOL_TIMEOUT", "").strip()
        try:
            self.default_tool_timeout = max(10, int(_t)) if _t else 600
        except ValueError:
            self.default_tool_timeout = 600
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-exec")
        self.audit_log_path = os.environ.get(
            "AI_AGENT_TOOL_AUDIT",
            os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "tool_audit.jsonl"),
        )
        self._audit_lock = threading.Lock()
        self._meta_lock = threading.Lock()

    def set_risk_guard(self, enabled: bool):
        """开/关高风险工具执行时拦截（危险命令黑名单不受此开关影响）"""
        self.risk_guard_enabled = bool(enabled)

    def get_tool_risk_level(self, name: str) -> str:
        meta = self._metadata.get(name)
        return meta.get("risk_level", "low") if meta else "low"

    def _validate_args(self, name: str, kwargs: dict[str, Any]) -> str | None:
        """按生成的 Schema 前置校验参数（类型/必填），返回错误说明或 None"""
        schema = next((s for s in self._schemas if s["function"]["name"] == name), None)
        if not schema:
            return None
        params = schema["function"].get("parameters", {})
        props = params.get("properties", {}) or {}
        required = params.get("required", []) or []

        missing = [r for r in required if r not in kwargs]
        if missing:
            return f"缺少必填参数: {', '.join(missing)}。请补齐后重新调用。"

        type_map = {"string": str, "integer": int, "number": (int, float),
                    "boolean": bool, "array": (list, tuple), "object": dict}
        for k, v in kwargs.items():
            spec = props.get(k)
            if not spec:
                continue  # 未知参数交给工具自行处理
            want = spec.get("type")
            py_t = type_map.get(want)
            if not py_t:
                continue
            ok = isinstance(v, py_t) and not (want in ("integer", "number") and isinstance(v, bool))
            if not ok:
                return (
                    f"参数 {k} 类型错误：应为 {want}，实际收到 {type(v).__name__}（值: {str(v)[:80]}）。"
                    f"请修正参数类型后重试。"
                )
        return None

    def _audit(self, tool_id: str, risk: str, kwargs: dict, duration_ms: int, status: str, result_head: str):
        """全链路审计留痕（JSONL，一行一次调用）"""

        def _redact(o):
            _H = ("api_key", "apikey", "access_key", "access-key", "secret",
                  "token", "password", "passwd", "credential", "private_key")
            if isinstance(o, dict):
                return {k: ("****" if any(h in str(k).lower() for h in _H) else _redact(v))
                        for k, v in o.items()}
            if isinstance(o, list):
                return [_redact(v) for v in o]
            return o

        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool_id,
                "risk": risk,
                "duration_ms": duration_ms,
                "status": status,
                "args": json.dumps(_redact(kwargs), ensure_ascii=False, default=str)[:500],
                "result_head": result_head[:200],
            }
            # 附加 token 用量（若 LLM 主循环有上报）；无则不写该字段，保持旧格式兼容
            global _pending_tokens
            if _pending_tokens:
                entry["tokens"] = dict(_pending_tokens)
                _pending_tokens = None
            os.makedirs(os.path.dirname(self.audit_log_path), exist_ok=True)
            # 简易轮转：超过 5MB 归档为 .1
            try:
                if os.path.exists(self.audit_log_path) and os.path.getsize(self.audit_log_path) > 5 * 1024 * 1024:
                    os.replace(self.audit_log_path, self.audit_log_path + ".1")
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)
            with self._audit_lock:
                with open(self.audit_log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning("异常被静默吞掉，已记录", exc_info=True)

    def _guarded_execute(self, func: Callable, *args, **kwargs):
        """工具执行统一入口：黑名单 → 风险拦截 → 参数校验 → 超时执行 → 审计"""
        name = func.__name__
        t0 = time.time()
        meta = self._metadata.get(name, {})
        tool_label = meta.get("id", name)
        risk = meta.get("risk_level", "low")
        audit_kwargs = dict(kwargs)

        # 1. 危险命令黑名单：仅集团模式（严格模式，is_allow_high()=False）下强制拦截
        #    单 Agent / Codex-X 破甲模式不拦截，放行专业操作
        if not is_allow_high():
            danger, pattern = _scan_dangerous_payload(kwargs)
            if danger:
                logger.warning("[RiskGuard] 工具 %s 命中危险命令黑名单 (%s)，已拦截", tool_label, pattern)
                self._audit(tool_label, risk, audit_kwargs, 0, "blocked-dangerous", pattern or "")
                return _risk_denied_response(
                    "此操作命中危险命令黑名单，已被安全策略拦截，拒绝执行。"
                    "请勿尝试绕过，请告知用户该操作被禁止。"
                )

        # 2. high 风险工具：需用户确认；成员代理豁免（招聘即授权）
        if (self.risk_guard_enabled and risk == "high"
                and not is_allow_high()):
            logger.warning("[RiskGuard] 拦截高风险工具调用: %s", tool_label)
            self._audit(tool_label, risk, audit_kwargs, 0, "blocked-high-risk", "")
            return _risk_denied_response(
                f"此操作（{tool_label}）为高风险操作，执行前必须获得用户的明确确认。"
                "请停止调用该工具，向用户说明将要执行的操作内容与潜在风险，"
                "待用户明确同意后再重试；若用户已同意但工具仍被拦截，"
                "请联系管理员在面板设置或通过环境变量 AI_AGENT_RISK_GUARD=off 调整安全策略。"
            )

        # 3. medium 风险：放行并记录日志
        if risk == "medium":
            logger.info("[RiskGuard] 执行 medium 风险工具: %s", tool_label)

        # 4. 参数前置校验：不合法直接拦截并给出修正指引（不执行、不崩溃）
        verr = self._validate_args(name, kwargs)
        if verr:
            self._audit(tool_label, risk, audit_kwargs, 0, "invalid-params", verr)
            return _risk_denied_response(f"参数校验未通过：{verr}")

        # 5. 带超时执行：防止单个工具卡死整条任务
        timeout_s = meta.get("timeout") or self.default_tool_timeout
        started = time.time()
        # 捕获当前线程的 job，确保 worker 线程持有正确上下文（避免多会话并发时全局覆盖）
        ctx_job = getattr(_current_job_local, "job", None)
        try:
            def _wrapped():
                _restore_job_ctx(ctx_job)
                return func(*args, **kwargs)
            fut = self._executor.submit(_wrapped)
            try:
                result = fut.result(timeout=timeout_s)
            except _FutureTimeout:
                fut.cancel()
                msg = f"工具执行超时（>{timeout_s}s），已终止等待。可尝试简化请求或拆分任务后重试。"
                logger.warning("[ToolTimeout] %s 超过 %ss", tool_label, timeout_s)
                self._audit(tool_label, risk, audit_kwargs, int((time.time() - started) * 1000), "timeout", msg)
                return _risk_denied_response(msg)
        except Exception as e:
            dur = int((time.time() - t0) * 1000)
            self._audit(tool_label, risk, audit_kwargs, dur, "error", str(e))
            raise

        dur = int((time.time() - t0) * 1000)
        self._audit(tool_label, risk, audit_kwargs, dur, "ok", str(result)[:200])
        return result

    def _load_states(self) -> dict[str, Any]:
        """从文件加载工具状态"""
        if os.path.exists(self.STATE_FILE):
            try:
                with open(self.STATE_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("工具状态文件损坏（%s），已回退空状态: %s", self.STATE_FILE, e)
            except OSError as e:
                logger.warning("读取工具状态文件失败（%s）: %s", self.STATE_FILE, e)
        return {}

    def _save_states(self):
        """将工具状态保存到文件（原子写：tmp + os.replace）"""
        tmp = self.STATE_FILE + ".tmp"
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._states, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.STATE_FILE)
        except OSError as e:
            logger.warning("写入工具状态文件失败（%s）: %s", self.STATE_FILE, e)
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        except (TypeError, ValueError) as e:
            # 状态对象含不可序列化字段（如 set/datetime），降级为尽力序列化
            logger.warning("工具状态含不可序列化字段，降级写入: %s", e)
            self._states = json.loads(json.dumps(self._states, ensure_ascii=False, default=str))
    
    def tool_exists(self, name: str) -> bool:
        """检查工具是否存在"""
        return name in self._tools
    
    def is_tool_enabled(self, name: str, enabled_ids: list[str]) -> bool:
        """
        检查工具是否在允许列表中。

        Args:
            name: 工具名称（函数名）
            enabled_ids: 允许使用的工具ID列表

        Returns:
            bool: 工具是否被允许使用
        """
        meta = self._metadata.get(name)
        if not meta:
            return False
        return meta["id"] in enabled_ids
    
    def get_tool_id(self, name: str) -> str | None:
        """获取工具的ID"""
        meta = self._metadata.get(name)
        if meta:
            return meta["id"]
        return None

    def register_tool(self, tool_id: str | Callable | type | None = None, **kwargs):
        """
        注册工具的装饰器。
        支持:
        @register_tool
        @register_tool("my_tool_id")
        @register_tool(id="my_tool_id")
        @register_tool(id="my_tool_id", category="system")
        @register_tool(id="my_tool_id", category="system", name_cn="系统服务")
        
        以及装饰类:
        @register_tool
        class MyTool:
            def execute(self, ...): ...
        """
        category = kwargs.get("category", "default")
        name_cn = kwargs.get("name_cn", "")
        risk_level = kwargs.get("risk_level", "low")
        tool_timeout = kwargs.get("timeout")  # 秒；None 用全局默认

        # 处理 id="xxx" 的关键字参数情况
        if tool_id is None and "id" in kwargs:
            tool_id = kwargs["id"]
        
        # 如果是类 (作为装饰器无参数直接使用 @register_tool)
        if inspect.isclass(tool_id):
            return self._register_class(tool_id, None, category, name_cn, risk_level, tool_timeout)

        # 如果是函数 (作为普通装饰器使用 @register_tool (无参数))
        if callable(tool_id):
            func = tool_id
            return self._register_func(func, None, category, name_cn, risk_level, tool_timeout)
            
        # 如果带有参数 @register_tool(...)
        def decorator(obj):
            if inspect.isclass(obj):
                return self._register_class(obj, tool_id, category, name_cn, risk_level, tool_timeout)
            else:
                return self._register_func(obj, tool_id, category, name_cn, risk_level, tool_timeout)
        return decorator

    def _register_class(self, clazz: type, tool_id: str | None, category: str, name_cn: str, risk_level: str, timeout: int | None = None):
        # 实例化类
        try:
            instance = clazz()
        except Exception as e:
            raise ValueError(f"Failed to instantiate tool class {clazz.__name__}: {e}")

        # 查找入口方法
        func = None
        if hasattr(instance, 'execute') and callable(instance.execute):
            func = instance.execute
        elif callable(instance):
            func = instance.__call__
        else:
            raise ValueError(f"Class {clazz.__name__} must implement 'execute' method or be callable.")

        # 确定工具ID
        if not tool_id:
            tool_id = clazz.__name__
            
        # 创建包装器以保持正确的名称和文档
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
            
        wrapper.__name__ = tool_id
        
        # 如果方法没有文档字符串，尝试使用类的文档字符串
        if not wrapper.__doc__:
            wrapper.__doc__ = inspect.getdoc(clazz)
            
        # 注册包装后的函数
        # 注意：这里我们返回 clazz，以便类定义保持不变，
        # 但我们在内部注册了 wrapper 函数作为工具执行体。
        self._register_func(wrapper, tool_id, category, name_cn, risk_level, timeout)
        return clazz

    def _register_func(self, func: Callable, tool_id: str | None, category: str, name_cn: str, risk_level: str, timeout: int | None = None):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return self._guarded_execute(func, *args, **kwargs)

        name = func.__name__
        # 如果没有提供ID，使用函数名作为ID
        final_id = tool_id if tool_id else name
        
        self._tools[name] = wrapper
        schema = self._generate_schema(func)
        self._schemas.append(schema)
        
        # 存储元数据
        state = self._states.get(final_id, {"show": True})
        self._metadata[name] = {
            "id": final_id,
            "name": name,
            "name_cn": name_cn,
            "category": category,
            "risk_level": risk_level,
            "description": schema["function"]["description"],
            "show": state.get("show", True),
            "timeout": int(timeout) if timeout else None,
        }
        
        return wrapper

    def set_tool_show_status(self, tool_id: str | None = None, show: bool = True, category: str | None = None) -> bool:
        """设置工具或分类下工具的显示状态"""
        found = False
        
        # 如果提供了 category，按分类批量设置
        if category:
            for name, meta in self._metadata.items():
                if meta.get("category") == category:
                    meta["show"] = show
                    self._states[meta["id"]] = {"show": show}
                    found = True
        
        # 如果提供了 tool_id，按 ID 设置
        elif tool_id:
            for name, meta in self._metadata.items():
                if meta["id"] == tool_id:
                    meta["show"] = show
                    self._states[tool_id] = {"show": show}
                    found = True
        
        if found:
            self._save_states()
            return True
        return False

    def get_openai_tools(self, enabled_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """
        返回 OpenAI 格式的工具定义。
        
        Args:
            enabled_ids: 允许使用的工具ID列表。如果不传，则返回所有(兼容旧行为)。
        """
        if enabled_ids is None:
            return self._schemas
        filtered_schemas = []
        for schema in self._schemas:
            name = schema["function"]["name"]
            meta = self._metadata.get(name)
            # 只有 ID 在启用列表中才返回
            if meta and meta["id"] in enabled_ids:
                filtered_schemas.append(schema)
        return filtered_schemas

    def get_all_tools_info(self) -> list[dict[str, Any]]:
        """获取所有工具的详细信息列表 (用于前端展示)"""
        infos = []
        for name, meta in self._metadata.items():
            # 创建副本以免修改原始元数据
            info = meta.copy()
            # 如果存在 name_cn，替换 name 字段
            if info.get("name_cn"):
                info["name"] = info["name_cn"]
            infos.append(info)
        return infos

    def get_tool_func(self, name: str) -> Callable | None:
        return self._tools.get(name)

    def _generate_schema(self, func: Callable) -> dict[str, Any]:
        """根据文档字符串和类型提示生成 OpenAI 函数 Schema"""
        sig = inspect.signature(func)
        doc = inspect.getdoc(func) or "No description provided."
        
        parameters = {
            "type": "object",
            "properties": {},
            "required": []
        }
        
        type_hints = get_type_hints(func)
        
        # 解析 docstring 的 Args 段，提取每个参数的描述（Google 风格，支持多行续行）
        arg_docs = {}
        if doc and "Args:" in doc:
            try:
                args_section = doc.split("Args:", 1)[1]
                stop_at = None
                for kw in ("Returns:", "Raises:", "Example", "Usage", "Note"):
                    i = args_section.find(kw)
                    if i != -1:
                        stop_at = i if stop_at is None else min(stop_at, i)
                if stop_at is not None:
                    args_section = args_section[:stop_at]
                cur_name, cur_desc = None, []
                for ln in args_section.splitlines():
                    m = re.match(r"^\s{2,}(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", ln)
                    if m:
                        if cur_name:
                            arg_docs[cur_name] = " ".join(cur_desc).strip()
                        cur_name, cur_desc = m.group(1), [m.group(2).strip()]
                    elif cur_name and ln.strip():
                        cur_desc.append(ln.strip())
                if cur_name:
                    arg_docs[cur_name] = " ".join(cur_desc).strip()
            except (re.error, AttributeError):
                # 正则解析失败时降级为无参数描述，不影响工具注册
                arg_docs = {}
            except Exception:
                logger.warning("解析参数文档失败，已降级为无描述", exc_info=True)
                arg_docs = {}

        for name, param in sig.parameters.items():
            if name == "self" or name == "cls":
                continue

            # Skip *args and **kwargs
            if param.kind == inspect.Parameter.VAR_POSITIONAL or param.kind == inspect.Parameter.VAR_KEYWORD:
                continue

            param_type = type_hints.get(name, str)
            json_type = self._python_type_to_json_type(param_type)

            param_info = {"type": json_type}
            desc = arg_docs.get(name)
            if desc:
                param_info["description"] = desc
            if param.default != inspect.Parameter.empty:
                d = param.default
                if isinstance(d, (str, int, float, bool)) or d is None:
                    param_info["default"] = d
                elif isinstance(d, (list, tuple)):
                    try:
                        param_info["default"] = list(d)
                    except Exception:
                        logger.warning("异常被静默吞掉，已记录", exc_info=True)

            parameters["properties"][name] = param_info

            if param.default == inspect.Parameter.empty:
                parameters["required"].append(name)
                
        return {
            "type": "function",
            "function": {
                "name": func.__name__,
                "strict": True,
                "description": doc,
                "parameters": parameters
            }
        }

    def _python_type_to_json_type(self, py_type) -> str:
        if py_type == int:
            return "integer"
        elif py_type == float:
            return "number"
        elif py_type == bool:
            return "boolean"
        elif py_type == list or getattr(py_type, "__origin__", None) == list:
            return "array"
        elif py_type == dict or getattr(py_type, "__origin__", None) == dict:
            return "object"
        else:
            return "string"

# 全局注册实例
registry = ToolRegistry()

# 装饰器别名
def register_tool(tool_id=None, **kwargs):
    return registry.register_tool(tool_id, **kwargs)

# 导入所有工具以确保它们被注册
from . import (  # noqa: F401  (导入以触发各工具的 @register_tool 注册副作用)
    agent_tools,
    edit,
    mcp_tool,
    mem0,
    scheduler,
    skill,
    summary,
    sys_ops,
    task,
    terminal,
    todo,
    webfetch,
)
