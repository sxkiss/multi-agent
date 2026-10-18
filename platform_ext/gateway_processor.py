"""多平台 IM 通道 → BT 对话桥接（MessageProcessor）。

把通道收到的 InboundMessage 桥接到 BT 现有 agent 对话体系：

- 用 channel_subject.subject_id 生成稳定 session_id（gw_<channel_type>_<subject>）；
- 连续对话：若该会话已有任务在运行，先取消，再发起新消息（避免任务排队堆积）；
- 每通道模板：读取该通道配置的 template，发起对话时传给 chat_start；
- 异步迭代 agent_main.chat_events 读取流式回复；
- 把文本增量 yield 成 MessageEvent.delta，结束时 completed()。

这样通道消息能真正进入 BT 的对话/记忆/工具体系，实现"对话绑定"。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator

logger = logging.getLogger("platform-ext-gateway-processor")

# 合法 session 字符（与 web_server._validate_session_id 对齐）
_SESSION_SAFE = re.compile(r"[^A-Za-z0-9_.:@-]")


def _safe_session_component(value: str) -> str:
    """清洗 subject_id 等，得到仅含安全字符的会话分量。"""
    cleaned = _SESSION_SAFE.sub("_", str(value or "anon"))
    cleaned = cleaned.strip("._")
    return cleaned[:64] or "anon"


def session_id_for(channel_type: str, subject_id: str) -> str:
    """生成稳定的 BT 会话 ID：gw_<channel_type>_<subject>。"""
    return f"gw_{_safe_session_component(channel_type)}_{_safe_session_component(subject_id)}"


def _extract_text(message) -> str:
    """从 InboundMessage 提取纯文本。"""
    try:
        return message.text or ""
    except Exception:  # noqa: BLE001
        try:
            parts = getattr(message, "content", []) or []
            return "\n".join(
                getattr(c, "text", "") for c in parts if getattr(c, "text", "")
            )
        except Exception:  # noqa: BLE001
            return ""


def _parse_sse_data(data_payload: str) -> Any:
    """解析 chat_events SSE 的 data 行（JSON 字符串）。

    BT 的 sse_pack 对字符串 data 直接输出裸文本（``data: <text>``，不 JSON 化），
    对非字符串 data 才输出 JSON（``data: <json>``）。这里只做纯 JSON 解析：

    - JSON 可解析 → 返回解析后的值（dict / list / int / str）
    - 不可解析（裸文本 / 空）→ 返回 ``None``，由调用方按裸文本处理
    """
    if not data_payload:
        return None
    try:
        import json

        return json.loads(data_payload)
    except Exception:  # noqa: BLE001
        return None


def _extract_sse_data(sse) -> dict[str, Any]:
    """从 SSE 文本中提取 event/data 并解析为 dict（供 processor 读 event/data）。

    兼容三种输入：
    - str（SSE 文本）：同时解析 ``event:`` 与 ``data:`` 行；
    - dict（某些封装直接返回事件对象）：原样返回；
    - data 行是纯数字/列表（json.loads 得到非 dict）→ 防御性包装成 message 事件，
      避免 ``data_payload.get("event")`` 在 int 上崩溃（此前报 'int' object has no attribute 'get'）。
    """
    if isinstance(sse, dict):
        return sse
    event = ""
    data_line = ""
    if isinstance(sse, str):
        for line in sse.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_line = line[5:].strip()
    parsed = _parse_sse_data(data_line)
    if parsed is None:
        # 裸文本：BT sse_pack 对字符串 data 不做 JSON 化，直接作为消息内容
        parsed = {"content": data_line}
    elif not isinstance(parsed, dict):
        # data 行是纯数字/列表（如序列号等），防御性包装成消息内容
        parsed = {"content": str(parsed)}
    return {"event": event, "data": parsed}


def _cancel_running_tasks(web_server, sid: str) -> None:
    """取消指定会话所有运行中的任务（连续对话：先取消再发新消息）。

    通过 chat_jobs 精确判断是否有 running 任务，避免误伤已完成任务。
    """
    try:
        active = web_server.chat_jobs.active_for_session(sid)
        running = [(k, j) for k, j in active if j.is_running]
        if not running:
            return
        logger.info("[gw-proc] 会话 %s 有 %d 个运行中任务，先取消", sid, len(running))
        # chat_stop 会 request_stop + 置 stopped + close agent
        web_server.agent_main.chat_stop({"session_id": sid})
    except Exception as e:  # noqa: BLE001
        logger.warning("[gw-proc] 取消运行中任务失败（忽略）: %s", e)


def _wait_tasks_stopped(web_server, sid: str, timeout: float = 5.0) -> None:
    """等待会话内所有任务退出运行态，避免新旧任务并发。"""
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            active = web_server.chat_jobs.active_for_session(sid)
            if not any(j.is_running for _, j in active):
                return
            asyncio.sleep(0.15)
    except Exception as e:  # noqa: BLE001
        logger.warning("[gw-proc] 等待任务停止超时/失败: %s", e)


def _channel_template(gateway, channel_type: str) -> str:
    """读取该通道配置的 template（可选）。"""
    try:
        cfg = gateway._gateway_cfg()
        return str((cfg.get(channel_type, {}) or {}).get("template", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _channel_mode(gateway, channel_type: str, default: str = "single") -> str:
    """读取该通道配置的对话模式（single/group/claude 等，可选）。

    通道对话默认走 BT 单 Agent（single）；如需集团编排或 Claude CLI，
    可在通道配置里设 mode 字段（面板保存配置时写入）。
    """
    try:
        cfg = gateway._gateway_cfg()
        val = str((cfg.get(channel_type, {}) or {}).get("mode", "") or "").strip()
        if val:
            return val
    except Exception:  # noqa: BLE001
        pass
    return default


def _channel_workspace(gateway, channel_type: str) -> str:
    """读取该通道配置的默认工作目录（可选）。

    面板可在每通道配置里设 workspace 字段；发起对话时传给 chat_start，
    让该通道的 agent 以指定目录为默认 cwd。留空则回退 BT 全局默认。
    """
    try:
        cfg = gateway._gateway_cfg()
        return str((cfg.get(channel_type, {}) or {}).get("workspace", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def make_processor(agent_main=None, chat_jobs=None, gateway=None):
    """构造 MessageProcessor（async generator）。

    可显式注入依赖（便于测试）；默认懒加载 web_server 的对应对象。
    """

    def _get_agent_main():
        if agent_main is not None:
            return agent_main
        try:
            import web_server

            return web_server.agent_main
        except Exception:  # noqa: BLE001
            return None

    def _get_chat_jobs():
        if chat_jobs is not None:
            return chat_jobs
        try:
            import web_server

            return web_server.chat_jobs
        except Exception:  # noqa: BLE001
            return None

    def _get_gateway():
        if gateway is not None:
            return gateway
        try:
            from platform_ext import gateway as gw

            return gw
        except Exception:  # noqa: BLE001
            return None

    async def processor(message) -> AsyncIterator[Any]:
        from octop_gateway.models import MessageEvent

        text = _extract_text(message)
        if not text.strip():
            return

        channel_type = str(getattr(message, "channel_type", "") or "unknown")
        subject = getattr(message, "channel_subject", None)
        subject_id = getattr(subject, "subject_id", None) or "anon"
        sid = session_id_for(channel_type, subject_id)

        ag = _get_agent_main()
        if ag is None:
            yield MessageEvent.error_event("BT agent_main 不可用")
            return
        ws = _get_chat_jobs()

        # 连续对话：先取消该会话运行中的任务，等其退出，再发新消息
        if ws is not None:
            _cancel_running_tasks_with(ws, ag, sid)
            _wait_tasks_stopped(ws, sid)

        # 每通道模板/模式/工作目录：从通道配置读取（可选）
        tpl = ""
        mode = "single"
        workspace = ""
        gw = _get_gateway()
        if gw is not None:
            tpl = _channel_template(gw, channel_type)
            mode = _channel_mode(gw, channel_type, default="single")
            workspace = _channel_workspace(gw, channel_type)

        # 启动后台对话任务（复用 BT 现有 agent 体系）
        start_params: dict[str, Any] = {
            "session_id": sid,
            "message": text,
            "mode": mode,
        }
        if tpl:
            start_params["template"] = tpl
        if workspace:
            start_params["workspace"] = workspace
        try:
            start_res = ag.chat_start(start_params)
        except Exception as e:  # noqa: BLE001
            logger.exception("[gw-proc] chat_start 失败")
            yield MessageEvent.error_event(f"启动对话失败: {e}")
            return

        # 校验启动是否成功
        if isinstance(start_res, dict) and not start_res.get("status", True):
            yield MessageEvent.error_event(str(start_res.get("msg") or "启动对话失败"))
            return

        # 订阅事件流，把文本增量转发为 MessageEvent.delta
        yield MessageEvent.typing()
        try:
            async for sse in ag.chat_events({"session_id": sid}):
                data_payload = _extract_sse_data(sse)
                event = data_payload.get("event")
                data = data_payload.get("data")
                if event == "message" and isinstance(data, dict):
                    content = str(data.get("content") or data.get("text") or "")
                    if content:
                        yield MessageEvent.delta(content)
                elif event == "error":
                    msg = ""
                    if isinstance(data, dict):
                        msg = str(data.get("msg") or data.get("content") or "")
                    elif data is not None:
                        msg = str(data)
                    if msg:
                        yield MessageEvent.error_event(msg)
                    else:
                        yield MessageEvent.completed()
                    return
                elif event == "message_end":
                    yield MessageEvent.completed()
                    return
        except Exception as e:  # noqa: BLE001
            logger.exception("[gw-proc] chat_events 流中断")
            yield MessageEvent.error_event(f"对话流中断: {e}")

    return processor


def _cancel_running_tasks_with(ws, ag, sid: str) -> None:
    """取消指定会话所有运行中任务（连续对话：先取消再发新消息）。"""
    try:
        active = ws.active_for_session(sid)
        running = [(k, j) for k, j in active if j.is_running]
        if not running:
            return
        logger.info("[gw-proc] 会话 %s 有 %d 个运行中任务，先取消", sid, len(running))
        ag.chat_stop({"session_id": sid})
    except Exception as e:  # noqa: BLE001
        logger.warning("[gw-proc] 取消运行中任务失败（忽略）: %s", e)


def _wait_tasks_stopped(ws, sid: str, timeout: float = 5.0) -> None:
    """等待会话内所有任务退出运行态，避免新旧任务并发。"""
    try:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            active = ws.active_for_session(sid)
            if not any(j.is_running for _, j in active):
                return
            asyncio.sleep(0.15)
    except Exception as e:  # noqa: BLE001
        logger.warning("[gw-proc] 等待任务停止超时/失败: %s", e)