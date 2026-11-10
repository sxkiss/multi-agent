"""标记 agent.chat() 产出的"致命错误"事件（不终止流式生成器）。

背景（2026-11-09 排查）：agent.py 外层兜底 except 把异常 yield 成 error 事件
而非 raise，生成器"正常"跑完 → 上层 _run_chat_job 照常 message_end + finish("done")，
导致"带 error 事件但状态 done"的伪成功轮，上层判定本轮正常结束、不再补救。

修复思路：不在 agent 层 raise（会破坏既有流式契约与取消语义），而是给 error
事件打一个进程内标记，让消费方 _run_chat_job 能识别出"本轮其实失败"。
标记放在事件 dict 的瞬态键上，不写入 SSE / 不落盘（_map_agent_chunk 只取
type/data，job.append 只吃映射后的 ev/data），映射层天然丢弃该键，无需额外清理。
"""
from __future__ import annotations

_MARK = "__fatal_agent_error__"


def mark_fatal(event: dict) -> dict:
    """标记该 error 事件为致命本轮错误，返回同一 dict 便于 yield。"""
    event[_MARK] = True
    return event


def is_fatal(event) -> bool:
    return isinstance(event, dict) and bool(event.get(_MARK))