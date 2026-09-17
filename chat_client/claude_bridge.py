"""
claude 模式桥梁：管理 claude -p 子进程生命周期，
通过 stdin/stdout stream-json 双向协议通信，
将 Claude Code 的 JSON-lines 输出转换为 ChatJob SSE 事件格式。

与 opencode 模式并存，共享同一套 ChatJob + SSE 基础设施。
设计约束：
- run_claude_chat() 在后台线程中被 web_server 调用（与 agent.chat() / run_opencode_chat() 同级）
- 每次会话启动独立子进程，进程退出即结束
- stdin 写入 JSON-lines，stdout 逐行读取 JSON-lines，无需 HTTP
"""

import json
import logging
import os
import select
import signal
import subprocess
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Claude CLI 路径定位 ──────────────────────────────────────────────────

def _claude_path() -> str:
    """定位 claude 可执行文件（优先绝对路径）"""
    candidates = [
        os.path.expanduser("~/.local/nodejs/node-latest/bin/claude"),
        os.path.expanduser("~/.local/bin/claude"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "claude"


# ── Claude CLI 子进程管理 ──────────────────────────────────────────────

class ClaudeProcess:
    """管理一个 claude -p 子进程（stdin/stdout 流式 JSON 协议），支持 --resume 续聊"""

    def __init__(self, workspace: str, effort: str = "max",
                 system_prompt: str = "", tools: str = "",
                 allowed_tools: str = "", disallowed_tools: str = "",
                 reuse_sid: str = "",
                 api_key: str = "", base_url: str = ""):
        self.workspace = workspace
        self.effort = effort
        self.system_prompt = system_prompt
        self.tools = tools
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.reuse_sid = reuse_sid
        self.api_key = api_key
        self.base_url = base_url
        self._proc: subprocess.Popen | None = None
        self._start_time: float = 0.0

    def start(self) -> None:
        env = os.environ.copy()
        # Claude Code 从环境变量读取认证信息
        # ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL
        # 优先使用自定义配置，否则保持当前环境中的值
        if self.api_key:
            env["ANTHROPIC_API_KEY"] = self.api_key
        if self.base_url:
            env["ANTHROPIC_BASE_URL"] = self.base_url

        cmd = [
            _claude_path(),
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            # stream-json 输出需要 --verbose 才能启用（否则 claude 直接报错退出）
            "--verbose",
        ]




        if self.system_prompt:
            cmd += ["--append-system-prompt", self.system_prompt]
        if self.tools:
            cmd += ["--tools", self.tools]
        if self.allowed_tools:
            cmd += ["--allowedTools", self.allowed_tools]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", self.disallowed_tools]
        # 续聊：复用历史会话（Claude CLI 按 session_id 恢复上下文）
        if self.reuse_sid:
            cmd += ["--resume", self.reuse_sid]

        logger.info("claude 启动: %s", " ".join(cmd[:6]) + " ...")

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.workspace,
            env=env,
            start_new_session=True,
        )
        self._start_time = time.time()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def write_message(self, data: dict) -> bool:
        """向 stdin 写入一条 JSON-line，失败返回 False"""
        # P2-38: 写入前确认进程就绪且 stdin 未关闭，避免 BlockingIOError
        if not self.is_alive():
            logger.warning("[ClaudeBridge] 写入失败：进程未运行")
            return False
        if self._proc.stdin is None or self._proc.stdin.closed:
            logger.warning("[ClaudeBridge] 写入失败：stdin 已关闭")
            return False
        try:
            line = json.dumps(data, ensure_ascii=False) + "\n"
            self._proc.stdin.write(line.encode("utf-8"))
            self._proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError) as e:
            logger.warning("claude stdin 写入失败: %s", e)
            return False

    def kill(self) -> None:
        if self._proc:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                deadline = time.time() + 8
                while self._proc.poll() is None and time.time() < deadline:
                    time.sleep(0.2)
                if self._proc.poll() is None:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            self._proc = None

    def close(self) -> None:
        self.kill()


# ── Claude stream-json -> ChatJob 事件转换 ──────────────────────────

