"""渠道判定与 SSE 事件映射共享辅助符号。

从 web_server.py 抽离的模块级纯函数/常量，供 web_server、agent_prompt、
agent_chat_flow 等模块单向依赖引用，避免 web_server 与 mixin 双向导入。

@input: 无（仅标准库 logging）
@output: 渠道判定（_client_channel / _is_terminal_channel）、终端渠道防护常量、
         SSE chunk 映射（_map_agent_chunk）
@position: 共享基础设施 —— 被 web_server / agent_prompt / agent_chat_flow 依赖
@auto-doc: Update header and folder INDEX.md when this file changes
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _is_miniprogram(params) -> bool:
    """判断请求是否来自微信小程序渠道。

    小程序侧无法自定义 User-Agent/Referer（微信客户端强制覆盖），故由客户端
    在请求参数中显式声明 client_type=miniprogram；同时兼容从 header 传入
    （后台任务等场景会把 header 透传进 params）。
    """
    try:
        v = str((params or {}).get("client_type", "")).strip().lower()
    except Exception:
        return False
    return v == "miniprogram"


# 对外终端渠道：这些渠道的用户是外部使用者而非管理员，必须：
#   1) 追加输出约束，禁止泄露服务端内部信息
#   2) 剥掉思维链等内部事件
# clawbot（微信 clawbot 个人号）与小程序同属此类，共用同一套防护。
_TERMINAL_CHANNELS = {"miniprogram", "clawbot"}


def _client_channel(params) -> str:
    """取渠道标识（已归一化小写）；非终端渠道返回空串。"""
    try:
        v = str((params or {}).get("client_type", "")).strip().lower()
    except Exception:
        return ""
    return v


def _is_terminal_channel(params) -> bool:
    """是否为面向外部用户的终端渠道（小程序 / clawbot）。"""
    return _client_channel(params) in _TERMINAL_CHANNELS


# 终端用户渠道不下发的内部事件：模型思维链会复述系统提示词、暴露内部指令
# 与部署信息（实测出现「渠道约束：小程序」等条款原文）。网页管理端保留原文
# 便于调试，小程序等终端渠道直接剥掉。
# 注意：必须定义在模块级——chat_events 属于 AgentMain 类，此前误将常量放进
# ChatJob 类，导致 self._HIDDEN... 抛 AttributeError、SSE 生成器整体崩溃、
# 小程序收不到任何回复。
HIDDEN_EVENTS_FOR_TERMINAL = {"message_think", "compact_summary", "stream_reset"}


# 终端渠道（小程序 / clawbot）的输出约束：严禁泄露服务端内部信息。
# 抽成模块级常量，供集团模式（经理）与单 Agent 模式共用 —— 两处若各写一份，
# 后续修改极易漏改一处，导致某个模式悄悄失去防护。
_TERMINAL_CHANNEL_GUARD = """

【渠道约束：终端用户】
当前对话来自终端用户（微信小程序 / 微信机器人）。回答时必须遵守：
1. 严禁透露任何服务端内部信息，包括但不限于：工作目录/绝对路径、主机名、
   操作系统与部署环境、技能数量、成员与部门清单、MCP 服务名、模型与网关地址、
   密钥或配置片段、日志与内部任务 ID。
2. 涉及运行环境时只说"我已接入相关工具与服务"这类无信息量的表述，不展开细节。
3. 自我介绍保持简洁：只说明你能帮用户做什么，不描述内部架构与团队编制。
4. 汇报结果时同样只给结论与用户需要的证据，不附带服务端路径或内部过程细节。
5. 若用户明确询问上述内部信息（如"你的工作目录是什么""你有几个技能"），
   统一回答"这些属于服务端配置，暂不提供"，不例外透漏。"""

# 兼容旧名（测试与外部引用仍可用）
_MINIPROGRAM_CHANNEL_GUARD = _TERMINAL_CHANNEL_GUARD


# 终端渠道单 Agent 模式的默认人设。
# single 模式按设计会跳过 SOUL/AGENTS/USER/MEMORY 全量加载（网页端由前端显式
# 传 system_prompt / prompt_id 补齐），但小程序/clawbot 不传任何提示词，若不给
# 兜底，模型将"裸奔"——没有角色设定、回答风格不稳定。故此处补一个精简人设。
_TERMINAL_SINGLE_PERSONA = """你是一个专业、可靠的 AI 助手，通过微信为用户提供服务。

