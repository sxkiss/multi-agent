"""
opencode 模式桥梁：管理 opencode serve 子进程生命周期，
转发用户消息，将 opencode SSE 事件流转换为 ChatJob SSE 事件格式。

与后端原生模式（agent.py）并存，共享同一套 ChatJob + SSE 基础设施。
设计约束：
- run_opencode_chat() 在后台线程中被 web_server 调用（与 agent.chat() 同级）
- 线程内创建独立 asyncio 事件循环，不依赖主线程 loop
- opencode 子进程按会话创建，会话结束自动销毁
"""

import asyncio
import json
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from chat_client.opencode_config import _PROJECT_ROOT

logger = logging.getLogger(__name__)

# ── 动态端口分配 ──────────────────────────────────────────────────────────

def _find_free_port() -> int:
    """从 49152-65535 随机探测可用端口"""
    for _ in range(30):
        port = random.randint(49152, 65535)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("无法分配可用端口")


# ── opencode 子进程管理 ──────────────────────────────────────────────────

@dataclass
class OpenCodeProcess:
    """管理一个 opencode serve 子进程"""
    port: int
    workspace: str
    config_dir: str
    _proc: subprocess.Popen | None = None
    _ready: bool = False
    _start_time: float = 0.0

    def start(self) -> None:
        env = os.environ.copy()
        # HOME 保持真实用户 → 直接读/写 ~/.config/opencode/opencode.json
        # OPENCODE_CONFIG 与 XDG_CONFIG_HOME 含义重叠，只设一个避免路径冲突
        env["OPENCODE_CONFIG"] = os.path.expanduser("~/.config/opencode/opencode.json")
        # systemd 最小 PATH 不含用户 bin，补充 opencode 所在目录
        node_bin = os.path.dirname(self._opencode_path())
        if os.path.isdir(node_bin) and node_bin not in env.get("PATH", ""):
            env["PATH"] = node_bin + os.pathsep + env.get("PATH", "")
        cmd = [self._opencode_path(), "serve", "--port", str(self.port), "--hostname", "127.0.0.1"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.workspace, env=env, start_new_session=True,
        )
        self._start_time = time.time()
        logger.info("opencode serve 已启动 pid=%s port=%d workspace=%s",
                     self._proc.pid, self.port, self.workspace)

    @staticmethod
    def _opencode_path() -> str:
        """定位 opencode 可执行文件（优先绝对路径）"""
        candidates = [
            os.path.expanduser("~/.local/nodejs/node-latest/bin/opencode"),
            os.path.expanduser("~/.local/bin/opencode"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        return "opencode"

    def wait_ready(self, timeout: float = 30.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = httpx.get(f"http://127.0.0.1:{self.port}/project", timeout=2)
                if r.status_code == 200:
                    self._ready = True
                    logger.info("opencode serve 就绪 (%.1fs)", time.time() - self._start_time)
                    return True
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            time.sleep(0.5)
        logger.error("opencode serve 启动超时 (%ds)", int(timeout))
        return False

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def kill(self) -> None:
        if self._proc:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                # 等最多 8 秒让 opencode 完成异步 DB 刷盘（WAL checkpoint）
                deadline = time.time() + 8
                while self._proc.poll() is None and time.time() < deadline:
                    time.sleep(0.2)
                if self._proc.poll() is None:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self._proc = None
            self._ready = False

    def close(self) -> None:
        """ChatJobManager.stop() 通过 agent.close() 终止任务"""
        self.kill()


# ── opencode SSE -> ChatJob 事件转换 ──────────────────────────────────

class OpenCodeEventTransformer:
    """将 opencode SSE 事件转换为 ChatJob SSE 事件"""

    def __init__(self):
        self._tool_calls: dict[str, dict] = {}
        self._prev_texts: dict[str, str] = {}  # pid -> 上一次 part.text，用于 diff
        self._msg_roles: dict[str, str] = {}   # messageID -> role (user/assistant)

    def transform(self, event: dict) -> list[tuple[str, Any]]:
        etype = event.get("type", event.get("_event", ""))
        handler = {
            "message.part.updated": self._on_part,
            "message.updated": self._on_message_updated,
            "permission.updated": self._on_permission,
            "session.idle": self._on_idle,
            "session.error": self._on_error,
        }.get(etype)
        if handler:
            return handler(event)
        return []

    def _on_part(self, event: dict) -> list[tuple[str, Any]]:
        props = event.get("properties", {})
        part = props.get("part", {})
        ptype = part.get("type", "")
        pid = part.get("id", "")
        mid = part.get("messageID", "")
        results = []

        # 用户消息回显：跳过（只回放 assistant 的内容）
        if mid and self._msg_roles.get(mid) == "user":
            return []

        if ptype == "text":
            # opencode 的 text part 是累积的完整文本（无 delta 字段），
            # 与上一帧 diff 得到增量，避免前端收到重复内容
            full = part.get("text", "")
            prev = self._prev_texts.get(pid, "")
            if full != prev:
                self._prev_texts[pid] = full
                content = full[len(prev):] if full.startswith(prev) else full
                if content:
                    # 事件名用 message + 纯字符串，与 native 模式 / 前端 SSE 处理器对齐
                    results.append(("message", content))
        elif ptype == "reasoning":
            # 兼容两种格式：纯文本 或 [{"type": "thinking", "text": "..."}]
            raw = part.get("text", "")
            if isinstance(raw, list):
                # opencode 开启 reasoning: True 时，文本可能是数组格式
                texts = []
                for item in raw:
                    if isinstance(item, dict):
                        texts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        texts.append(item)
                full = "".join(texts)
            else:
                full = str(raw)
            prev = self._prev_texts.get(pid, "")
            if full != prev:
                self._prev_texts[pid] = full
                content = full[len(prev):] if full.startswith(prev) else full
                if content:
                    # 事件名用 message_think + 纯字符串，与前端 SSE 处理器保持一致
                    results.append(("message_think", content))
        elif ptype == "tool":
            state = part.get("state", {})
            status = state.get("status", "")
            tool_name = part.get("tool", "")
            call_id = part.get("callID") or part.get("toolCallID") or pid
            if status == "running":
                args_str = json.dumps(state.get("input", {}), ensure_ascii=False)
                results.append(("tool_call", {"tool": tool_name, "args": args_str, "id": call_id}))
                self._tool_calls[call_id] = {"tool": tool_name}
            elif status in ("completed", "error"):
                output = state.get("output", "")
                results.append(("tool_result", {
                    "tool": self._tool_calls.pop(call_id, {}).get("tool", tool_name),
                    "result": str(output)[:50000], "id": call_id,
                }))
            else:
                # 兜底：确保工具调用有结果（虽然通常不需要，但防漏）
                results.append(("tool_result", {
                    "tool": self._tool_calls.pop(call_id, {}).get("tool", tool_name),
                    "result": "", "id": call_id,
                }))
        elif ptype == "subtask":
            results.append(("tool_call", {
                "tool": "subtask", "args": json.dumps({"prompt": part.get("prompt", "")}, ensure_ascii=False),
                "id": pid,
            }))
        return results

    def _on_message_updated(self, event: dict) -> list[tuple[str, Any]]:
        info = event.get("properties", {}).get("info", {})
        mid = info.get("id", "")
        if mid:
            self._msg_roles[mid] = info.get("role", "")
        tokens = info.get("tokens", {})
        if tokens:
            return [("usage", {"usage": {
                "input_tokens": tokens.get("input", 0),
                "output_tokens": tokens.get("output", 0),
            }})]
        return []

    def _on_permission(self, event: dict) -> list[tuple[str, Any]]:
        p = event.get("properties", {})
        return [("permission_ask", {"permissionID": p.get("permissionID", ""), "tool": p.get("tool", "")})]

    def _on_idle(self, _event) -> list[tuple[str, Any]]:
        return [("stop", {"usage": {}})]

    def _on_error(self, event: dict) -> list[tuple[str, Any]]:
        err = event.get("properties", {}).get("error", {})
        return [("error", {"data": str(err.get("message") or err.get("data") or "opencode 错误")[:500]})]


# ── 异步核心：订阅 SSE 事件流 ──────────────────────────────────────────

async def _subscribe_events(base_url: str, transformer: OpenCodeEventTransformer, on_event, timeout: float = 300):
    """异步订阅 opencode SSE 事件流，转换后回调 on_event(event, data)"""
    async with httpx.AsyncClient() as client:
        async with client.stream("GET", f"{base_url}/event", timeout=timeout) as resp:
            event_type = None
            async for raw_line in resp.aiter_lines():
                line = raw_line.rstrip("\r\n")
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str:
                        try:
                            obj = json.loads(data_str)
                            obj["type"] = obj.get("type", event_type)
                            for ev, ev_data in transformer.transform(obj):
                                on_event(ev, ev_data)
                                if ev == "stop":
                                    return
                        except json.JSONDecodeError:
                            pass
                elif line == "":
                    event_type = None


# ── 同步核心：创建会话并发送消息 ────────────────────────────────────────

async def _create_and_send(base_url: str, user_input: str, model: str, oc_sid: str = "") -> str:
    """复用或新建 opencode 会话并异步发送消息，返回 session_id"""
    # model 形式为 "provider/model_id"（如 "gateway/auto"）
    provider_id, model_id = model.split("/", 1) if "/" in model else ("gateway", model)
    async with httpx.AsyncClient() as client:
        if oc_sid:
            # 历史会话续聊：校验会话存在（已删除则回退新建）
            r = await client.get(f"{base_url}/session/{oc_sid}")
            if r.status_code != 200:
                logger.warning("复用 opencode 会话失败(HTTP %d)，回退新建: %s", r.status_code, oc_sid)
                oc_sid = ""
        if not oc_sid:
            r = await client.post(f"{base_url}/session", json={})
            r.raise_for_status()
            oc_sid = r.json()["id"]
        await client.post(
            f"{base_url}/session/{oc_sid}/prompt_async",
            json={
                "parts": [{"type": "text", "text": user_input}],
                "model": {"providerID": provider_id, "modelID": model_id},
                "agent": "general",
            },
            headers={"Content-Type": "application/json"},
        )
        return oc_sid


# ── 前端会话 ↔ opencode 会话映射（meta.json） ───────────────────────────

def _meta_path(session_id: str) -> str:
    """前端会话 meta.json 路径（sessions/<id>/meta.json）"""
    return os.path.join(_PROJECT_ROOT, "sessions", session_id, "meta.json")


def _load_meta(session_id: str) -> dict:
    try:
        with open(_meta_path(session_id), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_meta(session_id: str, meta: dict) -> None:
    try:
        p = _meta_path(session_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    except Exception:
        logger.warning("meta.json 写入失败 session=%s", session_id, exc_info=True)


def _resolve_reuse_sid(session_id: str) -> str:
    """解析可复用的 opencode 会话 ID：
    - session_id 本身以 ses_ 开头 → 直接复用
    - 否则查 meta.json 里上次写入的 opencode_sid 映射
    返回 "" 表示需要新建。
    """
    if session_id.startswith("ses_"):
        return session_id
    mapped = _load_meta(session_id).get("opencode_sid", "")
    if mapped and str(mapped).startswith("ses_"):
        return str(mapped)
    return ""


# ── 同步入口：在后台线程中运行 ──────────────────────────────────────────

def run_opencode_chat(
    session_id: str,
    user_input: str,
    job: Any,
    model: str = "auto",
    system_prompt: str | None = None,
    workspace: str = "",
    reasoning_effort: str = "max",
    base_url: str = "",
    api_key: str = "",
) -> None:
    """
    opencode 模式的对话入口（在后台线程中运行）。
    与 agent.chat() 同级，结果通过 job.append() 推到 SSE 流。
    会话数据写入全局 ~/.local/share/opencode/opencode.db。
    配置文件直接读写用户全局 ~/.config/opencode/opencode.json，不隔离。
    """
    # 历史会话续聊：ses_ 开头即为 opencode 全局会话 ID，复用之（不新建）
    # 或从 meta.json 读取上次映射的 opencode_sid（前端 Date.now() 会话复用同一 opencode 会话）
    reuse_sid = _resolve_reuse_sid(session_id)
    ws = workspace or _PROJECT_ROOT
    if reuse_sid and not workspace:
        # 对齐历史会话的工作目录
        try:
            import sqlite3
            conn = sqlite3.connect(_get_opencode_db_path(), timeout=5)
            row = conn.execute("SELECT directory FROM session WHERE id = ?", (reuse_sid,)).fetchone()
            conn.close()
            if row and row[0]:
                ws = row[0]
        except Exception:
            pass
    logger.info("opencode 模式启动 model=%s workspace=%s base=%s", model, ws, base_url)

    # 自定义 API 配置：写入全局 opencode.json（保留用户其他配置）
    if base_url or api_key:
        from chat_client.opencode_config import generate_opencode_config
        try:
            generate_opencode_config(session_id, "", workspace=ws, system_prompt=system_prompt,
                                     reasoning_effort=reasoning_effort,
                                     custom_base_url=base_url, custom_api_key=api_key)
        except Exception as e:
            logger.warning("opencode 配置写入失败，已记录: %s", e)

    def _on_ev(ev, data):
        job.append(ev, data)

    port = _find_free_port()
    proc = OpenCodeProcess(port=port, workspace=ws, config_dir="")
    if hasattr(job, "attach_agent"):
        job.attach_agent(proc)
    proc.start()

    try:
        if not proc.wait_ready(timeout=30):
            job.append("error", {"data": "opencode serve 启动超时"})
            job.append("message_end", None)
            job.finish("error")
            return

        base_url = f"http://127.0.0.1:{port}"
        transformer = OpenCodeEventTransformer()

        async def _run():
            oc_sid = await _create_and_send(base_url, user_input, model, oc_sid=reuse_sid)
            if reuse_sid:
                logger.info("opencode 会话已复用: %s", oc_sid)
            else:
                logger.info("opencode 会话已创建: %s", oc_sid)
            # 回写 opencode 会话 ID 到 meta.json，后续消息复用同一会话（而非每次新建）
            if oc_sid and not reuse_sid:
                meta = _load_meta(session_id)
                meta["opencode_sid"] = oc_sid
                _save_meta(session_id, meta)
            # P2-36: _subscribe_events 300s 超时后自动重连，避免长回复被截断为"done"
            # 注意：httpx timeout 抛 httpx.TimeoutException，不是 asyncio.TimeoutError
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    await _subscribe_events(base_url, transformer, _on_ev, timeout=300)
                    break  # 正常结束（收到 stop 事件）
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    if attempt < max_retries:
                        logger.warning("[OpenCodeBridge] SSE 订阅超时（%ds），第 %d 次重连", 300, attempt + 1)
                        await asyncio.sleep(2)
                        continue
                    else:
                        logger.error("[OpenCodeBridge] SSE 订阅超时，已达最大重试次数")
                        job.append("error", {"data": "opencode 事件流超时，请检查服务状态"})
                        job.append("message_end", None)
                        job.finish("error")
                        return
            # 优雅退出：abort 触发 DB flush，再等 5 秒确保 part 表写完
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    await c.get(f"{base_url}/session/{oc_sid}/abort")
            except Exception:
                pass
            await asyncio.sleep(5)

        asyncio.run(_run())
        job.append("message_end", None)
        job.finish("done")

    except Exception as e:
        logger.exception("opencode 模式对话异常")
        job.append("error", {"data": f"opencode 异常: {str(e)[:500]}"})
        job.append("message_end", None)
        job.finish("error")
    finally:
        proc.kill()
        # 全局配置文件不删除，auth.json 也不会被移除


# ── 全局 opencode.db 查询（读取历史会话）─────────────────────────────────

def _get_opencode_db_path() -> str:
    """全局 opencode 数据库路径（~/.local/share/opencode/opencode.db）"""
    return os.path.expanduser("~/.local/share/opencode/opencode.db")


def query_opencode_sessions(workspace_filter: str = "") -> list[dict]:
    """从全局 opencode.db 查询会话列表（按 directory 过滤）。

    注意：opencode 某些版本会出现 `session` 元数据表被清空但 `message`/`part`
    表仍有数据的情况。这里优先从 `session` 表取，缺失时回退到用 `message`
    表的 session_id 反查会话，保证历史不丢。
    """
    import sqlite3
    db_path = _get_opencode_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row

        # 1) 优先尝试 session 表
        session_rows = []
        try:
            if workspace_filter:
                session_rows = conn.execute(
                    "SELECT id, directory, title, time_updated, tokens_input, tokens_output "
                    "FROM session WHERE directory = ? ORDER BY time_updated DESC LIMIT 200",
                    (workspace_filter,),
                ).fetchall()
            else:
                session_rows = conn.execute(
                    "SELECT id, directory, title, time_updated, tokens_input, tokens_output "
                    "FROM session ORDER BY time_updated DESC LIMIT 200",
                ).fetchall()
        except Exception:
            session_rows = []

        sessions = [dict(r) for r in session_rows]

        # 2) session 表为空（被清）时，用 message 表反查 session_id + 最近时间
        if not sessions:
            # 从 message 表聚合出所有 session_id
            wc = "WHERE m.session_id IN (SELECT session_id FROM message GROUP BY session_id)"
            if workspace_filter:
                # 没有 directory 情况下只能按全部 session 反查（opencode 多 session 共用 cwd）
                pass
            agg = conn.execute(
                "SELECT m.session_id AS sid, "
                "MAX(CAST(m.time_created AS INTEGER)) AS last_t, "
                "MIN(CAST(m.time_created AS INTEGER)) AS first_t "
                "FROM message m "
                "GROUP BY m.session_id ORDER BY last_t DESC LIMIT 200"
            ).fetchall()
            for r in agg:
                sid = r["sid"]
                last_t = int(r["last_t"] or 0)
                # 取该 session 首条 user 消息作标题 + 从 message.data.path.cwd 提取 workspace
                first_user = ""
                fb_cwd = ""
                try:
                    um = conn.execute(
                        "SELECT id, data FROM message WHERE session_id = ? AND data LIKE '%\"role\":\"user\"%' "
                        "ORDER BY CAST(time_created AS INTEGER) ASC LIMIT 1",
                        (sid,),
                    ).fetchone()
                    if um:
                        d = json.loads(um["data"])
                        pth = d.get("path") or {}
                        if isinstance(pth, dict) and pth.get("cwd"):
                            fb_cwd = str(pth["cwd"])
                        # 文本存在 part 表（message.data 无 content 字段）
                        try:
                            tp = conn.execute(
                                "SELECT data FROM part WHERE message_id = ? AND json_extract(data,'$.type')='text' "
                                "ORDER BY CAST(time_created AS INTEGER) ASC LIMIT 1",
                                (um["id"],),
                            ).fetchone()
                            if tp:
                                first_user = str(json.loads(tp["data"]).get("text") or "")
                        except Exception:
                            pass
                        if not first_user:
                            content = d.get("content", "")
                            if isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        first_user = c.get("text", "")
                                        break
                            elif isinstance(content, str):
                                first_user = content
                except Exception:
                    first_user = ""
                # user 消息没有 path 时，用该会话最新一条消息兜底
                if not fb_cwd:
                    try:
                        lm = conn.execute(
                            "SELECT data FROM message WHERE session_id = ? AND data LIKE '%\"cwd\"%' "
                            "ORDER BY CAST(time_created AS INTEGER) DESC LIMIT 1",
                            (sid,),
                        ).fetchone()
                        if lm:
                            pth = (json.loads(lm["data"]) or {}).get("path") or {}
                            if isinstance(pth, dict) and pth.get("cwd"):
                                fb_cwd = str(pth["cwd"])
                    except Exception:
                        pass
                if workspace_filter and fb_cwd != workspace_filter:
                    continue
                sessions.append({
                    "id": sid,
                    "directory": fb_cwd,
                    "title": (first_user[:50].strip() or "opencode 会话"),
                    "time_updated": last_t,
                    "tokens_input": 0,
                    "tokens_output": 0,
                })

        conn.close()
        return sessions
    except Exception:
        logger.warning("opencode.db 查询失败", exc_info=True)
        return []


def query_opencode_history(session_id: str) -> list[dict]:
    """
    从 opencode.db 读取完整会话历史（user text / reasoning / tool / assistant text 全在 part 表）。
    输出历史兼容格式（前端 parseChatHistory 可渲染）：
    - user: content 数组
    - assistant: content 文本 + tool_calls
    - tool: tool_call_id + content（工具结果，前端回填到 tool_call 块）
    """
    import sqlite3
    db_path = _get_opencode_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row

        # session 表可能被清空（会话从 message 表反查而来），查不到也继续用 message/part 表
        sess = conn.execute(
            "SELECT slug AS directory, title FROM session WHERE id = ?", (session_id,)
        ).fetchone()

        # 全部消息按时间排序（一条 user 对应一条 assistant）
        msgs = conn.execute(
            "SELECT id, data, time_created FROM message WHERE session_id = ? "
            "ORDER BY time_created ASC",
            (session_id,),
        ).fetchall()
        if not msgs:
            conn.close()
            return []

        # 全部 parts 按 (message_id, time_created) 归组
        parts_by_msg: dict[str, list] = {}
        for row in conn.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created ASC, id ASC",
            (session_id,),
        ).fetchall():
            parts_by_msg.setdefault(row["message_id"], []).append(json.loads(row["data"]))
        conn.close()

        history = []
        for msg in msgs:
            info = json.loads(msg["data"])
            role = info.get("role", "")
            parts = parts_by_msg.get(msg["id"], [])
            ts = info.get("time", {}).get("created", msg["time_created"]) / 1000 or 0

            if role == "user":
                text = ""
                for p in parts:
                    if p.get("type") == "text":
                        text += p.get("text", "")
                if text.strip():
                    history.append({
                        "role": "user",
                        "content": [{"type": "text", "text": text}],
                        "timestamp": ts,
                    })

            elif role == "assistant":
                # 组装 assistant：text parts + tool parts + reasoning parts（思维链）
                text = ""
                reasoning = ""
                tool_calls = []
                tool_results = {}  # callID -> output
                for p in parts:
                    ptype = p.get("type", "")
                    if ptype == "text":
                        text += p.get("text", "")
                    elif ptype == "reasoning":
                        # 兼容两种格式：纯文本 或 [{"type": "thinking", "text": "..."}]
                        raw = p.get("text", "")
                        if isinstance(raw, list):
                            for item in raw:
                                if isinstance(item, dict):
                                    reasoning += item.get("text", "")
                                elif isinstance(item, str):
                                    reasoning += item
                        else:
                            reasoning += str(raw)
                    elif ptype == "tool":
                        state = p.get("state", {})
                        tool_calls.append({
                            "id": p.get("callID", ""),
                            "function": {"name": p.get("tool", ""), "arguments": json.dumps(state.get("input", {}), ensure_ascii=False)},
                        })
                        tool_results[p.get("callID", "")] = state.get("output", "")
                if not text.strip() and not tool_calls:
                    continue
                msg_row = {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": tool_calls,
                    "timestamp": ts,
                }
                if reasoning:
                    msg_row["reasoning_content"] = reasoning
                history.append(msg_row)
                # tool 结果消息（前端按 tool_call_id 回填）
                for tc in tool_calls:
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_results.get(tc["id"], ""),
                        "timestamp": ts + 0.001,
                    })
        return history
    except Exception:
        logger.warning("opencode 历史查询失败", exc_info=True)
        return []
