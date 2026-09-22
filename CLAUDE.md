# CLAUDE — 上下文恢复指南

> 本文件用于在上下文压缩后快速恢复关键信息。代理应定期重新读取此文件以保持上下文。

## 当前任务状态
- 项目：Multi-agent —— 通用型多智能体协作平台
- 工作目录：`<项目根目录>`（部署时由 systemd `WorkingDirectory` 指定）
- 服务端口：9876
- 系统单元：`multi-agent.service`

## 关键规则（必须遵守）
1. **安全第一**：执行危险命令前必须与用户确认
2. **真实落地**：不捏造数据，提供可执行的方案
3. **工具使用**：严禁重复调用同一工具，缺少参数时主动追问
4. **记忆维护**：重要信息记录到MEMORY.md
5. **Git 分支**：本地开发一律在 `dev` 分支（无则创建）；**只有 Boss 说"合并"才 merge 到 main 并推送**，不得自行合并或推送 main

## 当前上下文
- 用户：Boss（老板），使用简体中文
- 角色：AI助手，运维专项能力
- 风格：专业、直接、可落地

## 最近操作记录
- **2026-09-19** 诊断所有模式（opencode/claude/codex/native）工作目录读取与历史续对话：
  - opencode 工作目录读取 ✅、历史 167 条 ✅、会话复用 ✅
  - claude 工作目录 ✅、主会话历史 ✅；但 subagent(agent-*) 混入历史列表无法续聊
  - codex 工作目录 ✅、历史 ✅；但历史列表点击续聊因 session_id 为 rollout-文件名、
    run_codex_chat 只认纯 UUID → 每次新建会话（丢上下文）❌
  - native group/early single 历史 workspace 多为空（历史遗留）
- **2026-09-19** 修复 3 处并重启 bt-agent.service（注意：系统单元是 `bt-agent.service`，非文档中的 multi-agent.service）：
  1. `chat_client/codex_bridge.py:196` 续聊判定改为 `re.search` 提取 rollout 文件名末尾 UUID → 历史列表点击可真正 resume
  2. `chat_client/claude_bridge.py:query_claude_sessions` 跳过 `/subagents/` 目录 → 列表只剩主会话
  3. `chat_client/opencode_bridge.py:query_history` 残留 `slug AS directory` → 改为 `directory`

## 待办事项
- （无）

---
*最后更新：2026-09-19*
*代理应定期更新此文件以保持上下文连续性*