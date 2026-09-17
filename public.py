"""
Minimal public module compatibility layer for standalone AI Agent.
Provides the subset of API used by agent and chat_client.
"""
import json
import os
import datetime
import platform
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional


# ---------- Response helpers ----------

def return_data(status: bool, data: Any = None, msg: str = "") -> Dict[str, Any]:
    return {"status": status, "data": data, "msg": msg}


def returnMsg(status: bool, msg: str = "") -> Dict[str, Any]:
    return {"status": status, "msg": msg}


def to_dict_obj(d: Dict[str, Any]) -> Dict[str, Any]:
    return d


# ---------- User / Account ----------

def get_user_info() -> Dict[str, Any]:
    return {
        "uid": os.getenv("AI_AGENT_UID", ""),
        "access_key": os.getenv("AI_AGENT_ACCESS_KEY", ""),
        "username": os.getenv("AI_AGENT_USERNAME", ""),
    }


def get_oem_name() -> str:
    return ""


# ---------- System / OS ----------

def get_os_version() -> str:
    try:
        return platform.platform()
    except Exception:
        return "Linux"


def version() -> str:
    return "11.8.1"


# ---------- Date / Time ----------

def format_date(times: Optional[int] = None, format_str: str = "%Y-%m-%d %H:%M:%S") -> str:
    if times is None:
        times = int(time.time())
    try:
        return datetime.datetime.fromtimestamp(times).strftime(format_str)
    except Exception:
        return str(times)


# ---------- Pagination ----------

def get_page(total: int, p: int = 1, count: int = 10, url: str = "") -> Dict[str, Any]:
    p = max(1, p)
    start = (p - 1) * count
    end = min(start + count, total)
    return {
        "page": p,
        "page_size": count,
        "total": total,
        "total_pages": max(1, (total + count - 1) // count),
        "data": [],
    }


# ---------- Database shim ----------

class _Model:
    def __init__(self, table: str):
        self.table = table
        self._fields = "*"

    def field(self, *fields: str) -> "_Model":
        self._fields = ",".join(fields) if fields else "*"
        return self

    def select(self) -> List[Dict[str, Any]]:
        return []

    def where(self, *args, **kwargs) -> "_Model":
        return self


class _M:
    def __call__(self, table: str) -> _Model:
        return _Model(table)


M = _M()


# ---------- Logging ----------

def set_module_logs(*args: Any, **kwargs: Any) -> None:
    pass


def print_log(*args: Any, **kwargs: Any) -> None:
    pass


# ---------- Panel paths ----------

PANEL_PATH = os.getenv("AI_PANEL_PATH", "/www/server/panel")
