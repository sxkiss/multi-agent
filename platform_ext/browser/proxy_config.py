"""浏览器代理配置管理。

支持运行时动态设置/读取代理，持久化到状态目录下的 proxy.json，
优先级高于环境变量 BROWSER_USE_PROXY（工具自身配置优先，环境变量作为默认）。

  GET  /api/v2/browser/proxy          → 读取当前生效代理配置
  POST /api/v2/browser/proxy          → 设置/清除代理配置
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


def _proxy_config_path() -> Path:
    """代理配置存放路径：~/.octop-browser/proxy.json。"""
    from octop_browser.settings import state_dir  # noqa: PLC0415

    return Path(state_dir()) / "proxy.json"


def load_proxy_config() -> dict[str, Any]:
    """读取持久化代理配置。文件不存在或损坏时返回空配置。"""
    path = _proxy_config_path()
    if not path.exists():
        return {"enabled": False, "proxy": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "enabled": bool(data.get("enabled", False)),
                "proxy": str(data.get("proxy", "") or "").strip(),
            }
    except Exception:  # noqa: BLE001
        pass
    return {"enabled": False, "proxy": ""}


def save_proxy_config(enabled: bool, proxy: str) -> dict[str, Any]:
    """持久化代理配置到磁盘（原子写）。返回规范化后的配置。"""
    proxy = (proxy or "").strip()
    cfg = {"enabled": bool(enabled), "proxy": proxy}
    path = _proxy_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return cfg


def effective_proxy() -> str | None:
    """返回当前生效的代理地址。

    优先使用持久化配置（工具自身配置）；否则回退到环境变量 BROWSER_USE_PROXY。
    launcher 每次启动 Chrome 时调用本函数，保证修改后重启浏览器即生效。
    """
    cfg = load_proxy_config()
    if cfg.get("enabled") and cfg.get("proxy"):
        return cfg["proxy"]
    env = os.environ.get("BROWSER_USE_PROXY", "").strip()
    return env or None


class ProxyBody(BaseModel):
    enabled: bool = True
    proxy: str = ""


@router.get("/browser/proxy")
async def get_proxy() -> dict[str, Any]:
    """读取当前代理配置。"""
    cfg = load_proxy_config()
    env = os.environ.get("BROWSER_USE_PROXY", "").strip()
    return {
        "status": True,
        "data": {
            "configured": cfg,
            "env_proxy": env,
            "effective_proxy": effective_proxy(),
        },
        "msg": "",
    }


@router.post("/browser/proxy")
async def set_proxy(body: ProxyBody) -> dict[str, Any]:
    """设置/清除代理配置。enabled=False 或 proxy 为空表示清除工具自身配置。"""
    proxy = (body.proxy or "").strip()
    enabled = bool(body.enabled) and bool(proxy)
    cfg = save_proxy_config(enabled, proxy)
    return {
        "status": True,
        "data": {
            "configured": cfg,
            "env_proxy": os.environ.get("BROWSER_USE_PROXY", "").strip(),
            "effective_proxy": effective_proxy(),
        },
        "msg": "代理配置已保存，重启浏览器会话后生效",
    }