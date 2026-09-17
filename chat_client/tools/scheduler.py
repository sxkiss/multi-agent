"""
智能体定时任务系统：
- Boss 或 经理 可通过工具创建定时目标（如“每天 9 点检查磁盘并汇报”）
- 到点由后台调度线程自动以独立会话启动集团任务，互不阻塞、可在侧边栏回看
- 持久化 scheduled_tasks.json；支持 interval(每N分钟) 与 daily(每日HH:MM) 两种模式
"""
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from . import PROJECT_ROOT, register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

log = logging.getLogger("scheduler")

TASKS_FILE = os.environ.get(
    "AI_AGENT_SCHEDULE_FILE",
    os.path.join(PROJECT_ROOT, "scheduled_tasks.json"),
)
_LOCK = threading.Lock()
_runner = None          # 由 web_server 注入：callable(task_dict) -> 启动结果
_thread = None


# ---------------- 存储 ----------------

def _load() -> list[dict[str, Any]]:
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(tasks: list[dict[str, Any]]):
    tmp = TASKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TASKS_FILE)


# ---------------- 调度计算 ----------------

def _compute_next(task: dict[str, Any], now: float) -> float:
    """按计划类型计算下一次执行时间戳"""
    st = str(task.get("schedule_type", "")).strip().lower()
    sv = str(task.get("schedule_value", "")).strip()
    if st == "interval":
        minutes = max(5, int(sv))
        return now + minutes * 60
    if st == "daily":
        import re
        m = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", sv)
        if not m:
            raise ValueError(f"daily 时间格式应为 HH:MM，收到 {sv}")
        hh, mm = int(m.group(1)), int(m.group(2))
        dt = datetime.fromtimestamp(now).astimezone().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt.timestamp() <= now:
            dt += timedelta(days=1)
        return dt.timestamp()
    raise ValueError(f"不支持的 schedule_type: {st}")


def _normalize_new(task: dict[str, Any], now: float) -> dict[str, Any]:
    task.setdefault("id", uuid.uuid4().hex[:12])
    task["enabled"] = bool(task.get("enabled", True))
    task["created_at"] = int(now)
    task["last_run"] = 0
    task["running"] = False
    task["next_run"] = _compute_next(task, now)
    return task


# ---------------- 引擎 ----------------

def start_scheduler(runner):
    """注入执行回调（task_dict -> 启动结果）并启动调度线程"""
    global _runner, _thread
    _runner = runner
    if _thread and _thread.is_alive():
        return
    _thread = threading.Thread(target=_loop, name="ai-scheduler", daemon=True)
    _thread.start()
    log.info("[Scheduler] 定时任务调度线程已启动")


def _loop():
    while True:
        try:
            _tick()
        except Exception as e:
            log.warning(f"[Scheduler] tick 异常: {e}")
        time.sleep(15)


def _tick():
    now = time.time()
    due_list = []
    with _LOCK:
        tasks = _load()
        changed = False
        for t in tasks:
            if not t.get("enabled", True) or t.get("running"):
                continue
            try:
                nr = t.get("next_run") or _compute_next(t, now)
            except ValueError as e:
                t["enabled"] = False
                t["last_error"] = str(e)
                changed = True
                continue
            if nr <= now:
                t["running"] = True           # 防重叠：执行期间不再触发
                t["last_run"] = int(now)
                try:
                    t["next_run"] = _compute_next(t, now + 1)
                except ValueError:
                    logger.warning("异常被静默吞掉，已记录", exc_info=True)
                changed = True
                due_list.append(dict(t))
        if changed:
            _save(tasks)
    # 锁外派发
    for t in due_list:
        try:
            res = _runner(t) if _runner else {"status": False, "msg": "runner 未注册"}
            ok = isinstance(res, dict) and res.get("status")
            log.info(f"[Scheduler] 触发定时任务 {t['name']} -> {'OK' if ok else res}")
        finally:
            with _LOCK:
                tasks = _load()
                for x in tasks:
                    if x.get("id") == t["id"]:
                        x["running"] = False
                        break
                _save(tasks)


# ---------------- 核心操作（供工具与 API 复用） ----------------

def op_create(name: str, objective: str, schedule_type: str,
              schedule_value: str, agents=None, model: str = "") -> dict[str, Any]:
    name = str(name).strip()
    if not name:
        return {"status": False, "msg": "缺少任务名称 name"}
    objective = str(objective).strip()
    if not objective:
        return {"status": False, "msg": "缺少目标 objective"}
    st = str(schedule_type).strip().lower()
    if st not in ("interval", "daily"):
        return {"status": False, "msg": "schedule_type 仅支持 interval(每N分钟) / daily(每日HH:MM)"}

    probe = {"schedule_type": st, "schedule_value": str(schedule_value).strip()}
    try:
        nxt = _compute_next(probe, time.time())
    except ValueError as e:
        return {"status": False, "msg": str(e)}

    with _LOCK:
        tasks = _load()
        if any(t.get("name") == name for t in tasks):
            return {"status": False, "msg": f"同名任务已存在: {name}"}
        if len(tasks) >= 100:
            return {"status": False, "msg": "定时任务数量已达上限（100）"}
        t = {
            "id": uuid.uuid4().hex[:12],
            "name": name[:60],
            "objective": objective[:2000],
            "schedule_type": st,
            "schedule_value": str(schedule_value).strip(),
            "agents": [str(a) for a in (agents or [])][:20],
            "model": str(model or "").strip(),
        }
        now = time.time()
        t.update({
            "enabled": True,
            "created_at": int(now),
            "last_run": 0,
            "running": False,
            "next_run": nxt,
        })
        tasks.append(t)
        _save(tasks)

    from_dt = datetime.fromtimestamp(nxt).astimezone().strftime("%m-%d %H:%M")
    return {"status": True, "msg": f"定时任务「{name}」已创建，下次执行：{from_dt}", "id": t["id"]}


