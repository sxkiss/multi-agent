"""可插拔工作区后端。

BT 现有会话/文件直接写本地文件系统（sessions/ + workspace/）。
本模块引入一个轻量后端抽象：默认「本地」后端，行为与现状一致；
同时提供「文件系统(自定义根)」与「Docker 沙箱」两个额外后端类型，
用于隔离 Agent 文件访问。

不改变 BT 现有默认行为：未配置任何后端时，读写全部落到默认本地根目录。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from platform_ext import PROJECT_ROOT

CONFIG_FILE = os.path.join(PROJECT_ROOT, "config.json")

KIND_LOCAL = "local"
KIND_FILESYSTEM = "filesystem"
KIND_DOCKER = "docker"

SUPPORTED_KINDS = {KIND_LOCAL, KIND_FILESYSTEM, KIND_DOCKER}

_DEFAULT_ROOT = os.path.join(PROJECT_ROOT, "workspace")


# ── 后端注册表 ─────────────────────────────────────────────
@dataclass
class StorageBackend:
    kind: str
    name: str
    root: str | None = None      # filesystem: 自定义根
    docker_image: str | None = None  # docker: 镜像
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)

    def resolve(self, rel_path: str) -> str:
        """把相对路径解析为绝对路径。实现各后端策略。"""
        rel = rel_path.strip("/")
        if self.kind == KIND_DOCKER:
            # Docker 后端：路径仍以本地映射目录为锚（容器内 /workspace 映射到该目录）
            base = self._local_mount_root()
        else:
            base = self.root or _DEFAULT_ROOT
        return os.path.realpath(os.path.join(base, rel))

    def _local_mount_root(self) -> str:
        return os.path.join(_DEFAULT_ROOT, "__docker__", self.name)


def _load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return {}


def _write_config(cfg: dict[str, Any]) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)


def _backends_section() -> dict[str, Any]:
    cfg = _load_config()
    pe = cfg.setdefault("platform_ext", {})
    st = pe.setdefault("storage", {})
    return st


def _read_backends() -> list[dict[str, Any]]:
    return _backends_section().get("backends", [])


def _save_backends(backends: list[dict[str, Any]]) -> None:
    cfg = _load_config()
    cfg.setdefault("platform_ext", {}).setdefault("storage", {})["backends"] = backends
    _write_config(cfg)


# ── CRUD ────────────────────────────────────────────────────
def list_backends() -> list[dict[str, Any]]:
    return _read_backends()


def get_backend(name: str) -> dict[str, Any] | None:
    for b in _read_backends():
        if b["name"] == name:
            return b
    return None


def create_backend(kind: str, name: str, *, root: str | None = None,
                   docker_image: str | None = None, enabled: bool = True) -> dict[str, Any]:
    if kind not in SUPPORTED_KINDS:
        raise ValueError(f"unsupported backend kind: {kind} (supported: {sorted(SUPPORTED_KINDS)})")
    name = str(name or "").strip()
    if not name:
        raise ValueError("backend name is required")
    backends = _read_backends()
    if any(b["name"] == name for b in backends):
        raise ValueError(f"backend already exists: {name}")
    backend = {
        "kind": kind, "name": name, "enabled": enabled,
        "root": root, "docker_image": docker_image,
    }
    backends.append(backend)
    _save_backends(backends)
    return backend


def update_backend(name: str, *, enabled: bool | None = None, root: str | None = None,
                   docker_image: str | None = None) -> dict[str, Any] | None:
    backends = _read_backends()
    for b in backends:
        if b["name"] == name:
            if enabled is not None:
                b["enabled"] = enabled
            if root is not None:
                b["root"] = root
            if docker_image is not None:
                b["docker_image"] = docker_image
            _save_backends(backends)
            return b
    return None


def delete_backend(name: str) -> bool:
    backends = _read_backends()
    new = [b for b in backends if b["name"] != name]
    if len(new) == len(backends):
        return False
    _save_backends(new)
    return True


# ── 文件访问（经活动后端）──────────────────────────────────
def _active_backend() -> StorageBackend:
    backends = _read_backends()
    for b in backends:
        if b.get("enabled"):
            return StorageBackend(**b)
    return StorageBackend(kind=KIND_LOCAL, name="default")


def resolve_path(rel_path: str) -> str:
    return _active_backend().resolve(rel_path)


def safe_resolve(rel_path: str) -> str | None:
    """安全解析：防止路径穿越。返回绝对路径或 None。"""
    rel = str(rel_path or "").strip()
    if not rel or ".." in rel.split("/") or "\x00" in rel:
        return None
    base = os.path.realpath(_active_backend().resolve(""))
    cand = os.path.realpath(os.path.join(base, rel))
    if not cand.startswith(base + os.sep) and cand != base:
        return None
    return cand


def read_text(rel_path: str) -> str | None:
    p = safe_resolve(rel_path)
    if p and os.path.isfile(p):
        with open(p, "r", encoding="utf-8") as fh:
            return fh.read()
    return None


def write_text(rel_path: str, content: str) -> bool:
    p = safe_resolve(rel_path)
    if not p:
        return False
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


def list_dir(rel_path: str) -> list[dict[str, Any]]:
    p = safe_resolve(rel_path)
    if not p or not os.path.isdir(p):
        return []
    out = []
    for name in sorted(os.listdir(p)):
        full = os.path.join(p, name)
        out.append({"name": name, "is_dir": os.path.isdir(full), "size": os.path.getsize(full) if os.path.isfile(full) else 0})
    return out


def probe_backend(name: str) -> dict[str, Any]:
    b = get_backend(name)
    if not b:
        return {"ok": False, "error": "backend not found"}
    try:
        be = StorageBackend(**b)
        base = be.resolve("")
        os.makedirs(base, exist_ok=True)
        if be.kind == KIND_DOCKER:
            # 探测 docker 可用性
            r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                               capture_output=True, text=True, timeout=15, check=False)
            if r.returncode != 0:
                return {"ok": False, "error": f"docker 不可用: {(r.stderr or r.stdout).strip()[:300]}"}
            return {"ok": True, "docker_version": r.stdout.strip(), "mount_root": be._local_mount_root()}
        return {"ok": True, "root": base, "writable": os.access(base, os.W_OK)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}