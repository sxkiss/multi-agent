"""config.json 的安全读写。

历史问题：多个模块在 JSON 解析失败时静默返回空 dict，随后整文件回写，
把 api_key、platform_ext、connector_hub 等其它配置全部清空。

本模块保证：
- 解析失败抛 ConfigError，绝不把损坏文件当成空配置回写；
- 写入走临时文件 + fsync + os.replace，进程中断不会留下半截 JSON；
- update_config 在同一把文件锁内完成读-改-写，避免并发写互相覆盖。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - 非 POSIX 平台
    fcntl = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    """配置文件存在但无法安全读取。"""


def load_config(path: str) -> dict[str, Any]:
    """读取配置；文件不存在视为空配置，损坏则抛错而不是返回 {}。"""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"读取配置文件失败，已拒绝覆盖: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件根节点必须是 JSON 对象: {path}")
    return data


def save_config(path: str, cfg: dict[str, Any]) -> None:
    """原子写入配置，保留原文件权限与末尾换行。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    trailing_newline = _has_trailing_newline(path)
    try:
        mode: int | None = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        mode = None

    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
            if trailing_newline:
                fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update_config(path: str, mutate: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
    """加锁完成读-改-写，mutate 就地修改传入的配置字典。"""
    with _locked(path):
        cfg = load_config(path)
        mutate(cfg)
        save_config(path, cfg)
        return cfg


def _has_trailing_newline(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) == b"\n"
    except OSError:
        return False


@contextmanager
def _locked(path: str):
    if fcntl is None:
        yield
        return
    lock_path = os.path.abspath(path) + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