class ClaudeEventTransformer:
    """将 Claude Code stdout JSON-lines 转换为 ChatJob SSE 事件"""

    def __init__(self):
        self._prev_texts: dict[str, str] = {}   # assistant_msg_id -> 上一次完整文本（用于 diff）
        self._prev_thinks: dict[str, str] = {}  # 同上，用于 thinking content
        self._pending_tool: str = ""            # 最近一次 tool_use 的 id（等待补发 tool_result）
        self.session_id: str = ""
        self.model: str = ""
        self.tools: list = []

    def transform(self, raw_line: str) -> list[tuple[str, Any]]:
        """解析一行 JSON，返回 [(event_name, event_data), ...]"""
        try:
            event = json.loads(raw_line.strip())
        except (json.JSONDecodeError, ValueError):
            return []

        etype = event.get("type", "")

        # ── system: init ──
        if etype == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id", "")
            self.model = event.get("model", "")
            self.tools = event.get("tools", [])
            return []

        # ── system: thinking_tokens（思考进度，映射为 reasoning 进度）──
        if etype == "system" and event.get("subtype") == "thinking_tokens":
            delta = event.get("estimated_tokens_delta", 0)
            if delta > 0:
                return [("reasoning", {"response": "·"})]
            return []

        # ── assistant（核心内容）──
        if etype == "assistant":
            msg = event.get("message", {})
            content_parts = msg.get("content", [])
            msg_id = msg.get("id", "")
            usage = msg.get("usage", {})
            results = []

            for part in content_parts:
                part_type = part.get("type", "")

                # thinking content → reasoning 事件（增量 diff）
                if part_type == "thinking":
                    full_think = part.get("thinking", "")
                    prev = self._prev_thinks.get(msg_id, "")
                    if full_think != prev:
                        self._prev_thinks[msg_id] = full_think
                        delta = full_think[len(prev):] if full_think.startswith(prev) else full_think
                        if delta:
                            results.append(("message_think", delta))

                # text content → content 事件（增量 diff）
                # 同时补发上一个 tool_use 的 tool_result（Claude Code 内部执行工具不推送结果事件）
                elif part_type == "text":
                    if self._pending_tool:
                        results.append(("tool_result", {"tool": "", "result": "", "id": self._pending_tool}))
                        self._pending_tool = ""
                    full_text = part.get("text", "")
                    prev = self._prev_texts.get(msg_id, "")
                    if full_text != prev:
                        self._prev_texts[msg_id] = full_text
                        delta = full_text[len(prev):] if full_text.startswith(prev) else full_text
                        if delta:
                            results.append(("message", delta))

                # tool_use → tool_call 事件
                # 先补发上一个 tool_use 的 tool_result（多工具连续调用场景）
                elif part_type == "tool_use":
                    if self._pending_tool:
                        results.append(("tool_result", {"tool": "", "result": "", "id": self._pending_tool}))
                    tool_name = part.get("name", "")
                    tool_input = part.get("input", {})
                    tool_id = part.get("id", "")
                    args_str = json.dumps(tool_input, ensure_ascii=False)
                    results.append(("tool_call", {
                        "tool": tool_name, "args": args_str, "id": tool_id,
                    }))
                    self._pending_tool = tool_id

            # usage 事件（仅 output_tokens > 0 时发送）
            if usage and usage.get("output_tokens", 0) > 0:
                results.append(("usage", {"usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                }}))

            return results

        # ── tool_result（工具执行结果，Claude Code 有时会推送）──
        if etype == "tool_result":
            return [("tool_result", {
                "tool": event.get("tool", ""),
                "result": str(event.get("output", "") or event.get("content", ""))[:50000],
                "id": event.get("tool_use_id", ""),
            })]

        return []


# ── 同步入口：在后台线程中运行 ──────────────────────────────────────────

def run_claude_chat(
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
    Claude 模式的对话入口（在后台线程中运行）。
    与 agent.chat() / run_opencode_chat() 同级，结果通过 job.append() 推到 SSE 流。
    session_id 以 UUID 形式（claude 会话 ID）传入时自动 --resume 续聊；
    首次对话（非 UUID session_id）新建会话，真实 claude session_id 通过 meta_info 事件返回前端。
    """
    ws = workspace or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 续聊判定：session_id 是合法 UUID → claude 历史 会话；否则新建
    import re as _re
    reuse_sid = session_id if _re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", session_id or ""
    ) else ""

    logger.info("claude 模式启动 model=%s workspace=%s base=%s reuse_sid=%s", model, ws, base_url, reuse_sid or "(新建)")

    proc = ClaudeProcess(
        workspace=ws,
        effort=reasoning_effort,
        system_prompt=system_prompt or "",
        reuse_sid=reuse_sid,
        api_key=api_key,
        base_url=base_url,
    )

    if hasattr(job, "attach_agent"):
        job.attach_agent(proc)

    try:
        proc.start()

        # 写入用户消息（Claude Code stream-json input 协议）
        user_event = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": user_input}],
            },
        }
        if not proc.write_message(user_event):
            job.append("error", {"data": "claude stdin 写入失败（进程未启动）"})
            job.append("message_end", None)
            job.finish("error")
            return

        # ── 排空 stderr 防止缓冲区满导致子进程阻塞 ──
        def _drain_stderr():
            try:
                for _ in iter(proc._proc.stderr.readline, b''):
                    pass
            except Exception:
                pass
        threading.Thread(target=_drain_stderr, daemon=True).start()

        transformer = ClaudeEventTransformer()
        buf = b""  # 行缓冲：claude 输出以 \n 分隔的 JSON-lines
        # P2-37: 限制 buf 上限，防止单行超大工具结果打爆内存
        MAX_BUF_SIZE = 20 * 1024 * 1024  # 20MB
        t0 = time.time()
        TIMEOUT = 300  # 5 分钟总超时
        done = False
        sent_meta = False

        while proc.is_alive() and (time.time() - t0) < TIMEOUT and not done:
            ready, _, _ = select.select([proc._proc.stdout], [], [], 2)
            if not ready:
                continue
            try:
                chunk = os.read(proc._proc.stdout.fileno(), 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            # P2-37: buf 超限直接丢弃，避免 OOM
            if len(buf) > MAX_BUF_SIZE:
                logger.warning("[ClaudeBridge] buf 超过 %dMB，截断后续输入", MAX_BUF_SIZE // 1024 // 1024)
                buf = b""
            # 按 \n 切分完整行
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                for ev, data in transformer.transform(line_str):
                    job.append(ev, data)
                # init 事件：把真实 claude session_id 通知前端（首次对话后前端用它续聊）
                if transformer.session_id and not sent_meta:
                    job.append("meta_info", {
                        "user_msg_id": session_id,
                        "ai_msg_id": transformer.session_id,
                        "claude_session_id": transformer.session_id,
                    })
                    sent_meta = True
                # result 事件 = 对话结束
                try:
                    obj = json.loads(line_str)
                    if obj.get("type") == "result":
                        usage = obj.get("usage", {})
                        if usage:
                            job.append("usage", {"usage": {
                                "input_tokens": usage.get("input_tokens", 0),
                                "output_tokens": usage.get("output_tokens", 0),
                            }})
                        if obj.get("is_error"):
                            job.append("error", {"data": obj.get("error", "claude 请求失败")[:500]})
                        done = True
                        break
                except (json.JSONDecodeError, ValueError):
                    pass

        job.append("message_end", None)
        job.finish("done")

    except Exception as e:
        logger.exception("claude 模式对话异常")
        job.append("error", {"data": f"claude 异常: {str(e)[:500]}"})
        job.append("message_end", None)
        job.finish("error")
    finally:
        proc.close()


# ── claude 会话历史查询（读取 ~/.claude/projects/<dir-slug>/*.jsonl）──────

def _claude_project_dir(workspace: str = "") -> str:
    """根据 workspace 路径推导 claude 项目目录名（路径中的 / 替换为 -）"""
    ws = os.path.abspath(workspace or "/")
    return os.path.expanduser("~/.claude/projects/" + ws.replace("/", "-"))


def query_claude_sessions(workspace_filter: str = "") -> list[dict]:
    """查询 claude 会话列表（从 ~/.claude/projects/ 下扫描 jsonl）。

    注意：claude 的实际结构是 <project>/<sid>/ 目录里只有 subagents/ 和
    tool-results/，主会话 jsonl 不在第一层。这里递归扫描所有 *.jsonl
    （包括 subagents/agent-*.jsonl），按文件 mtime 排序，统一作为 session
    候选，确保历史不丢。
    """
    import glob
    if workspace_filter:
        base = _claude_project_dir(workspace_filter)
        # 指定 workspace 时递归扫该 project 下全部 jsonl（含 subagents）
        all_files = glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)
    else:
        # 递归扫所有 project 目录下的 jsonl
        all_files = glob.glob(
            os.path.expanduser("~/.claude/projects/**/*.jsonl"),
            recursive=True,
        )
    sessions = []
    for f in all_files:
        try:
            mtime = os.path.getmtime(f)
            # 跳过 tool-results 目录下的文件（那是工具结果片段，不是主会话）
            if "/tool-results/" in f:
                continue
            # 文件名：subagents/agent-xxx.jsonl 取 agent-xxx；其它用 .jsonl 去后缀
            base_name = os.path.basename(f)[:-6]
            if base_name.startswith("agent-"):
                sid = base_name
            else:
                sid = base_name
            first_user = ""
            file_cwd = ""
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    # 首次拿到 cwd 就记录（jsonl 每行都带 cwd 字段）
                    if not file_cwd and d.get("cwd"):
                        file_cwd = str(d["cwd"])
                    if d.get("type") == "user":
                        content = d.get("message", {}).get("content", [])
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    txt = (c.get("text") or "").strip()
                                    if txt and not txt.startswith("<"):
                                        first_user = txt[:50]
                                        break
                        elif isinstance(content, str):
                            txt = content.strip()
                            if txt and not txt.startswith("<"):
                                first_user = txt[:50]
                        if first_user:
                            break
            if not first_user:
                first_user = "claude 会话"
            sessions.append({
                "id": sid,
                "title": first_user,
                "time_updated": int(mtime * 1000),
                "directory": file_cwd,
                "source": "claude",
            })
        except Exception:
            continue
    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions[:200]


def query_claude_history(session_id: str, workspace: str = "") -> list[dict]:
    """
    从 claude 项目 jsonl 读取完整会话历史，输出历史兼容格式：
    - user: content 数组 [{type:text,...}]
    - assistant: content 文本 + tool_calls
    - tool: tool_call_id + content
    """
    # 在所有项目目录中查找该会话文件（claude 按 cwd 分目录）
    candidates = []
    if workspace:
        candidates.append(os.path.join(_claude_project_dir(workspace), f"{session_id}.jsonl"))
    # 搜索所有项目目录（兜底）
    import glob as _glob
    candidates.append(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))

    path = None
    for c in candidates:
        hits = _glob.glob(c)
        if hits:
            path = hits[0]
            break
    if not path or not os.path.exists(path):
        return []

    history = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                try:
                    d = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                dtype = d.get("type", "")
                ts_raw = d.get("timestamp", "")
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp() if ts_raw else 0
                except Exception:
                    ts = 0

                if dtype == "user":
                    content = d.get("message", {}).get("content", [])
                    texts = []
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") == "text":
                                txt = c.get("text") or ""
                                if txt.strip() and not txt.startswith("<local-command"):
                                    texts.append(txt)
                    elif isinstance(content, str) and content.strip():
                        texts.append(content)
                    if texts:
                        history.append({
                            "role": "user",
                            "content": [{"type": "text", "text": t} for t in texts],
                            "timestamp": ts,
                        })

                elif dtype == "assistant":
                    content = d.get("message", {}).get("content", [])
                    text = ""
                    tool_calls = []
                    tool_results = {}
                    if isinstance(content, list):
                        for c in content:
                            ctype = c.get("type", "") if isinstance(c, dict) else ""
                            if ctype == "text":
                                text += c.get("text", "")
                            elif ctype == "thinking":
                                pass  # 思考内容不进历史回放
                            elif ctype == "tool_use":
                                tool_calls.append({
                                    "id": c.get("id", ""),
                                    "function": {
                                        "name": c.get("name", ""),
                                        "arguments": json.dumps(c.get("input", {}), ensure_ascii=False),
                                    },
                                })
                    if not text.strip() and not tool_calls:
                        continue
                    history.append({
                        "role": "assistant",
                        "content": text,
                        "tool_calls": tool_calls,
                        "timestamp": ts,
                    })
                    for tc in tool_calls:
                        history.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_results.get(tc["id"], ""),
                            "timestamp": ts + 0.001,
                        })
    except Exception:
        logger.warning("claude 历史查询失败: %s", path, exc_info=True)
        return []
    return history
