# MEMORY — 记忆与偏好

> 本文件记录跨会话需要延续的上下文：用户稳定偏好、重要决策、待办事项。
> 只记录有价值的信息，避免琐碎流水账。

## 用户偏好

- 始终使用简体中文回复。
- 期望"执行优先"的答复：先给可验证的具体步骤与证据，再下结论；结论必须附带依据（代码片段、日志、命令输出）。
- 涉及删除数据、重启服务等危险操作前，必须先向用户确认。

## 重要决策 / 上下文

- **项目定位**：Multi-agent —— 通用多智能体协作平台，采用「一人一集团」模式（Boss 下达目标 → 经理拆解计划 → 部门/成员并行执行 → 汇总汇报）。
- **技术栈**：后端 FastAPI + uvicorn（OpenAI 兼容协议，可接任意网关）；前端 Vue3 + Vite。
- **部署形态**：服务端口 9876；systemd 单元 `bt-agent.service`（注意：不是 multi-agent.service，历史笔记曾写错）；工作目录为项目根目录（git 仓库）；**`Restart=always` + `RestartSec=5`，杀进程后 5 秒自动重启，无需 sudo systemctl restart**。服务进程会 `pkill -f "opencode serve --port"` 清理残留（模块加载时执行）。
- **架构分层**：
  - API 层：`web_server.py`（FastAPI 入口，全部端点）
  - 业务层：`chat_client/agent.py`（主 Agent 循环）
  - 工具层：`chat_client/tools/*`；多智能体：`task.py`
  - 记忆/RAG：`memory.py`、`retrieval.py`；MCP：`mcp_client.py`；Skills：`skills.py` + `skills/*/SKILL.md`
  - 调度：`tools/scheduler.py`；热重载：`hotreload.py`
- **配置**：`config.json` 是唯一事实源；配套 `crew_agents.json`、`scheduled_tasks.json`、`agents.json`。RAG 参数：`rag_retrieval_count: 10`、`rag_final_count: 5`。
- **Agent 类要点**（`chat_client/agent.py`）：接收 `session_id` + 配置 dict，提取 api_key/base_url/model_name/rag_trigger_threshold/max_tool_iterations/context_window_kb/enabled_tools；基于 `openai` 库，维护 `original_tools` 列表，集成 `MemoryManager`、`RAGService`/`ExternalRAGService`、工具 `registry`。
- **opencode 集成计划**（`PLAN_opencode_integration.md`）：将 opencode 作为载体后端接入，保留现有集团模式编排；已完成 MonkeyCode 实地调研（10.126.126.100）；涉及 ACP 模式、配置注入、状态机、keepalive 参考实现。
- **allbot 小助手插件**（`allbot/plugins/AssistantPlugin`）：微信→Multi-agent 的桥接插件，通过 SSE 流式对接 `/api/chat/start` + `/api/chat/events`；配置 `api-mode=single`，触发词"小助手"。

- **双 AgentMain 陷阱（重要）**：项目里有**两个** `AgentMain` 类——`web_server.py:286` 定义并 `web_server.py:1945` 实例化为 `agent_main`（独立部署实际加载的），与 `agent_main.py:49` 的面板兼容版是**两套独立代码**，方法集不同。给 `/api/*` 加路由时，必须调用 `web_server.py` 自己 AgentMain 实例上的方法；不能假设 `agent_main.py` 的方法在运行时可用（曾因误调 `set_tool_show_status`/`set_skill_status`/`del_chat_msg` 导致 500）。
- **前端已独立部署化（2026-09-16）**：原本前端通过 `window.ai_tools.send({url:'/plugin?action=a&name=ai_agent&s=xxx'})` 走面板代理。已全量改造为 `fetch('/api/...')` 标准 REST，不再依赖 `window.ai_tools`（`ChatInput.vue` 仅保留 `window.ai_tools?.open` 兼容分支，独立环境自然跳过）。`App.vue`/`SettingsDrawer.vue` 各注入 `apiGet/apiPost` 封装。后端为此补了 `POST /api/chat/delete_message`、`/api/tools/show_status`、`/api/skills/status` 三个路由。部署用 `./deploy.sh`（先 build 再 rsync 到 `static/assets/` + 根 index.html）。
- **改后必重启**：改 `web_server.py` 后必须杀进程让 systemd 自动拉起（约 5~13 秒），否则新路由仍是 500/旧代码；验证可用 `curl /api/config` 看是否 200。
- **凭据脱敏契约（2026-09-22）**：`GET /api/config` 的 `api_key` / `embedding_api_key` 一律回空串 + `<field>_set` 布尔（与 search 配置同一约定，不再回 `"--"` 占位符）；前端保存时留空 = 不修改。`GET /api/models` 支持不带 key：仅当 `base_url` 与已存 `api_base_url` 一致（或为空）时后端回退已存凭据，不一致则拒绝（防凭据被诱导发往任意地址）。
- **停止任务机制（2026-09-22）**：`ChatJob.cancel_event` + Agent 循环检查点（流式 chunk / 重试等待 / 工具执行前 / 迭代开头）；停止时为未执行 tool_calls 补"未执行"结果防下一轮 400。`ChatJobManager.stop()` 顺序：`request_stop()` → `finish("stopped")` → `agent.close()`。
- **延迟重启手法（2026-09-22 已验证）**：对话本身跑在服务进程内（opencode serve 挂在 bt-agent.service cgroup）时，重启要经 `atd` 调度独立脚本（cgroup `0::/system.slice/atd.service`，不受重启牵连），脚本用 `flock` 防重复实例，轮询 `jobs/*.jsonl` 静默窗口判断回合结束（MIN_DELAY=150s 起步、QUIET=120s 无新增、STEP=20s 递增、CAP=900s 上限）后再 kill MainPID，避免掐断自己的回复。脚本 `/tmp/opencode/delayed_restart.sh`，日志 `/tmp/opencode/restart-bt-agent.log`。

## 待办 / 跟进

- **REVIEW.md 代码审查**：共 35 个问题（严重 4 / 高 9 / 中 10 / 低 12），尚未修复。关键严重项：
  - `web_server.py:414-416` — RAG 配置键不匹配：代码读 `rag.retrieval_count`，但 `DEFAULT_CONFIG` 用的是 `rag_retrieval_count`，导致 RAG 配置实际失效。
  - `agent_main.py:124` — `__init__` 直接修改共享类变量 `DEFAULT_CONFIG`，存在跨实例污染风险。
  - 工具执行层安全防线可被完整绕过：`risk_level` 已注册但从未被检查。
  - 前端 SSE 解析器存在多处跨 chunk 边界缺陷（消息拆分时可能丢数据）。
- **opencode 集成**：按 `PLAN_opencode_integration.md` 推进接入（下一步需用户确认优先级）。
