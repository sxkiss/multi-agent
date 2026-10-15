"""
@input: json, os, threading, time, typing, uuid
@output: HitlApprovalStore — 审批闭环（P7，对齐 Octop gateway/hitl）
@position: Security layer — 高风险工具审批的 pending 队列 + TTL + 批准/拒绝
@auto-doc: Update header and folder INDEX.md when this file changes

P7（2026-10-15，boss「修建议的全部」）：对齐 Octop `gateway/hitl/coordinator.py`
+ `store.py` 的轻量版。

bt 此前只有「会话级档位开关」（set_session_hitl），没有**审批闭环**——
高风险工具被拦截后无法「暂停-等待-批准-继续」。本模块提供：

- 创建审批请求（pending）：含 session/tool/args/摘要/过期时间
- 查询 pending（按会话）
- 批准/拒绝：更新状态
- TTL 自动过期
- 持久化：落盘 hitl_approvals.json（重启不丢）

设计取舍：不做完整 job 暂停-恢复（那需要改造 chat 主循环）；先提供
「pending 记录 + 批准后放行」的机制，供 web_server/工具层查询。
"""
import json
import logging
import os
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TTL = 300  # 秒


class HitlApprovalStore:
    """审批请求存储（线程安全 + 落盘）。"""

    def __init__(self, base_dir: str, ttl: int = DEFAULT_TTL):
        self.base_dir = base_dir
        self.ttl = int(ttl or DEFAULT_TTL)
        self.path = os.path.join(base_dir, "hitl_approvals.json")
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if os.path.isfile(self.path):
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._pending = data
        except Exception as e:
            logger.warning("加载审批记录失败: %s", e)

    def _save_locked(self) -> None:
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._pending, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as e:
            logger.warning("保存审批记录失败: %s", e)

    def _purge_expired_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._pending.items()
                   if v.get("status") == "pending" and v.get("expires_at", 0) < now]
        for k in expired:
            self._pending[k]["status"] = "expired"
        if expired:
            self._save_locked()

    def request(self, *, session_id: str, tool: str, args: Any = None,
                summary: str = "", ttl: int | None = None) -> dict[str, Any]:
        """创建审批请求，返回记录（含 id）。"""
        rid = uuid.uuid4().hex[:12]
        now = time.time()
        rec = {
            "id": rid,
            "session_id": session_id,
            "tool": tool,
            "args": args,
            "summary": summary or f"请求执行高风险工具 {tool}",
            "status": "pending",
            "created_at": now,
            "expires_at": now + int(ttl or self.ttl),
        }
        with self._lock:
            self._pending[rid] = rec
            self._save_locked()
        logger.info("[HITL] 审批请求 %s: %s (%s)", rid, tool, session_id)
        return rec

    def get(self, rid: str) -> dict[str, Any] | None:
        with self._lock:
            self._purge_expired_locked()
            return dict(self._pending[rid]) if rid in self._pending else None

    def list_pending(self, session_id: str = "") -> list[dict[str, Any]]:
        with self._lock:
            self._purge_expired_locked()
            out = [dict(v) for v in self._pending.values()
                   if v.get("status") == "pending"
                   and (not session_id or v.get("session_id") == session_id)]
        out.sort(key=lambda r: r.get("created_at", 0))
        return out

    def decide(self, rid: str, approve: bool) -> dict[str, Any] | None:
        """批准/拒绝审批请求。返回更新后的记录。"""
        with self._lock:
            self._purge_expired_locked()
            rec = self._pending.get(rid)
            if not rec:
                return None
            if rec.get("status") != "pending":
                return dict(rec)
            rec["status"] = "approved" if approve else "rejected"
            rec["decided_at"] = time.time()
            self._save_locked()
            return dict(rec)

    def is_approved(self, rid: str) -> bool:
        with self._lock:
            self._purge_expired_locked()
            rec = self._pending.get(rid)
            return bool(rec and rec.get("status") == "approved")


_stores: dict[str, HitlApprovalStore] = {}
_stores_lock = threading.Lock()


def get_hitl_store(base_dir: str) -> HitlApprovalStore:
    with _stores_lock:
        if base_dir not in _stores:
            _stores[base_dir] = HitlApprovalStore(base_dir)
        return _stores[base_dir]
