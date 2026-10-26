#!/usr/bin/env python3
"""同步 mem0 记忆到 platform_ext/memory_octop（供前端面板展示）。

每 10 分钟由 crontab 触发一次，自动跳过已入库的记忆。
"""
import json
import sys
import time

sys.path.insert(0, "/home/sxkiss/bt")
import requests
from platform_ext.memory_octop import _get_memory

LOG = "/home/sxkiss/bt/mem0_import.log"
MEM0_URL = "http://127.0.0.1:8000/memories"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    m = _get_memory()
    # 取当前已入库的 node content 前 50 字符作为去重 key
    existing = set()
    for n in m.backend.get_tree():
        existing.add(n.content[:60] if n.content else "")

    r = requests.get(MEM0_URL, params={"limit": 1000}, timeout=15)
    mems = r.json().get("results") or []
    log(f"mem0 共 {len(mems)} 条，已有 {len(existing)} 条去重")

    ok = fail = skip = 0
    for mem in mems:
        content = str(mem.get("memory") or "").strip()
        if not content:
            skip += 1
            continue
        if content[:60] in existing:
            skip += 1
            continue
        agent = str(mem.get("agent_id") or mem.get("user_id") or "unknown")
        try:
            m.store(content=content, topic=agent, level="leaf")
            ok += 1
            existing.add(content[:60])
        except Exception as e:
            fail += 1
            log(f"  ✗ {mem.get('id','?')}: {e}")

    log(f"完成：写入={ok} 跳过={skip} 失败={fail}")


if __name__ == "__main__":
    main()
