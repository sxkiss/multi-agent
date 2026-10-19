"""platform_ext 段配置的独立文件读写封装。

目标：把 gateway / connector_hub 等容易频繁写入的段从巨型 config.json 中拆出，
降低「一行写崩全清」的风险。底层完全复用 config_store 的原子写 + fcntl 锁。

约定：
- 每个段对应一个独立 JSON 文件（gateway -> gateway.json，connector_hub -> connector_hub.json）；
- 首次读取时若独立文件不存在但 config.json 中对应段存在，自动迁移到独立文件，
  并在 config.json 原段位置写入 {"_migrated_to": "<file>"} 标记，防止重复迁移；
- 迁移是幂等的：独立文件已存在则不再从 config.json 搬；config.json 已标记则不重复写。
"""

from __future__ import annotations

import os
from typing import Any

from config_store import load_config, save_config, update_config
from platform_ext import PROJECT_ROOT

# 段名 -> 独立文件名（位于项目根目录）
# 目前仅接入 gateway / connector_hub；teams/storage/usage 等如需拆分，
# 在此登记后再把对应模块改为 load_section/save_section 即可。
_SECTION_FILES: dict[str, str] = {
    "gateway": "gateway.json",
    "connector_hub": "connector_hub.json",
}

# 迁移标记：写入 config.json 原段位置，表示该段已迁到独立文件
_MIGRATED_MARKER = "_migrated_to"


def _config_path(config_path: str | None) -> str:
    return config_path or os.path.join(PROJECT_ROOT, "config.json")


def section_file(name: str, config_path: str | None = None) -> str:
    """返回段对应的独立文件绝对路径（与 config.json 同目录）。"""
    filename = _SECTION_FILES.get(name)
    if not filename:
        raise KeyError(f"未注册的配置段: {name}")
    base = os.path.dirname(os.path.abspath(_config_path(config_path)))
    return os.path.join(base, filename)


def load_section(name: str, config_path: str | None = None) -> dict[str, Any]:
    """读取独立文件中的段配置；不存在且 config.json 有对应段时自动迁移。"""
    path = section_file(name, config_path)
    data = load_config(path)
    if data:
        return data

    _migrate_from_config(name, path, _config_path(config_path))
    return load_config(path)


def save_section(name: str, data: dict[str, Any], config_path: str | None = None) -> None:
    """加锁原子写独立文件（复用 config_store.update_config）。"""
    path = section_file(name, config_path)

    def _apply(cur: dict[str, Any]) -> None:
        cur.clear()
        cur.update(data)

    update_config(path, _apply)


def _migrate_from_config(name: str, path: str, cfg_path: str) -> None:
    """首读迁移：config.json 中的对应段搬到独立文件。

    幂等规则：
    - 独立文件已存在且有内容 -> 不迁移；
    - config.json 中对应段不存在或已是迁移标记 -> 不迁移；
    - 迁移成功后把原段替换为 {"_migrated_to": "<filename>"} 标记。
    """
    cfg = load_config(cfg_path)

    # 段在 config.json 中的位置：platform_ext.<name>，connector_hub 例外在顶层
    parent = cfg.get("platform_ext") if name != "connector_hub" else cfg
    if not isinstance(parent, dict):
        return
    section = parent.get(name)
    if not isinstance(section, dict):
        return
    if _MIGRATED_MARKER in section:
        return

    # 原子写独立文件，成功后再标记 config.json（两步都复用 config_store）
    save_config(path, section)
    def _mark(full: dict[str, Any]) -> None:
        if name == "connector_hub":
            full["connector_hub"] = {_MIGRATED_MARKER: os.path.basename(path)}
        else:
            full.setdefault("platform_ext", {})[name] = {
                _MIGRATED_MARKER: os.path.basename(path)
            }
    update_config(cfg_path, _mark)
