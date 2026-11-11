"""bt 真·实时增量会话端点 + 每实例独立工位（2026-11-11 续办）。

语义（BOSS 逐轮校准后定稿）：
- 以「大屏 SSE 连上那一刻」为界，只呈现此后真正新产生的事件；首屏不灌历史工单。
- 工位 = **每个 agent 运行实例**（Task 子代理 / RunCrew 集团团队一视同仁），
  共享同一套运行源 ``get_background_manager()._tasks``（web 单进程单 worker、同进程
  直读，实证见交付报告）；区别**只在各自被允许调用的工具集**（allowed_tools /
  strict_tools），**不再按 mode 分家**，也不再按工具名聚合成「一个工位」。
- 工具调用 = 某个实例工位上的「活动 / 工单」，不再是工位本身。

实例身份（全部真实可推导，非硬造）：
- 稳定 id = ``bg_task_id``（_BgTask.task_id / tool_result 的 <task_id> / background_result.task_id）；
- 派发关联键 = ``tool_call.id``（真实配对键，BOSS 明确认可的「与 tool_call.id 的关联」）；
- 类型 = ``subagent_type``（tool_call.args，真实）或 _BgTask.tool（Task=子代理 / RunCrew=集团）。

Add-only：/api/console/{agents,events,stream} 三路由，boot.js + bridge.js 零改可用。
增量源 = jobs/*.jsonl 文件尾部新增字节（chat_jobs.py:81 每事件 f.write 落盘），
按连接时 (文件,字节偏移) 基线比较；不再按 timestamp / Date.now()-5 过滤，不全量重扫历史。
复用 bt 既有鉴权口径（admin/service），不改 auth.py、不加白名单；
EventSource/同步XHR/fetch 不能带 header，由前端 bt-token.js 附 ?token=。
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("viz-stream")

router = APIRouter(tags=["viz-stream"])
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JOBS_DIR = os.path.join(_PROJECT_ROOT, "jobs")

# 只保留「逐条内容事件」；meta_info / usage / message_end / __status__ 非内容，忽略
_CONTENT_EVENTS = {"tool_call", "tool_result", "message", "message_think"}
_POLL_SEC = 0.5          # 增量轮询间隔（文件尾部，开销极小）
_MAX_BATCH = 200         # 单帧最多推的真实事件数
_DISPATCH_TOOLS = {"Task", "RunCrew"}          # 派发 agent 实例的工具（不是工位）
_TASK_ID_RE = re.compile(r"<task_id>\s*([0-9a-fA-F-]{8,})\s*</task_id>")

# 模块级「实例工位注册表」：web 单 worker 内 /agents 与 /stream 共享，保证 name 对得上。
# 单进程单 worker 是硬前提（实证），多 worker 下此注册表不跨进程，见报告「诚实局限」。
_REG_LOCK = threading.RLock()
_REG: dict[str, Any] = {
    "seats": {},        # seat_name -> 工位信息（每个 agent 实例一条）
    "task_inst": {},    # bg_task_id  -> seat_name（稳定实例 id 关联）
    "call_inst": {},    # tool_call.id -> seat_name（派发关联键）
    "sub_seat": {},     # subagent_type -> seat_name（显示/归属补齐）
    "built_at": 0.0,
}


def _auth_ok(request: Request) -> bool:
    """admin / service: 放行；与 platform_ext.viz._auth_ok 同口径。"""
    try:
        from auth import extract_token, is_admin_token, verify_token
        t = extract_token(request)
        if not t:
            return False
        if is_admin_token(_PROJECT_ROOT, t):
            return True
        p = verify_token(_PROJECT_ROOT, t)
        return bool(p) and str(p.get("sub") or "").startswith("service:")
    except Exception:
        logger.warning("[viz-stream] 鉴权异常", exc_info=True)
        return False


def _job_short(job_key: str) -> str:
    return job_key.replace("::", ":")[-8:]


def _seat_name(kind: str, iid: str) -> str:
    """稳定实例工位键 = 类型 + 实例 id 摘要。不同实例 id → 不同键 → 天然不合并。

    注意：不是工具名、不是子代理「类型」，而是**运行实例**（bg_task_id / call.id）的摘要。
    """
    key = re.sub(r"[^0-9a-zA-Z]", "", str(iid))[:8] or "anon"
    return f"{kind}·{key}"


def _kind_of(tool: str, sub: str = "") -> str:
    """运行实例的类别：子代理 / 集团 / 后台。三者是同一类运行实体，只是工具集不同。"""
    if tool == "Task" or sub:
        return "子代理"
    if tool == "RunCrew":
        return "集团"
    return "后台"


def _args_dict(args) -> dict:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            return {}
    return args if isinstance(args, dict) else {}


def _ensure_seat(reg, seat, *, instance_id="", kind="后台", tool="", status="",
                 parent_job="", session_id="", start_time=0.0, sub="", tools=None):
    """登记（或补齐）一个实例工位。不改已有实例身份，只补字段。"""
    s = reg["seats"].get(seat)
    if s is None:
        s = {"name": seat, "instance_id": instance_id, "kind": kind,
             "tool": tool, "status": status, "parent_job": parent_job,
             "session_id": session_id, "start_time": start_time,
             "sub": sub, "tools": [], "inbound": 0, "outbound": 0,
             "total": 0, "last_ts": start_time}
        reg["seats"][seat] = s
    for k, v in (("instance_id", instance_id), ("sub", sub), ("status", status),
                 ("parent_job", parent_job), ("session_id", session_id)):
        if v and not s.get(k):
            s[k] = v
    if start_time and not s.get("start_time"):
        s["start_time"] = start_time
        s["last_ts"] = start_time
    if tools:
        for t in tools:
            if t and t not in s["tools"]:
                s["tools"].append(t)
    return s


def _live_instances() -> list[dict]:
    """同进程直读共享运行源 ``get_background_manager()._tasks``（实证：9876 单 worker）。

    每条 _BgTask = 一个 agent 运行实例；字段全部来自内存态：
    task_id / tool / status / parent_job_key / session_id / start_time。
    跨进程 / import 失败 / 无实例 → 返回空（如实降级，绝不硬造实例）。
    """
    try:
        from chat_client.tools import get_background_manager
        bgm = get_background_manager()
        with bgm._lock:
            snap = [(t.task_id, t.tool, t.status, t.parent_job_key,
                     t.session_id, t.start_time) for t in bgm._tasks.values()]
        return [{"instance_id": a, "tool": b, "status": c, "parent_job": d,
                 "session_id": e, "start_time": f} for a, b, c, d, e, f in snap]
    except Exception as ex:
        logger.debug("[viz-stream] 内存运行源不可读（跨进程/未加载）：%s", ex)
        return []


def _scan_dispatch_from_jsonl(reg):
    """从落盘 jsonl 的派发事件，为**内存活跃实例**补 call.id / subagent_type 归属映射。

    只补「实例归属 / 显示信息」，**绝不凭空建历史工位**（不灌历史）；
    jsonl 里无对应内存实例的历史派发事件一律跳过（那些是历史，不进花名册）。
    """
    live_tasks = set(reg["task_inst"].keys())       # 仅内存活跃实例
    if not live_tasks:
        return
    try:
        files = glob.glob(os.path.join(_JOBS_DIR, "*.jsonl"))
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for f in files[:60]:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(r, dict) or r.get("event") not in ("tool_call", "tool_result"):
                        continue
                    d = r.get("data")
                    if not isinstance(d, dict):
                        continue
                    tool = str(d.get("tool") or "")
                    cid = str(d.get("id") or "")
                    if tool not in _DISPATCH_TOOLS or not cid:
                        continue
                    sub = ""
                    tid = ""
                    if r.get("event") == "tool_call":
                        sub = str(_args_dict(d.get("args")).get("subagent_type") or "")
                    else:
                        m = _TASK_ID_RE.search(str(d.get("result") or ""))
                        tid = m.group(1) if m else ""
                    if tid not in live_tasks:
                        continue                      # 历史/外来实例：不建工位
                    seat = reg["task_inst"][tid]
                    reg["call_inst"][cid] = seat
                    if sub:
                        reg["sub_seat"][sub] = seat
                        _ensure_seat(reg, seat, sub=sub, tools=[tool])
    except Exception:
        logger.warning("[viz-stream] 派发事件扫描失败", exc_info=True)


def _build_registry() -> dict:
    """**只补不删**活跃实例（2026-11-11 续办修复）。

    主源 = 内存 _tasks（共享运行源直读），副源 = 落盘派发补映射。
    关键：绝不 clear seats / task_inst / call_inst / sub_seat —— 这些是 /stream
    逐帧累积的「真实事件→实例工位」归属映射，重复调 /agents（boot.js 每次刷新）
    必须保留，否则新事件的 call.id 会丢失实例归属、又退回兜底工位。
    - 对当前内存活跃实例：补登记（_ensure_seat 幂等）+ 刷新内存态 status；
    - 对已消失的内存实例：只把 status 标终态，**不删工位、不删任何映射**；
    - 工位是稳定实例身份（不是瞬时工单），保留不影响「项目/工单区开屏空」。
    """
    with _REG_LOCK:
        reg = _REG
        live_seats: set[str] = set()
        for ins in _live_instances():
            tid = ins["instance_id"]
            kind = _kind_of(ins["tool"])
            seat = _seat_name(kind, tid)
            live_seats.add(seat)
            reg["task_inst"][tid] = seat
            s = _ensure_seat(reg, seat, instance_id=tid, kind=kind, tool=ins["tool"],
                             status=ins["status"], parent_job=ins["parent_job"],
                             session_id=ins["session_id"], start_time=ins["start_time"],
                             tools=[ins["tool"]])
            s["status"] = ins["status"]          # 唯一会随运行变化的字段：内存态
        for seat, s in reg["seats"].items():
            if (s.get("instance_id") and seat not in live_seats
                    and s.get("status") not in ("done", "failed", "stopped")):
                s["status"] = "done"            # 只标态，绝不删除
        _scan_dispatch_from_jsonl(reg)          # 增量补映射，不清空
        reg["built_at"] = time.time()
        return reg


def _current_reg() -> dict:
    with _REG_LOCK:
        if not _REG["built_at"]:
            return _build_registry()
        return _REG


def _agent_status(st: str) -> str:
    """内存态 → boot.js 认的 online / idle / offline。"""
    if st in ("running", "queued"):
        return "online"
    if st == "pending":
        return "idle"
    return "offline"


def _prefetch_dispatch(lines: list[str], reg, ts: float):
    """预读本批新增行：从 tool_result 的 <task_id> 把 call.id 归到实例工位。

    这是「共享运行源 + 落盘关联键」的 join：bg_task_id 是内存 _tasks 的 key，
    <task_id> 是落盘派发 result 的真实字段，二者相等即同一实例。
    """
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict) or r.get("event") != "tool_result":
            continue
        d = r.get("data")
        if not isinstance(d, dict):
            continue
        tool = str(d.get("tool") or "")
        cid = str(d.get("id") or "")
        m = _TASK_ID_RE.search(str(d.get("result") or ""))
        if tool not in _DISPATCH_TOOLS or not cid or not m:
            continue
        tid = m.group(1)
        kind = _kind_of(tool)
        with _REG_LOCK:
            seat = reg["task_inst"].get(tid) or _seat_name(kind, tid)
            reg["task_inst"][tid] = seat
            reg["call_inst"][cid] = seat
            _ensure_seat(reg, seat, instance_id=tid, kind=kind, tool=tool,
                         start_time=ts, tools=[tool])


def _parse_line(base: str, line: str, ts: float, seq: int, reg) -> dict | None:
    """一行真实 jsonl → 一条桥接 message。

    ★ name 不再是工具名：改归属到**实例工位**（seat_name）或会话兜底工位；
      工具名降为 content 里的「活动 / 工单」文本。
    全部字段来自真实记录，不伪造配对：request_id 取 tool_call.id / tool_result.id。
    """
    try:
        r = json.loads(line)
    except Exception:
        return None
    if not isinstance(r, dict) or "__status__" in r:
        return None
    ev, d = r.get("event"), r.get("data")
    if ev not in _CONTENT_EVENTS:
        return None
    mid = f"{base}:{seq}"
    session_seat = "会话·" + _job_short(base)
    if ev in ("tool_call", "tool_result") and isinstance(d, dict):
        tool = str(d.get("tool") or "tool")
        cid = str(d.get("id") or "")
        args = _args_dict(d.get("args")) if ev == "tool_call" else {}
        sub = str(args.get("subagent_type") or "")
        kind = _kind_of(tool, sub)
        with _REG_LOCK:
            seat = reg["call_inst"].get(cid)
            if not seat and sub:
                seat = reg["sub_seat"].get(sub)
            if not seat:
                # 落盘 jsonl 无实例标记的普通工具事件：如实归「会话·job」兜底工位，
                # 不硬造实例身份、不把工具名包装成工位（见报告「诚实局限」）。
                seat = session_seat
            reg["call_inst"].setdefault(cid, seat) if cid else None
            s = reg["seats"].get(seat)
            if s is not None:
                if tool not in s["tools"]:
                    s["tools"].append(tool)
                if ev == "tool_call":
                    s["inbound"] += 1
                else:
                    s["outbound"] += 1
                s["total"] = s["inbound"] + s["outbound"]
                s["last_ts"] = ts
        if ev == "tool_call":
            content = (tool + " · " + (sub + " · " if sub else "") + str(args))[:120]
        else:
            content = (tool + " → " + str(d.get("result") or ""))[:120]
        return {"id": mid, "name": seat, "direction": "inbound" if ev == "tool_call" else "outbound",
                "request_id": cid, "content": content, "timestamp": ts, "type": ev}
    if isinstance(d, str) and d.strip():
        return {"id": mid, "name": session_seat, "direction": "inbound",
                "request_id": "", "content": d[:120], "timestamp": ts, "type": ev}
    return None


def _offsets() -> dict[str, int]:
    """当前所有 jobs/*.jsonl 的字节大小快照 → 连接基线 / 增量比较基准。"""
    out: dict[str, int] = {}
    try:
        for f in glob.glob(os.path.join(_JOBS_DIR, "*.jsonl")):
            try:
                out[f] = os.path.getsize(f)
            except OSError:
                pass
    except Exception:
        pass
    return out


def _read_new(baseline: dict[str, int], reg) -> list[dict]:
    """只读取「连接基线之后新增的字节」并解析成 message，原地推进基线。
    - 新出现的文件（连接后创建）→ 从 0 起全部视为新增；
    - 只处理到最后一个完整换行符，未写完的半行留给下一轮（不丢事件）；
    - 不扫描历史尾部、不做 timestamp 过滤 → 历史永不进屏。
    """
    msgs: list[dict] = []
    cur = _offsets()
    ts = time.time()
    for path, size in cur.items():
        start = baseline.get(path, 0)
        if size <= start:
            baseline[path] = size
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                raw = fh.read()
        except OSError:
            continue
        cut = raw.rfind(b"\n")
        if cut < 0:                       # 还没有一行写完整，基线不推进
            continue
        complete = raw[:cut + 1].decode("utf-8", errors="replace")
        baseline[path] = start + cut + 1
        base = os.path.basename(path)[:-6]
        lines = [ln.strip() for ln in complete.splitlines() if ln.strip()]
        _prefetch_dispatch(lines, reg, ts)           # ★ 先 join 实例身份，再逐行归属
        for k, line in enumerate(lines):
            m = _parse_line(base, line, ts + k * 0.0001, start + k, reg)
            if m:
                msgs.append(m)
    for p in list(baseline):
        if p not in cur:
            del baseline[p]
    msgs.sort(key=lambda m: m["timestamp"])
    return msgs


@router.get("/api/console/agents")
async def console_agents(request: Request):
    """boot.js:42 同步取，需非空以便 LIVE=true 跳过 PRESET 假项目；
    boot.js 用 a.role 分组、a.name 作 __WHQ_DEPT_OF__ 键，bridge.js 的 chainOf
    要求事件 name 在该表里才建真工单。

    ★ 返回**实例级工位清单**：每个 agent 运行实例一条（_BgTask 一条 = 一个工位），
      稳定实例 id + 类型[子代理/集团] + 归属父 job + 内存态 status + 调用过的工具集。
      不再是「工具名全集」、不按 mode 两套、不含任何历史工单 / 假人名 / 假项目。
    """
    if not _auth_ok(request):
        return JSONResponse({"msg": "unauthorized"}, status_code=403)
    reg = _build_registry()
    seats = reg["seats"]
    if not seats:
        # 无活跃实例：给一个中性「实时监听」工位保 LIVE=true（跳过 PRESET 假项目），
        # 非假人名 / 非假项目；当前无活跃任务则屏空工单，符合上一轮结论。
        agents = [{"name": "实时监听", "display_name": "实时监听", "role": "运行实例",
                   "inbound": 0, "outbound": 0, "total": 0, "last_ts": 0.0,
                   "status": "idle", "instance_id": "", "kind": "监听",
                   "parent_job": "", "tools": [], "raw_status": ""}]
    else:
        agents = []
        with _REG_LOCK:
            for seat, s in seats.items():
                tail = seat.split("·")[-1]
                disp = (s.get("sub") or s.get("kind")) + "·" + tail
                # role 用真实 subagent_type（或实例 id 尾缀）派生：不同实例 → 不同分组
                # → boot.js groupOf 给不同部门索引 → 屏上各占独立工位而非合并成一个。
                role_name = s.get("sub") or ((s.get("kind") or "后台") + "·" + tail)
                agents.append({
                    "name": seat, "display_name": disp,
                    "role": role_name,
                    "inbound": s["inbound"], "outbound": s["outbound"],
                    "total": s["total"], "last_ts": s["last_ts"],
                    "status": _agent_status(s.get("status") or ""),
                    # Add-only 附加字段（boot.js 不读，供验收 / 调试）：
                    "instance_id": s["instance_id"], "kind": s["kind"],
                    "parent_job": s["parent_job"], "tools": s["tools"],
                    "raw_status": s["status"],
                })
    return JSONResponse({"agents": agents, "source": "bgm._tasks(in-proc)"})


@router.get("/api/console/events")
async def console_events(request: Request, limit: int = 200, since: float = 0.0):
    """boot.js:101 预热 + bridge.js:118 历史热身。
    真·实时：**返回空** —— 首屏项目/工单列表必须为空起步，不灌任何历史。
    since/limit 保留仅为兼容原版前端签名，绝对值一律忽略（连接基线在 /stream 里）。"""
    if not _auth_ok(request):
        return JSONResponse({"msg": "unauthorized"}, status_code=403)
    return JSONResponse({"events": []})


@router.get("/api/console/stream")
async def console_stream(request: Request, since: float = 0.0):
    """SSE 真·实时增量：连接瞬间确立 (文件,字节偏移) 基线，此后只推新增行。
    bridge.js:204 传的 since=Date.now()/1000-5 绝对值被**忽略**（口径与文件偏移无关），
    以服务端本次连接基线为准；不改前端。首帧不发任何历史，只 keepalive。
    ★ 每条新增行的 name 归属到具体实例工位（_prefetch_dispatch / _parse_line）。"""
    if not _auth_ok(request):
        return JSONResponse({"msg": "unauthorized"}, status_code=403)
    reg = _current_reg()

    async def gen():
        baseline = _offsets()             # ★ 界点：连上这一刻，之前全是历史，永不回灌
        yield ": connected (baseline set, history excluded)\n\n"
        for _ in range(1200):            # ~10min，客户端 EventSource 自动重连
            if await request.is_disconnected():
                break
            msgs = _read_new(baseline, reg)
            if msgs:
                for i in range(0, len(msgs), _MAX_BATCH):
                    yield ("event: messages\ndata: " +
                           json.dumps(msgs[i:i + _MAX_BATCH], ensure_ascii=False) + "\n\n")
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(_POLL_SEC)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