def op_list() -> list[dict[str, Any]]:
    with _LOCK:
        tasks = _load()
    out = []
    for t in tasks:
        nxt = t.get("next_run", 0)
        out.append({
            "id": t.get("id"),
            "name": t.get("name"),
            "objective": (t.get("objective") or "")[:80],
            "schedule": f"{t.get('schedule_type')}:{t.get('schedule_value')}",
            "enabled": t.get("enabled", True),
            "running": t.get("running", False),
            "last_run": time.strftime("%m-%d %H:%M", time.localtime(t["last_run"])) if t.get("last_run") else "-",
            "next_run": time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt else "-",
        })
    return out


def op_cancel(name_or_id: str) -> dict[str, Any]:
    key = str(name_or_id).strip()
    with _LOCK:
        tasks = _load()
        remain = [t for t in tasks if t.get("id") != key and t.get("name") != key]
        if len(remain) == len(tasks):
            return {"status": False, "msg": f"未找到任务: {key}"}
        removed = [t for t in tasks if t not in remain]
        _save(remain)
    return {"status": True, "msg": f"已取消定时任务「{removed[0].get('name')}」"}


def op_run_now(name_or_id: str, runner) -> dict[str, Any]:
    key = str(name_or_id).strip()
    with _LOCK:
        tasks = _load()
        target = next((t for t in tasks if t.get("id") == key or t.get("name") == key), None)
        if not target:
            return {"status": False, "msg": f"未找到任务: {key}"}
        if target.get("running"):
            return {"status": False, "msg": "该任务正在执行中"}
        target["last_run"] = int(time.time())
        _save(tasks)
    res = runner(target) if runner else {"status": False, "msg": "runner 未注册"}
    with _LOCK:
        tasks = _load()
        for x in tasks:
            if x.get("id") == target["id"]:
                x["running"] = False
                break
        _save(tasks)
    return {"status": bool(isinstance(res, dict) and res.get("status")),
            "msg": res.get("msg") or ("已触发" if res.get("status") else "触发失败")}


# ---------------- Agent 工具封装 ----------------

@register_tool(category="Agent", name_cn="创建定时任务", risk_level="medium", timeout=30)
def ScheduleTask(name: str, objective: str, schedule_type: str = "daily",
                 schedule_value: str = "09:00", agents: list | None = None, model: str = "") -> str:
    """
    创建定时任务：到点后系统自动以独立会话启动集团执行该目标。
    两种计划类型：
    - interval：每 N 分钟一次，schedule_value 填分钟数（如 "30"）
    - daily：每天固定时刻，schedule_value 填 HH:MM（如 "09:00"）

    Args:
        name: 任务名称（唯一）
        objective: 到点要让集团完成的目标描述
        schedule_type: interval | daily
        schedule_value: 分钟数或 HH:MM
        agents: 可选。限定执行的团队成员名单（留空由经理自行安排）
        model: 可选。指定执行模型（留空用默认模型）
    """
    r = op_create(name, objective, schedule_type, schedule_value, agents, model)
    if r.get("status"):
        return _xml_response("done", f"{r['msg']}\\n目标：{objective}\\n到点后将在后台自动执行，过程与结果可在左侧会话列表（sched_* 开头）中查看。")
    return _xml_response("error", r["msg"])


@register_tool(category="Agent", name_cn="查询定时任务", risk_level="low", timeout=15)
def ListScheduledTasks() -> str:
    """
    列出全部定时任务及其状态（下次执行时间等）。
    """
    rows = op_list()
    if not rows:
        return _xml_response("done", "当前没有任何定时任务")
    lines = ["名称 | 计划 | 状态 | 上次 | 下次 | 目标"]
    for r in rows:
        state = "执行中" if r["running"] else ("启用" if r["enabled"] else "停用")
        lines.append(
            f"{r['name']} | {r['schedule']} | {state} | {r['last_run']} | {r['next_run']} | {r['objective']}"
        )
    return _xml_response("done", "\n".join(lines))


@register_tool(category="Agent", name_cn="取消定时任务", risk_level="medium", timeout=15)
def CancelScheduledTask(name_or_id: str) -> str:
    """
    取消（删除）一个定时任务。可传任务名或任务 ID。

    Args:
        name_or_id: 任务名称或 ID
    """
    r = op_cancel(name_or_id)
    return _xml_response("done" if r["status"] else "error", r["msg"])


@register_tool(category="Agent", name_cn="立即执行定时任务", risk_level="medium", timeout=60)
def RunScheduledTaskNow(name_or_id: str) -> str:
    """
    立即触发某个定时任务（不影响其后续排期）。

    Args:
        name_or_id: 任务名称或 ID
    """
    r = op_run_now(name_or_id, _runner)
    return _xml_response("done" if r["status"] else "error", r["msg"])
