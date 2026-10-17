"""
@input: json, os, shutil, tarfile, time, threading, typing
@output: create_backup / list_backups / restore_backup / auto_backup_loop
@auto-doc: Update header and folder INDEX.md when this file changes

P4-d（2026-10-15）：
bt 此前无备份体系。本模块提供：
- create_backup：把关键数据（config/sessions/crew_agents/skills_state/mcp）打包为 tar.gz
- list_backups / restore_backup：列出与恢复
- auto_backup_loop：后台线程按间隔自动备份（保留最近 N 份）
不备份大文件目录（skills/ 内容、sessions 的 vector_store 可选排除）。
"""
import json
import os
import shutil
import tarfile
import threading
import time
from typing import Any

_BACKUP_LOCK = threading.Lock()
_auto_thread: threading.Thread | None = None
_auto_stop = threading.Event()

# 默认备份的路径（相对 base_dir）
DEFAULT_TARGETS = [
    "config.json",
    "crew_agents.json",
    "scheduled_tasks.json",
    "tools_state.json",
    "skills_state.json",
    "SOUL.md",
    "AGENTS.md",
    "USER.md",
    "MEMORY.md",
    "sessions",
    "mcp",
    "knowledge",
]


def _backup_dir(base_dir: str) -> str:
    d = os.path.join(base_dir, "backups")
    os.makedirs(d, exist_ok=True)
    return d


def create_backup(base_dir: str, *, targets: list[str] | None = None,
                  label: str = "", keep: int = 10) -> dict[str, Any]:
    """创建备份 tar.gz。返回 {status, path, size, files}。"""
    targets = targets or DEFAULT_TARGETS
    bdir = _backup_dir(base_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    suffix = f"_{label}" if label else ""
    name = f"bt-backup{suffix}_{ts}.tar.gz"
    out_path = os.path.join(bdir, name)

    with _BACKUP_LOCK:
        count = 0
        tmp = out_path + ".tmp"
        try:
            with tarfile.open(tmp, "w:gz") as tf:
                for rel in targets:
                    full = os.path.join(base_dir, rel)
                    if not os.path.exists(full):
                        continue
                    # 排除大目录/临时文件
                    def _filter(ti):
                        bn = os.path.basename(ti.name)
                        if bn.endswith(".tmp") or bn.endswith(".pid"):
                            return None
                        return ti
                    tf.add(full, arcname=rel, filter=_filter)
                    count += 1
            os.replace(tmp, out_path)
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            return {"status": False, "msg": f"备份失败: {e!s}"}

        # 清理旧备份
        pruned = _prune(bdir, keep)

    size = os.path.getsize(out_path)
    return {"status": True, "path": out_path, "name": name, "size": size,
            "size_mb": round(size / 1024 / 1024, 2), "entries": count, "pruned": pruned}


def _prune(bdir: str, keep: int) -> list[str]:
    """保留最近 keep 份备份，删除更旧的。返回被删列表。"""
    try:
        files = [f for f in os.listdir(bdir)
                 if f.startswith("bt-backup") and f.endswith(".tar.gz")]
        files.sort(key=lambda f: os.path.getmtime(os.path.join(bdir, f)), reverse=True)
        removed = []
        for f in files[keep:]:
            try:
                os.remove(os.path.join(bdir, f))
                removed.append(f)
            except OSError:
                pass
        return removed
    except Exception:
        return []


def list_backups(base_dir: str) -> dict[str, Any]:
    bdir = _backup_dir(base_dir)
    rows = []
    for f in os.listdir(bdir):
        if f.startswith("bt-backup") and f.endswith(".tar.gz"):
            p = os.path.join(bdir, f)
            st = os.stat(p)
            rows.append({"name": f, "size": st.st_size,
                         "size_mb": round(st.st_size / 1024 / 1024, 2),
                         "mtime": int(st.st_mtime)})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return {"status": True, "backups": rows, "total": len(rows)}


def restore_backup(base_dir: str, name: str, *, dry_run: bool = True) -> dict[str, Any]:
    """恢复备份。默认 dry_run=True 只列出将覆盖的文件，需显式 dry_run=False 才落盘。"""
    bdir = _backup_dir(base_dir)
    path = os.path.join(bdir, os.path.basename(name))
    if not os.path.exists(path):
        return {"status": False, "msg": f"备份不存在: {name}"}
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = [m.name for m in tf.getmembers() if not m.isdir()]
    except Exception as e:
        return {"status": False, "msg": f"读取备份失败: {e!s}"}

    if dry_run:
        return {"status": True, "dry_run": True, "would_restore": members,
                "count": len(members),
                "msg": "预演：以下文件将被覆盖。确认后传 dry_run=False 执行恢复。"}

    # 恢复前先给当前状态打个备份，防误操作
    pre = create_backup(base_dir, label="pre-restore", keep=20)
    try:
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(base_dir)  # 备份由本模块生成，成员路径可信
    except Exception as e:
        return {"status": False, "msg": f"恢复失败: {e!s}", "pre_backup": pre.get("path")}
    return {"status": True, "restored": members, "count": len(members),
            "pre_backup": pre.get("path")}


def start_auto_backup(base_dir: str, *, interval_sec: int = 3600, keep: int = 10) -> dict[str, Any]:
    """启动后台自动备份线程（幂等）。"""
    global _auto_thread
    if _auto_thread and _auto_thread.is_alive():
        return {"status": True, "msg": "自动备份已在运行", "interval_sec": interval_sec}
    _auto_stop.clear()

    def _loop():
        while not _auto_stop.is_set():
            try:
                create_backup(base_dir, label="auto", keep=keep)
            except Exception:
                pass
            _auto_stop.wait(max(60, interval_sec))

    _auto_thread = threading.Thread(target=_loop, name="bt-auto-backup", daemon=True)
    _auto_thread.start()
    return {"status": True, "msg": f"自动备份已启动（每 {interval_sec}s）", "interval_sec": interval_sec}


def stop_auto_backup() -> dict[str, Any]:
    _auto_stop.set()
    return {"status": True, "msg": "自动备份停止信号已发出"}


def auto_backup_status() -> dict[str, Any]:
    running = bool(_auto_thread and _auto_thread.is_alive())
    return {"status": True, "running": running}
