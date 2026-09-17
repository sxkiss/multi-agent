"""
codex 模式桥梁：管理 codex exec 子进程生命周期，
通过 stdin/stdout JSONL 协议通信（--json 模式），
将 Codex CLI 的输出事件转换为 ChatJob SSE 事件格式。

Rollout 历史文件格式（~/.codex/sessions/<date>/rollout-*.jsonl）：
  {"timestamp":"...","ordinal":N,"type":"response_item","payload":{"type":"message","role":"developer|user","content":[...]}}
"""

import json
import logging
import os
import re
import select
import signal
import subprocess
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Codex CLI 路径定位 ──────────────────────────────────────────────────

def _codex_path() -> str:
    candidates = [
        os.path.expanduser("~/.local/nodejs/node-latest/bin/codex"),
        os.path.expanduser("~/.local/bin/codex"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return "codex"


# ── Codex CLI 子进程管理 ──────────────────────────────────────────────

class CodexProcess:
    def __init__(self, workspace: str, effort: str = "max",
                 system_prompt: str = "", reuse_thread_id: str = "",
                 model: str = "auto", base_url: str = "", api_key: str = ""):
        self.workspace = workspace
        self.effort = effort
        self.system_prompt = system_prompt
        self.reuse_thread_id = reuse_thread_id
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self._proc: subprocess.Popen | None = None

    def start(self, prompt: str = "") -> None:
        env = os.environ.copy()
        if self.api_key:
            env["OPENAI_API_KEY"] = self.api_key
        if self.base_url:
            env["OPENAI_BASE_URL"] = self.base_url

        # 补 PATH（systemd 环境可能缺 nodejs bin）
        node_bin = os.path.dirname(_codex_path())
        if os.path.isdir(node_bin) and node_bin not in env.get("PATH", ""):
            env["PATH"] = node_bin + os.pathsep + env.get("PATH", "")

        cmd = [
            _codex_path(),
            "exec",
            "--json",
            "--skip-git-repo-check",
        ]
        # 续聊：codex exec resume <thread_id> <prompt>
        if self.reuse_thread_id:
            cmd += ["resume", self.reuse_thread_id]
        # prompt 作为命令行参数（codex exec 不读 stdin）
        if prompt:
            cmd.append(prompt)
        if self.model and self.model != "auto":
            cmd += ["-c", f'model="{self.model}"']
        # 系统提示词通过 -c 覆盖（codex 无 --append-system-prompt）
        if self.system_prompt:
            # TOML 字符串需要转义引号
            sp = self.system_prompt.replace('"', '\\"')
            cmd += ["-c", f'agent.system_prompt="{sp}"']

        logger.info("codex 启动: %s ...", " ".join(cmd[:8]))
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=self.workspace,
            env=env, start_new_session=True,
        )

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

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


# ── Codex JSONL (--json 输出) → ChatJob SSE 事件转换 ──────────────────

class CodexEventTransformer:
    """将 codex exec --json stdout JSONL 转换为 ChatJob SSE 事件"""

    def __init__(self):
        self.thread_id: str = ""
        self._prev_texts: dict[str, str] = {}   # msg_id -> last full text
        self._prev_thinks: dict[str, str] = {}   # msg_id -> last full thinking
        self._pending_tool: str = ""

    def transform(self, raw_line: str) -> list[tuple[str, Any]]:
        try:
            ev = json.loads(raw_line.strip())
        except Exception:
            return []
        return self._handle(ev)

    def _handle(self, ev: dict) -> list[tuple[str, Any]]:
        etype = ev.get("type", "")
        item = ev.get("item", {}) or {}
        results = []

        # thread.started
        if etype == "thread.started":
            self.thread_id = ev.get("thread_id", "")
            return []

        # turn.started
        if etype == "turn.started":
            return []

        # item.completed：核心事件
        if etype == "item.completed":
            item_type = item.get("type", "")
            if item_type == "reasoning":
                text = item.get("text", "")
                results.append(("message_think", text))
            elif item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    results.append(("message", text))
            elif item_type == "error":
                msg = item.get("message", "codex 错误")
                results.append(("error", {"data": msg[:500]}))
            elif item_type == "tool_use":
                tool_name = item.get("name", "")
                tool_input = item.get("input", {})
                tool_id = item.get("id", "")
                args_str = json.dumps(tool_input, ensure_ascii=False)
                results.append(("tool_call", {"tool": tool_name, "args": args_str, "id": tool_id}))
                self._pending_tool = tool_id
            elif item_type == "tool_result":
                output = item.get("output", "") or item.get("content", "")
                results.append(("tool_result", {
                    "tool": item.get("tool", ""),
                    "result": str(output)[:50000],
                    "id": item.get("tool_use_id", ""),
                }))
                self._pending_tool = ""
            return results

        # turn.completed：结束 + usage
        if etype == "turn.completed":
            usage = ev.get("usage", {})
            if usage:
                results.append(("usage", {"usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                }}))
            results.append(("stop", {"usage": {}}))
            return results

        return []


# ── 同步入口：在后台线程中运行 ──────────────────────────────────────────

def run_codex_chat(
    session_id: str, user_input: str, job: Any,
    model: str = "auto", system_prompt: str | None = None,
    workspace: str = "", reasoning_effort: str = "max",
    base_url: str = "", api_key: str = "",
) -> None:
    ws = workspace or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # 续聊判定：UUID 格式即为 codex thread_id
    reuse_tid = session_id if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", session_id or ""
    ) else ""

    logger.info("codex 模式启动 model=%s workspace=%s reuse=%s", model, ws, reuse_tid or "(新建)")

    proc = CodexProcess(
        workspace=ws, effort=reasoning_effort,
        system_prompt=system_prompt or "", reuse_thread_id=reuse_tid,
        model=model, base_url=base_url, api_key=api_key,
    )
    if hasattr(job, "attach_agent"):
        job.attach_agent(proc)

    try:
        proc.start(prompt=user_input)

        # 排空 stderr
        def _drain_stderr():
            try:
                for _ in iter(proc._proc.stderr.readline, b''):
                    pass
            except Exception:
                pass
        threading.Thread(target=_drain_stderr, daemon=True).start()

        transformer = CodexEventTransformer()
        buf = b""
        MAX_BUF = 20 * 1024 * 1024
        t0 = time.time()
        TIMEOUT = 300
        done = False
        sent_meta = False

        # 循环读 stdout（进程退出后也要把残留数据读完）
        while not done and (time.time() - t0) < TIMEOUT:
            ready, _, _ = select.select([proc._proc.stdout], [], [], 2)
            if not ready:
                if not proc.is_alive():
                    # 进程已退出，再扫一次残留数据
                    try:
                        remaining = os.read(proc._proc.stdout.fileno(), 65536)
                        if not remaining:
                            break
                        buf += remaining
                    except OSError:
                        break
                continue
            try:
                chunk = os.read(proc._proc.stdout.fileno(), 65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_BUF:
                buf = b""
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                for ev, data in transformer.transform(line_str):
                    job.append(ev, data)
                # 首次 meta_info
                if transformer.thread_id and not sent_meta:
                    job.append("meta_info", {
                        "user_msg_id": session_id,
                        "ai_msg_id": transformer.thread_id,
                        "codex_thread_id": transformer.thread_id,
                    })
                    sent_meta = True
                # turn.completed = 结束
                try:
                    obj = json.loads(line_str)
                    if obj.get("type") == "turn.completed":
                        done = True
                        break
                except Exception:
                    pass

        job.append("message_end", None)
        job.finish("done")
    except Exception as e:
        logger.exception("codex 模式对话异常")
        job.append("error", {"data": f"codex 异常: {str(e)[:500]}"})
        job.append("message_end", None)
        job.finish("error")
    finally:
        proc.close()


# ── Rollout 历史解析（~/.codex/sessions/<date>/rollout-*.jsonl）────────

def _codex_sessions_dir() -> str:
    return os.path.expanduser("~/.codex/sessions")


def query_codex_sessions(workspace_filter: str = "") -> list[dict]:
    """扫描 ~/.codex/sessions/<date>/rollout-*.jsonl，提取标题/时间/目录。

    workspace_filter 非空时：
      1) 仅显示该 workspace 下的会话（按 cwd 字段精确匹配）
      2) 标记 workspace 字段供前端使用
    rollout 文件顶层包含 cwd + session_meta，可直接读出
    """
    import glob
    sessions = []
    base = _codex_sessions_dir()
    if not os.path.isdir(base):
        return sessions
    for f in glob.glob(os.path.join(base, "**", "rollout-*.jsonl"), recursive=True):
        try:
            mtime = os.path.getmtime(f)
            tid = os.path.basename(f).replace(".jsonl", "")  # rollout-YYYY-MM-DDTHH-MM-SS-UUID
            file_cwd = ""
            first_user = ""
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    # rollout 顶层带 cwd / session_meta
                    if not file_cwd and d.get("type") == "session_meta":
                        sm = d.get("payload", {}) or {}
                        file_cwd = sm.get("cwd", "") or ""
                    if d.get("type") != "response_item":
                        continue
                    payload = d.get("payload", {})
                    if payload.get("type") != "message":
                        continue
                    role = payload.get("role", "")
                    if role != "user":
                        continue
                    for c in payload.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "input_text":
                            txt = (c.get("text") or "").strip()
                            if txt and not txt.startswith("<"):
                                first_user = txt[:50]
                                break
                    if first_user:
                        break
            if not first_user:
                first_user = "codex 会话"
            # workspace 过滤：空 = 全部；非空 = 严格匹配 cwd
            if workspace_filter and file_cwd != workspace_filter:
                continue
            sessions.append({
                "id": tid,
                "title": first_user,
                "time_updated": int(mtime * 1000),
                "directory": file_cwd,
                "source": "codex",
            })
        except Exception:
            continue
    sessions.sort(key=lambda s: s["time_updated"], reverse=True)
    return sessions[:200]


def query_codex_history(thread_id: str, workspace: str = "") -> list[dict]:
    """
    thread_id 支持两种格式：
      - 纯 UUID (msg_ / 从 meta_info 获得的 codex_thread_id)
      - rollout-前缀文件名 (rollout-YYYY-MM-DDTHH-MM-SS-UUID)
    """
    import glob as _glob
    base = _codex_sessions_dir()
    if not os.path.isdir(base):
        return []

    # 尝试匹配文件名
    path = None
    # 精确 rollout-<tid>.jsonl
    hits = _glob.glob(os.path.join(base, "**", f"rollout-{thread_id}.jsonl"), recursive=True)
    if hits:
        path = hits[0]
    else:
        # 模糊：只含该 UUID 的 rollout 文件
        hits = _glob.glob(os.path.join(base, "**", f"*{thread_id}*.jsonl"), recursive=True)
        for h in hits:
            if os.path.basename(h).endswith(f"{thread_id}.jsonl"):
                path = h
                break

    if not path or not os.path.exists(path):
        return []

    history = []
    tool_results_cache = {}  # call_id -> output（function_call_output）
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                payload = d.get("payload", {})
                # 提前收集 function_call_output，按 call_id 索引
                if payload.get("type") == "function_call_output":
                    cid = payload.get("call_id", "")
                    if cid:
                        tool_results_cache[cid] = str(payload.get("output", "") or "")
                    continue
                if payload.get("type") != "message":
                    continue
                role = payload.get("role", "")
                ts_raw = d.get("timestamp", "")
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp() if ts_raw else 0
                except Exception:
                    ts = 0

                if role == "user":
                    texts = []
                    for c in payload.get("content", []):
                        if isinstance(c, dict) and c.get("type") == "input_text":
                            txt = (c.get("text") or "").strip()
                            if txt and not txt.startswith("<"):
                                texts.append(txt)
                    if texts:
                        history.append({
                            "role": "user",
                            "content": [{"type": "text", "text": t} for t in texts],
                            "timestamp": ts,
                        })

                elif role in ("developer", "assistant"):
                    text = ""
                    tool_calls = []
                    for c in payload.get("content", []):
                        if isinstance(c, dict):
                            ctype = c.get("type", "")
                            if ctype == "input_text":
                                text += (c.get("text") or "")
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
                    # 补发 tool 结果（用 call_id 匹配 codex 的 function_call_output）
                    for tc in tool_calls:
                        tid = tc["id"]
                        out = tool_results_cache.get(tid, "")
                        history.append({
                            "role": "tool",
                            "tool_call_id": tid,
                            "content": out,
                            "timestamp": ts + 0.001,
                        })
    except Exception:
        logger.warning("codex 历史查询失败: %s", path, exc_info=True)
        return []
    return history
