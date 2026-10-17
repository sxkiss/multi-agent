#!/usr/bin/env python3
"""批量导入 mem0 记忆到知识库（2026-10-13）

流程：拉取全部记忆（/memories?limit=1000）→ 逐条 KnowledgeAdd(text=) 入库
命名：mem0-<agent>-<id前8位>
进度：分批打印到 stdout + 写日志文件，支持断点续传（跳过已入库 doc_id）
"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/sxkiss/bt")
import requests
from chat_client.tools.knowledge import _get_kb

LOG = "/home/sxkiss/bt/mem0_import.log"
MEM0_URL = "http://127.0.0.1:8000/memories"


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_agent(m: dict) -> str:
    return str(m.get("agent_id") or m.get("user_id") or "unknown")


def main():
    # 1. 拉取全部记忆
    log("拉取 mem0 全部记忆…")
    r = requests.get(MEM0_URL, params={"limit": 1000}, timeout=15)
    data = r.json()
    mems = data.get("results") or []
    log(f"共 {len(mems)} 条记忆")

    if not mems:
        log("无记忆可导入")
        return

    # 2. 已导入的 doc_id（断点续传）
    kb = _get_kb()
    existing = set()
    try:
        for d in kb.list_documents():
            n = d.get("name") or ""
            if n.startswith("mem0-"):
                existing.add(n)
    except Exception as e:
        log(f"读取已有文档失败（继续）: {e}")

    # 3. 逐条入库
    ok = 0
    fail = 0
    skip = 0
    t0 = time.time()
    for i, m in enumerate(mems, 1):
        mid = str(m.get("id") or "")
        text = str(m.get("memory") or "").strip()
        agent = get_agent(m)
        name = f"mem0-{agent}-{mid[:8]}" if mid else f"mem0-{agent}-{i}"
        if name in existing:
            skip += 1
            continue
        if not text:
            skip += 1
            continue
        try:
            res = kb.add_document(text=text, name=name)
            if res.get("status"):
                ok += 1
                existing.add(name)
            else:
                fail += 1
                log(f"  ✗ 失败 [{i}/{len(mems)}] {name}: {res.get('msg','')[:100]}")
        except Exception as e:
            fail += 1
            log(f"  ✗ 异常 [{i}/{len(mems)}] {name}: {e}")
        if i % 25 == 0:
            dt = time.time() - t0
            log(f"  进度 {i}/{len(mems)} 成功={ok} 失败={fail} 跳过={skip} 耗时={dt:.0f}s")

    dt = time.time() - t0
    log(f"完成：成功={ok} 失败={fail} 跳过={skip} 总耗时={dt:.0f}s")
    log(f"知识库文档总数: {len(kb.list_documents())}")


if __name__ == "__main__":
    main()