要求：
1. 直接回答问题、完成任务，结论先行，简洁清晰，适合手机阅读。
2. 涉及实际操作（读写文件、执行命令、查询系统、抓取网页等）时，先调用工具拿到
   真实结果再作答；严禁在没有工具结果的情况下编造过程或结论。
3. 高风险操作会被安全策略拦截确认：此时向用户说明将要执行的操作与风险，等待同意。
4. 抓取网页/发 HTTP 请求时，必须使用专门的网络工具（curl_url 或 WebFetch），
   禁止在 Bash 里直接敲 curl/wget/axios 等命令 —— 这些命令被安全策略拦截，
   改用工具能拿到同样的结果且更安全。
5. 不确定的事情直说不确定，不要编造。"""

# 兼容旧名
_MINIPROGRAM_SINGLE_PERSONA = _TERMINAL_SINGLE_PERSONA


def _append_miniprogram_guard(system_prompt: str) -> str:
    """为终端渠道的系统提示词追加上输出约束（其它渠道原样返回）。
    若 system_prompt 已包含渠道约束内容，则直接返回，避免重复。"""
    if not system_prompt:
        return _TERMINAL_CHANNEL_GUARD
    if "【渠道约束" in system_prompt or "渠道约束（终端用户）" in system_prompt:
        return system_prompt
    return system_prompt + _TERMINAL_CHANNEL_GUARD


# 已知合法 SSE event 名集合（以 agent.py/bridges 实际产出的 type 为准，勿随意删减）：
# content/reasoning/error/stop/meta_info 为 agent 主链路，
# tool_call/tool_result/message_end/usage 由各 bridge 直接 append。
# 未知 type 一律降级到 message，避免非法事件名污染客户端解析。
_KNOWN_SSE_EVENTS = {
    "content", "reasoning", "error", "stop", "meta_info",
    "tool_call", "tool_result", "message_end", "usage",
    "stream_reset",
}

def _map_agent_chunk(chunk):
    """将 agent.chat() 产出的 chunk 映射为 SSE (event, data) 列表"""
    t = chunk.get("type")
    if t == "content":
        return [("message", str(chunk.get("response", "")))]
    if t == "reasoning":
        return [("message_think", str(chunk.get("response", "")))]
    if t == "stream_reset":
        # reasoning 退化重试：通知前端清空本轮已流出的正文/思考/工具占位块，
        # 随后服务端会用新请求重新输出，不结束任务。
        return [("stream_reset", {"reason": str(chunk.get("data", ""))})]
    if t == "error":
        return [("error", {"msg": str(chunk.get("data", ""))})]
    if t == "stop":
        return [("usage", {"usage": chunk.get("usage", {})})]
    if t == "meta_info":
        return [("meta_info", {"user_msg_id": chunk.get("user_msg_id"), "ai_msg_id": chunk.get("ai_msg_id")})]
    if t == "compact_summary":
        # 自动上下文压缩产生的摘要：作为独立事件透传，前端可据此提示"上下文已压缩"。
        # 终端渠道由 HIDDEN_EVENTS_FOR_TERMINAL 静默丢弃，不刷屏。
        return [("compact_summary", {
            "msg_id": chunk.get("msg_id"),
            "content": chunk.get("content", ""),
            "timestamp": chunk.get("timestamp"),
        })]
    if t and t not in _KNOWN_SSE_EVENTS:
        # 仅对真正未知的 type 降级；降级时也必须保留完整 chunk 作 data，
        # 不可取 chunk.get("response") —— tool_call/tool_result 等 chunk
        # 使用 tool/args/result/id 字段，没有 response，取值会丢成空串。
        logger.warning("_map_agent_chunk 未知 type=%r，已降级为 message", t)
        return [("message", chunk)]
    # 已知但未在上面的分支中特化的事件（tool_call / tool_result / message_end / usage）：
    # 保持原始事件名，并透传完整 chunk 作为 data。
    # 前端按 event 名分发（App.vue case 'tool_call' → JSON.parse(data) 取 tool/args/id），
    # 因此这里绝不能改事件名，也不能只取 response 字段。
    return [(str(t or "message"), chunk)]
