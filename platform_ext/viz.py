"""双模式大屏聚合点 —— GET /api/v2/viz/state

设计约束：
- 新路径 /api/v2/* 隔离，不改动任何旧 /api/* 路由与响应壳；
- **只读**遍历 chat_jobs._jobs，不重放 last_id 游标（绕开 _MAX_EVENTS=2000
  截断、compact_summary / stream_reset 回滚语义）；
- 按 tool_call / tool_result 的 `id` 配对，last-write-wins 折叠；
- **按 job 级聚合忙/闲**，绝不逐 tool 事件驱动大屏状态机
  （一次请求可产生上百 tool_call，逐事件会让画面疯狂抖动）；
- single 建模为「单节点特殊集团态」，与 group 输出同构 VizState；
- service/admin 身份供数，聚合层不持普通用户 token；
- feature flag 默认 off：off 时返回 {"enabled": false, ...} 禁用态，不报错。

本模块**不修改 chat_jobs.py 的任何既有函数**，仅从外部只读其单例。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("platform-ext-viz")

# ── feature flag：已打开（2026-11-10 BOSS 授权）。改本常量不改 config.json 既有键值。
# 注意：这是 Python 模块常量，改动只落磁盘；运行中的服务仍是旧值，需重启才生效。──
VIZ_ENABLED = True

# 空窗期保留的 last snapshot 存活时长（秒），超时则只回 idle 空态
_LAST_SNAPSHOT_TTL = 300.0

router = APIRouter(prefix="/api/v2", tags=["platform-ext-viz"])

# 模块级缓存：仅在内存，用于「无 running job」时回填上一份态。
# 用普通锁即可（跨线程来自 FastAPI 线程池）；不落盘、不改运行中配置。
import threading as _threading

_last_snapshot: dict[str, Any] | None = None
_last_snapshot_at: float = 0.0
_last_lock = _threading.Lock()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _auth_ok(request: Request) -> bool:
    """service/admin 身份放行；普通 token（card:/wx: 等）拒绝。

    对齐 chat_routes._assert_session_owner 的口径：admin 与 sub 为
    service: 前缀的令牌放行。聚合层以服务/管理身份统一供数，
    不持普通用户 token，避免按 session 归属被 403。
    """
    try:
        from auth import extract_token, is_admin_token, verify_token

        token = extract_token(request)
        if not token:
            return False
        if is_admin_token(_PROJECT_ROOT, token):
            return True
        payload = verify_token(_PROJECT_ROOT, token)
        if not payload:
            return False
        return str(payload.get("sub") or "").startswith("service:")
    except Exception:  # noqa: BLE001 - 鉴权依赖异常不应 500 拖垮路由
        logger.warning("[viz] 鉴权校验异常", exc_info=True)
        return False


def _mode_of(job: Any) -> str:
    """job.mode 优先；无则降级扫 events 判 group / solo。"""
    m = getattr(job, "mode", None)
    if isinstance(m, str) and m:
        return "group" if m == "group" else "solo"
    # 降级：仅当 events 里出现过 crew_plan / crew_step 才判 group
    try:
        for e in job.events:
            if e.get("event") in ("crew_plan", "crew_step"):
                return "group"
    except Exception:  # noqa: BLE001
        pass
    return "solo"


def _snapshot_events(job: Any) -> list[dict]:
    """只读拷贝 events（在 job 自己的锁内），避免遍历时被 append 改动。"""
    try:
        lock = getattr(job, "_lock", None)
        if lock:
            with lock:
                return [dict(e) for e in job.events]
        return [dict(e) for e in job.events]
    except Exception:  # noqa: BLE001
        return []


def _fold_group(job: Any, events: list[dict]) -> dict:
    """集团态：从 crew_plan.steps + crew_step 覆盖折叠出节点/边/指标/告警。"""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    title = str(getattr(job, "label", "") or "")
    alerts: list[dict] = []

    # 步骤号(1-based) → agent 名。task.py 的 depends_on 存的是数字步号，
    # 大屏部门链要用名字查 dept，故在此把数字依赖规范化成节点名。
    idx_to_name: dict[int, str] = {}
    steps_from_plan: dict[str, dict] = {}
    for e in events:
        if e.get("event") != "crew_plan":
            continue
        data = e.get("data") or {}
        if isinstance(data, dict):
            title = str(data.get("objective") or title)[:120]
            for pos, s in enumerate(data.get("steps") or [], 1):
                if not isinstance(s, dict):
                    continue
                name = str(s.get("agent") or "")
                if not name:
                    continue
                # step 字段是 1-based 步号（task.py crew_plan）；缺省用枚举序
                try:
                    step_no = int(s.get("step") or pos)
                except (TypeError, ValueError):
                    step_no = pos
                idx_to_name[step_no] = name
                # depends_on 元素可能是数字步号，也可能是 agent 名 → 统一成名字
                norm_deps = []
                for d in (s.get("depends_on") or []):
                    ds = str(d)
                    norm_deps.append(idx_to_name.get(int(ds), ds)
                                     if ds.isdigit() else ds)
                steps_from_plan[name] = {
                    "id": name,
                    "name": name,
                    "dept": str(s.get("dept") or ""),
                    "state": "pending",
                    "task": str(s.get("task") or ""),
                    "ts": s.get("ts"),
                    "level": s.get("level", 0),
                    "depends_on": norm_deps,
                }
    nodes = {k: dict(v) for k, v in steps_from_plan.items()}

    # last-write-wins 覆盖：后面到达的 crew_step 覆盖节点状态
    for e in events:
        if e.get("event") != "crew_step":
            continue
        d = e.get("data") or {}
        if not isinstance(d, dict):
            continue
        name = str(d.get("agent") or "")
        st = str(d.get("status") or "")
        if not name:
            continue
        node = nodes.get(name)
        if node is None:
            node = {"id": name, "name": name, "dept": str(d.get("dept") or ""),
                    "state": "pending", "task": "", "ts": None,
                    "level": d.get("level", 0), "depends_on": d.get("depends_on") or []}
            nodes[name] = node
        if st:
            node["state"] = st
        if d.get("dept"):
            node["dept"] = str(d.get("dept"))
        if d.get("task"):
            node["task"] = str(d.get("task"))
        if d.get("ts"):
            node["ts"] = d.get("ts")
        if st == "fail":
            alerts.append({"level": "error",
                           "msg": f"步骤失败: {name} {node.get('task','')}",
                           "ts": node.get("ts")})

    # 依赖边：优先用节点 depends_on（本次 task.py 补 emit 后可用）；
    # 历史 job 无该字段 → edges=[]（诚实留空，不伪造）
    for name, node in nodes.items():
        for dep in (node.get("depends_on") or []):
            edges.append({"from": str(dep), "to": name})

    node_list = list(nodes.values())
    steps_done = sum(1 for n in node_list if n.get("state") in ("done", "fail"))
    return {
        "title": title,
        "nodes": node_list,
        "edges": edges,
        "metrics": {"steps_done": steps_done, "steps_total": len(node_list),
                    "tool_calls": _count_tool_calls(events)},
        "alerts": alerts,
    }


def _count_tool_calls(events: list[dict]) -> int:
    return sum(1 for e in events if e.get("event") == "tool_call")


def _subagent_nodes(events: list[dict]) -> list[dict]:
    """[真数据] 从真实 `Task` tool_call 重建「子代理」节点，让 solo 不再是 1 个孤节点。

    真实证据（jobs/1794304926080::221635.jsonl）：single 模式会派 Task 子代理，
    tool_call.args 是 JSON 字符串，含 {description, subagent_type, task_id, ...}；
    子代理不是独立 ChatJob（create_if_idle 仅主入口调用），进度经父 job events 回灌，
    因此**只能、且恰好能**从父 job 的 tool_call/tool_result 重建。用 id 配对判 done/running。
    这是「真实派发的子代理」，非伪造；无 Task 调用时返回空列表（诚实留空）。
    """
    import json as _json
    calls: dict[str, dict] = {}
    order: list[str] = []
    results: dict[str, bool] = {}
    for e in events:
        ev = e.get("event")
        d = e.get("data")
        if not isinstance(d, dict):
            continue
        if ev == "tool_call" and str(d.get("tool") or "") == "Task":
            cid = str(d.get("id") or "")
            raw = d.get("args")
            if isinstance(raw, str):
                try:
                    raw = _json.loads(raw)
                except Exception:
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            # 流式 chunk 可能先发空 args、再发完整 args：以最后带 subagent_type/description 的为准
            sa = str(raw.get("subagent_type") or "").strip()
            desc = str(raw.get("description") or "").strip()
            if not cid:
                continue
            if cid not in calls:
                order.append(cid)
                calls[cid] = {"subagent_type": sa, "description": desc,
                              "task_id": str(raw.get("task_id") or "")}
            else:
                if sa:
                    calls[cid]["subagent_type"] = sa
                if desc:
                    calls[cid]["description"] = desc
        elif ev == "tool_result" and str(d.get("tool") or "") == "Task":
            results[str(d.get("id") or "")] = True

    nodes = []
    for cid in order:
        c = calls[cid]
        if not (c["subagent_type"] or c["description"]):
            continue  # 纯流式空壳，跳过
        name = c["subagent_type"] or "子代理"
        dept = "子代理 · " + (c["subagent_type"] or "通用")
        state = "done" if results.get(cid) else "running"
        nodes.append({
            "id": cid, "name": name, "dept": dept, "state": state,
            "task": c["description"][:80], "ts": None, "level": 1, "depends_on": [],
            "kind": "subagent",  # 供前端区分「工位」与「子代理」
        })
    return nodes


def _fold_solo(job: Any, events: list[dict]) -> dict:
    """单节点集团态：按 job 级忙/闲聚合，不逐 tool 驱动。

    - 用 tool_call/tool_result 的 `id` 配对，得出「已配对/未配对」计数；
    - 大屏状态 = job 级：running→忙（running）、终态→闲（done）；
    - 工具流水条数据（tool_pipeline）按配对顺序给出，供前端滚动展示；
    - **2026-11-10**：纳入真实 Task 子代理节点（_subagent_nodes），solo 不再只有 1 个孤节点；
    - 不伪造 request_id（HQ 通道依赖它，solo 走 PARK 直写）。
    """
    calls: dict[str, dict] = {}      # id -> {tool, args}
    results: dict[str, str] = {}     # id -> result 摘要
    order: list[str] = []
    usage: dict[str, Any] = {}
    alerts: list[dict] = []

    for e in events:
        ev = e.get("event")
        d = e.get("data")
        if ev == "tool_call" and isinstance(d, dict):
            cid = str(d.get("id") or "")
            if not cid:
                continue
            if cid not in calls:
                order.append(cid)
            calls[cid] = {"tool": str(d.get("tool") or ""),
                          "args": str(d.get("args") or "")[:120]}
        elif ev == "tool_result" and isinstance(d, dict):
            cid = str(d.get("id") or "")
            results[cid] = str(d.get("result") or "")[:120]
        elif ev == "usage" and isinstance(d, dict):
            usage = d.get("usage") or usage
        elif ev == "error" and isinstance(d, dict):
            alerts.append({"level": "error", "msg": str(d.get("msg") or ""), "ts": None})

    pipeline = []
    for cid in order:
        c = calls.get(cid, {})
        pipeline.append({"id": cid, "tool": c.get("tool", ""),
                         "state": "done" if cid in results else "running"})
    pending = sum(1 for p in pipeline if p["state"] == "running")

    status = str(getattr(job, "status", "") or "")
    # job 级忙/闲：仅 running 视为「开工/忙」，其余（含终态）视为「停机/闲」
    node_state = "running" if status == "running" else "done"
    last_tool = pipeline[-1]["tool"] if pipeline else ""

    subs = _subagent_nodes(events)
    nodes = [{
        "id": "solo", "name": "主工位", "dept": "单 ag",
        "state": node_state,
        "task": (f"{last_tool}…" if node_state == "running" and last_tool else ""),
        "ts": None, "level": 0, "depends_on": [], "kind": "station",
    }] + subs  # 真实子代理并入 nodes[]

    return {
        "title": str(getattr(job, "label", "") or ""),
        "nodes": nodes,
        "edges": [],  # 单节点天然无协作依赖；子代理为并列派发，无依赖边（诚实）
        "metrics": {"tool_calls": len(pipeline), "pending": pending,
                    "subagents": len(subs), "usage": usage},
        "alerts": alerts,
        "tool_pipeline": pipeline,  # 供「单工位 + 工具流水条」呈现
    }


def _build_state(job: Any) -> dict:
    events = _snapshot_events(job)
    mode = _mode_of(job)
    folded = _fold_group(job, events) if mode == "group" else _fold_solo(job, events)
    state: dict[str, Any] = {
        "enabled": True,
        "mode": mode,
        "job_key": str(getattr(job, "key", "") or ""),
        "session_id": str(getattr(job, "session_id", "") or ""),
        "job_status": str(getattr(job, "status", "") or ""),
        "title": folded.get("title", ""),
        "nodes": folded.get("nodes", []),
        "edges": folded.get("edges", []),
        "metrics": folded.get("metrics", {}),
        "alerts": folded.get("alerts", []),
        "updated_at": time.time(),
    }
    if "tool_pipeline" in folded:
        state["tool_pipeline"] = folded["tool_pipeline"]
    return state


def _idle_state() -> dict:
    """空窗期：返回 idle + 保留 last snapshot(带 TTL) + 全节点 idle，
    不是空白、不是误报停机。"""
    global _last_snapshot, _last_snapshot_at
    with _last_lock:
        last = dict(_last_snapshot) if _last_snapshot else None
        fresh = last and (time.time() - _last_snapshot_at) <= _LAST_SNAPSHOT_TTL
    base = last if fresh else {
        "mode": "solo", "job_key": "", "session_id": "", "title": "",
        "nodes": [], "edges": [], "metrics": {}, "alerts": [],
    }
    nodes = []
    for n in (base.get("nodes") or []):
        nn = dict(n)
        nn["state"] = "idle"
        nodes.append(nn)
    return {
        "enabled": True,
        "mode": base.get("mode", "solo"),
        "job_key": base.get("job_key", ""),
        "session_id": base.get("session_id", ""),
        "job_status": "idle",
        "title": base.get("title", ""),
        "nodes": nodes,
        "edges": [],
        "metrics": base.get("metrics", {}),
        "alerts": [],
        "updated_at": time.time(),
        "reason": "no_active_job",
    }


def _pick_job(session_id: str = "") -> Any:
    """遍历 _jobs：只取 running 中 created_at 最新；无 running 返回 None（→ idle）。

    2026-11-10 修正：原实现 `running or jobs` 会在无 running 时退回终态 done job，
    把历史结果冒充「实时状态」，是大屏「假数据」根因之一。改为严格只认 running。
    """
    from chat_jobs import chat_jobs

    with chat_jobs._lock:
        jobs = list(chat_jobs._jobs.values())
    if session_id:
        jobs = [j for j in jobs if str(getattr(j, "session_id", "")) == session_id]
    running = [j for j in jobs if str(getattr(j, "status", "")) == "running"]
    if not running:
        return None
    running.sort(key=lambda j: float(getattr(j, "created_at", 0) or 0), reverse=True)
    return running[0]


@router.get("/viz/state")
async def viz_state(request: Request):
    """GET /api/v2/viz/state —— 全模式统一大屏状态快照。"""
    # flag 关闭：返回禁用态，不鉴权、不读 job、不报错
    if not VIZ_ENABLED:
        return JSONResponse({"enabled": False, "reason": "viz_disabled",
                             "msg": "大屏聚合点未启用（VIZ_ENABLED=False）"})

    if not _auth_ok(request):
        return JSONResponse({"enabled": True, "error": "unauthorized",
                             "msg": "需要 admin 或 service 身份"}, status_code=403)

    session_id = str(request.query_params.get("session_id") or "").strip()
    try:
        job = _pick_job(session_id)
        if job is None:
            return JSONResponse(_idle_state())
        state = _build_state(job)
        # 仅 running 时缓存为 last snapshot，供空窗期回填
        if state.get("job_status") == "running":
            global _last_snapshot, _last_snapshot_at
            with _last_lock:
                _last_snapshot = dict(state)
                _last_snapshot_at = time.time()
        return JSONResponse(state)
    except Exception as exc:  # noqa: BLE001 - 聚合失败降级为空闲态，绝不 500 拖垮路由
        logger.warning("[viz] 聚合失败: %s", exc, exc_info=True)
        return JSONResponse(_idle_state())
