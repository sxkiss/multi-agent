"""
@input: json, os, re, threading, yaml
@output: GuardRules / get_guard_rules — 可编辑 YAML 护栏规则库（P7，对齐 Octop tool_guard_rules）
@position: Security layer — 磁盘 YAML 护栏规则，替代硬编码危险命令黑名单
@auto-doc: Update header and folder INDEX.md when this file changes

P7（2026-10-15，boss「修建议的全部」）：对齐 Octop `security/tool_guard_rules.py`。

原实现把危险命令正则**硬编码**在 `tools/__init__.py` 的 `_DANGEROUS_CMD_PATTERNS`，
用户无法调整。本模块改为**磁盘 YAML 规则库**：

- 位置：`security/tool_guard/rules.yaml`（首次运行自动 seed 内置规则）
- 结构：rules 列表，每条 {id, pattern, severity, action, description}
  - action: block（拦截）/ warn（仅记录）
  - severity: critical / high / medium
- 校验：加载时校验 YAML 合法 + 正则可编译，非法条目跳过并告警
- 热加载：文件 mtime 变化自动重载（30s 检查）
- 向后兼容：YAML 不存在时回退到内置默认规则

对外接口：
- get_guard_rules(base_dir) -> GuardRules
- GuardRules.scan(text) -> (blocked: bool, rule: dict | None)
"""
import logging
import os
import re
import threading
import time
from typing import Any

import yaml

logger = logging.getLogger(__name__)

RULES_DIRNAME = os.path.join("security", "tool_guard")
RULES_FILENAME = "rules.yaml"

# 内置默认规则（首次 seed 到磁盘；对齐原 _DANGEROUS_CMD_PATTERNS + 扩充）
DEFAULT_RULES: list[dict[str, Any]] = [
    {"id": "rm-rf-root", "pattern": r"rm\s+(-[a-zA-Z]+\s+)*-[a-zA-Z]*[rf][a-zA-Z]*\s+/(?:\s|$)",
     "severity": "critical", "action": "block", "description": "递归删除根目录"},
    {"id": "rm-rf-nondir", "pattern": r"rm\s+-[a-zA-Z]*r[a-zA-Z]*f?[a-zA-Z]*\s+/[^/\w]",
     "severity": "critical", "action": "block", "description": "递归删除根下非目录"},
    {"id": "mkfs", "pattern": r"mkfs(\.\w+)?\b",
     "severity": "critical", "action": "block", "description": "格式化文件系统"},
    {"id": "dd-blockdev", "pattern": r"\bdd\b[^|;&]*\bof=/dev/(?:sd|vd|nvme|hd)",
     "severity": "critical", "action": "block", "description": "dd 直接写块设备"},
    {"id": "fork-bomb", "pattern": r":\(\)\s*\{.*\}\s*;\s*:",
     "severity": "critical", "action": "block", "description": "fork bomb"},
    {"id": "shutdown", "pattern": r"\b(?:shutdown|halt|poweroff|reboot|init\s+[06])\b",
     "severity": "high", "action": "block", "description": "关机/重启类"},
    {"id": "overwrite-disk", "pattern": r">\s*/dev/sd[a-z]",
     "severity": "critical", "action": "block", "description": "覆盖磁盘设备"},
    {"id": "chmod-777-root", "pattern": r"\bchmod\s+-R\s+777\s+/(?:\s|$)",
     "severity": "high", "action": "block", "description": "递归 777 根目录"},
    {"id": "wipefs", "pattern": r"\bwipefs\b",
     "severity": "high", "action": "block", "description": "擦除文件系统签名"},
    {"id": "history-clear", "pattern": r"\bhistory\s+-c\b",
     "severity": "medium", "action": "warn", "description": "清空命令历史"},
]

_cache: dict[str, Any] = {"ts": 0.0, "rules": None, "mtime": 0.0}
_TTL = 30


def _rules_path(base_dir: str) -> str:
    return os.path.join(base_dir, RULES_DIRNAME, RULES_FILENAME)


def _seed_if_missing(path: str) -> None:
    if os.path.isfile(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {
            "_comment": "可编辑的护栏规则库。action: block(拦截)/warn(仅记录)；"
                        "severity: critical/high/medium。修改后 30s 内自动热加载。",
            "rules": DEFAULT_RULES,
        }
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(doc, f, allow_unicode=True, sort_keys=False)
        logger.info("已 seed 护栏规则库: %s", path)
    except OSError as e:
        logger.warning("seed 护栏规则库失败: %s", e)


class GuardRules:
    """护栏规则库：加载 YAML、校验、扫描。"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.path = _rules_path(base_dir)
        self._lock = threading.Lock()
        self.rules: list[dict[str, Any]] = []
        self._compiled: list[tuple[re.Pattern, dict[str, Any]]] = []
        self._load(force=True)

    def _mtime(self) -> float:
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return 0.0

    def _load(self, force: bool = False) -> None:
        now = time.time()
        mt = self._mtime()
        with self._lock:
            if (not force and self.rules and now - _cache["ts"] < _TTL
                    and _cache["mtime"] == mt):
                return
        _seed_if_missing(self.path)
        rules = self._read_and_validate()
        compiled = []
        for r in rules:
            try:
                compiled.append((re.compile(r["pattern"]), r))
            except re.error as e:
                logger.warning("护栏规则 %s 正则非法，已跳过: %s", r.get("id"), e)
        with self._lock:
            self.rules = rules
            self._compiled = compiled
            _cache["ts"] = now
            _cache["mtime"] = mt

    def _read_and_validate(self) -> list[dict[str, Any]]:
        """读取并校验 YAML；非法时回退默认规则。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("护栏规则库读取失败，回退内置默认: %s", e)
            return list(DEFAULT_RULES)
        raw = doc.get("rules") if isinstance(doc, dict) else None
        if not isinstance(raw, list):
            logger.warning("护栏规则库格式错误（rules 非列表），回退内置默认")
            return list(DEFAULT_RULES)
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            pat = item.get("pattern")
            if not isinstance(pat, str) or not pat.strip():
                continue
            out.append({
                "id": str(item.get("id") or f"rule-{len(out)}"),
                "pattern": pat,
                "severity": str(item.get("severity") or "high"),
                "action": str(item.get("action") or "block"),
                "description": str(item.get("description") or ""),
            })
        return out or list(DEFAULT_RULES)

    def scan(self, text: str) -> tuple[bool, dict[str, Any] | None]:
        """扫描文本，返回 (是否拦截, 命中规则)。warn 级不拦截但记录。"""
        if not text:
            return False, None
        self._load()  # 触发 mtime 热加载
        with self._lock:
            compiled = list(self._compiled)
        for rx, rule in compiled:
            try:
                if rx.search(text):
                    if rule.get("action") == "warn":
                        logger.warning("[GuardRules] 命中警告规则 %s: %s",
                                       rule.get("id"), rule.get("description"))
                        continue
                    return True, rule
            except re.error:
                continue
        return False, None


def get_guard_rules(base_dir: str) -> GuardRules:
    return GuardRules(base_dir)
