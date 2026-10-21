# Multi-agent API 文档

> 基础地址：`http://<host>:9876`
> 运行方式：`uvicorn web_server:app --host 0.0.0.0 --port 9876`

---

## 一、聊天接口（核心）

### 1.1 启动对话任务

```
POST /api/chat/start
Content-Type: application/json
```

**请求体：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `message` | string | ✅ | 用户输入内容 |
| `session_id` | string | ✅ | 会话 ID（同一 session_id 共享历史） |
| `mode` | string | ✅ | 工作模式：`group` / `single` / `opencode` / `claude` / `codex` |
| `model` | string | ❌ | 模型名称，默认 `auto` |
| `template` | string | ❌ | 提示词模板名（opencode/claude/single 共用） |
| `system_prompt` | string | ❌ | 自定义系统提示词（优先级高于 template） |
| `workspace` | string | ❌ | 工作目录绝对路径 |
| `tools` | string[] | ❌ | 工具 ID 列表（仅 group 模式有效，其他模式自动全量加载） |
| `thinking` | string | ❌ | 是否启用思考链，默认 `"true"` |
| `web_search` | string | ❌ | 是否启用联网搜索，默认 `"true"` |
| `reasoning_effort` | string | ❌ | 推理深度：`low`/`medium`/`high`/`max`，默认 `"max"`（仅 opencode/claude/codex） |
| `hitl` | string | ❌ | HITL 审批模式：`ask`/`allow_all`/`allow_tools` |
| `hitl_tools` | string[] | ❌ | `allow_tools` 模式下的白名单工具 |
| `base_url` | string | ❌ | 自定义 API 地址（opencode/claude/codex） |
| `api_key` | string | ❌ | 自定义 API 密钥 |
| `api_protocol` | string | ❌ | API 协议：`responses`/`chat`/`anthropic` |

**响应（成功）：**

```json
{
  "status": true,
  "data": {
    "session_id": "abc-123",
    "job": "abc-123-0",
    "started": true
  }
}
```

**响应（失败）：**

```json
{
  "status": false,
  "msg": "缺少参数 message"
}
```

---

### 1.2 订阅事件流（SSE）

```
GET /api/chat/events?session_id=<sid>&job=<job_key>&last_id=<id>
Accept: text/event-stream
```

| 参数 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会话 ID |
| `job` | string | 启动时返回的 job key |
| `last_id` | int | 断线续传：上次收到的最大事件 ID，从下一个开始推送 |

**事件格式：**

```
id: <事件序号>
event: <事件类型>
data: <JSON 数据>
```

**事件类型一览：**

| event | data 结构 | 说明 |
|-------|-----------|------|
| `message` | `"一段文本"` | AI 回复的文本片段（流式输出） |
| `message_think` | `"思考过程文本"` | AI 思考过程（thinking 模式） |
| `message_end` | `null` | 对话结束 |
| `tool_call` | `{"type":"tool_call","tool":"name","args":"{...}","id":"..."}` | AI 调用了某个工具 |
| `tool_result` | `{"type":"tool_result","tool":"name","result":"...","id":"..."}` | 工具执行结果 |
| `usage` | `{"usage":{"total_tokens":N,...}}` | token 用量统计 |
| `meta_info` | `{"user_msg_id":"xxx","ai_msg_id":"xxx"}` | 消息 ID 信息 |
| `compact_summary` | `{"content":"[自动压缩的历史摘要]\n..."}` | 上下文压缩摘要 |
| `stream_reset` | `{"reason":"..."}` | reasoning 退化重试，清空本轮输出 |
| `crew_plan` / `crew_step` | JSON | 集团模式：计划 / 成员进度 |
| `error` | `{"msg":"错误信息"}` | 错误 |
| `emit_progress` | `{"msg":"进度信息"}` | 工具执行进度推送 |

---

### 1.3 停止任务

```
POST /api/chat/stop
Content-Type: application/json

{"session_id": "abc-123", "job": "abc-123-0"}
```

---

### 1.4 查询任务状态

```
GET /api/chat/status?session_id=<sid>&job=<job_key>
```

返回 `{running, status, last_id, elapsed, jobs}`。

---

### 1.5 查看历史事件

```
GET /api/chat?session_id=<sid>&job=<job_key>
```

返回该 job 的所有已缓存事件列表。

### 1.6 历史消息 / 删除

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/chat/history` | 会话历史列表（按 workspace 分组） |
| GET | `/api/chat/messages` | 获取指定会话的全部消息 |
| POST | `/api/chat/delete` | 删除会话。body: `{session_id}` |
| POST | `/api/chat/delete_batch` | 批量删除会话。body: `{session_ids: [...]}` |
| POST | `/api/chat/delete_message` | 删除单条消息。body: `{session_id, message_id}` |

---

## 二、各模式详细说明

### 2.1 group 模式（集团模式）

**特点：** 经理（Manager）自动拆解任务，分派给子代理执行。用户只需下达目标。

**请求示例：**

```json
{
  "message": "帮我分析服务器安全状态并生成报告",
  "session_id": "demo-001",
  "mode": "group",
  "model": "auto"
}
```

**行为：**
- `strict_tools = True`，经理只使用编排工具：`Task`、`RunCrew`、`CreateDepartment`、`RecruitMember`
- 系统提示词自动组装：SOUL.md + AGENTS.md + USER.md + MEMORY.md + 组织架构
- 子代理各自拥有完整工具集
- 最大并行任务数：3（`MAX_PARALLEL=3`）

### 2.2 single 模式（单 Agent 直干）

**特点：** 一个 Agent 直接完成所有工作，不经过分派。拥有完整工具集。

**请求示例：**

```json
{
  "message": "查看当前服务器负载",
  "session_id": "demo-002",
  "mode": "single",
  "model": "auto",
  "workspace": "/path/to/your/project"
}
```

**行为：**
- `strict_tools = False`，自动加载全部 80+ 内置工具
- 提示词来源优先级：`system_prompt` > `template` > config.json 默认提示词
- 工具集覆盖：文件操作、系统管理、网络检测、网站管理、Docker、数据库、mem0 记忆、看图、浏览器、媒体发送等
- 启用技能摘要注入（自动发现技能）
- 启用 mem0 记忆系统说明

### 2.3 opencode 模式

**特点：** 调用 opencode CLI 作为后端，支持完整的代码编辑和终端操作。

**请求示例：**

```json
{
  "message": "在项目中添加一个 health check 端点",
  "session_id": "demo-003",
  "mode": "opencode",
  "model": "auto",
  "workspace": "/path/to/your/project",
  "template": "default",
  "reasoning_effort": "max"
}
```

**行为：**
- 通过 `opencode_bridge.py` 启动 opencode CLI 子进程
- 使用 STDIO 传输协议
- 支持 session 恢复（`meta_info.opencode_session_id`）

### 2.4 claude 模式

**特点：** 调用 Claude Code CLI 作为后端。

**请求示例：**

```json
{
  "message": "重构这段代码，提高可读性",
  "session_id": "demo-004",
  "mode": "claude",
  "model": "auto",
  "workspace": "/path/to/your/project",
  "template": "default",
  "reasoning_effort": "max"
}
```

### 2.5 codex 模式

**特点：** 调用 Codex CLI 作为后端，支持线程恢复。

**请求示例：**

```json
{
  "message": "修复这个 bug",
  "session_id": "demo-005",
  "mode": "codex",
  "model": "auto",
  "workspace": "/path/to/your/project"
}
```

**相关接口：** `/api/codex/status`、`/api/codex/test`、`/api/codex/cli-config`（GET/POST）。

---

## 三、完整调用流程

```
前端 / 客户端
     │
     │ ① POST /api/chat/start  （启动任务）
     │    返回: { session_id, job, started }
     │
     │ ② GET  /api/chat/events?session_id=xxx&job=xxx  （订阅 SSE）
     │    接收: message / tool_call / tool_result / usage / message_end
     │
     │ ③ POST /api/chat/stop  （可选：停止任务）
     │
     └── 完成
```

**SSE 重连：**

```
GET /api/chat/events?session_id=xxx&job=xxx&last_id=42
// 从第 43 个事件开始推送（续传）
```

---

## 四、辅助接口（完整路由表）

> 以下为当前 `web_server.py` 中实际注册的全部 HTTP 路由。

### 4.1 配置 / 模型 / 组织

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 获取全局配置 |
| POST | `/api/config` | 保存全局配置 |
| GET | `/api/search/config` | 获取搜索配置 |
| POST | `/api/search/config` | 保存搜索配置 |
| GET | `/api/models` | 获取可用模型列表 |
| GET | `/api/agents` | 获取预置 Agent 列表 |
| GET | `/api/org` | 获取组织架构 |
| GET | `/api/metrics` | 运行指标（工具/LLM 调用计数）；`format=prometheus` 返回文本 |

### 4.2 工具 / 插件 / 技能

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/tools` | 获取所有工具列表（含分类、风险等级） |
| POST | `/api/tools/show_status` | 切换工具显示状态 |
| GET | `/api/plugins` | 列出磁盘上的插件及加载状态 |
| POST | `/api/plugins/load` | 加载插件（name 留空则加载全部） |
| POST | `/api/plugins/unload` | 卸载插件 |
| GET | `/api/skills` | 获取所有技能列表 |
| POST | `/api/skills/install` | 安装技能（ZIP） |
| POST | `/api/skills/uninstall` | 卸载技能 |
| POST | `/api/skills/install_url` | 从 URL 安装技能 |
| POST | `/api/skills/status` | 切换技能启用状态 |
| GET | `/api/skills/presentation` | 技能展示元数据（icon/分类/触发词） |
| GET | `/api/skills/packages` | 列出已打包的技能包 |
| POST | `/api/skills/export_package` | 导出技能包 ZIP（base64） |
| POST | `/api/skills/import_package` | 导入技能包 ZIP |
| POST | `/api/hotreload` | 热重载工具/技能/子代理 |

### 4.3 集团 / 专家 / 会话模式

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/crew/agents` | 获取子代理列表 |
| POST | `/api/crew/save` | 保存/创建子代理 |
| POST | `/api/crew/delete` | 删除子代理 |
| POST | `/api/crew/dept_save` | 创建部门 |
| POST | `/api/crew/dept_delete` | 删除部门 |
| GET | `/api/experts` | 列出本地专家库 |
| GET | `/api/experts/{expert_id}` | 获取单个专家详情 |
| GET | `/api/conversation/modes` | 列出会话模式（Ask/Plan/Craft） |

### 4.4 MCP

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/mcp/servers` | 列出 MCP 服务器 |
| POST | `/api/mcp/servers/add` | 添加 MCP 服务器 |
| POST | `/api/mcp/servers/remove` | 移除 MCP 服务器 |
| POST | `/api/mcp/servers/install` | 从市场安装 MCP 服务器 |
| GET | `/api/mcp/market` | MCP 市场列表 |
| GET | `/mcp/health` | MCP 服务健康检查 |
| GET | `/mcp/tools` | MCP 工具列表 |
| POST | `/mcp/tools/{tool_name}/call` | MCP 工具调用 |

### 4.5 文件 / 知识库

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/files/raw` | 安全读取文件（白名单路径，支持预览/下载） |
| GET | `/api/files/browse` | 浏览白名单目录 |
| POST | `/api/files/upload` | 上传文件 |
| GET | `/api/knowledge/list` | 列出知识库文档 + 统计 |
| POST | `/api/knowledge/upload` | 上传文档入库（pdf/docx/pptx/xlsx/html/md/txt/图片 OCR） |
| POST | `/api/knowledge/add_text` | 直接入库一段文本。body: `{text, name?}` |
| POST | `/api/knowledge/search` | 语义检索。body: `{query, top_k?, score?}` |
| POST | `/api/knowledge/delete` | 删除文档。body: `{doc_id}` |
| GET | `/api/kb/jobs` | 知识库索引任务队列状态 |

### 4.6 会话管理 / 上下文治理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/session/context` | 上下文用量分解（token 估算） |
| GET | `/api/session/export` | 导出会话 Markdown/JSON |
| POST | `/api/session/fork` | 从指定消息分叉新会话 |
| GET | `/api/sessions/stats` | 各会话规模统计 |
| GET | `/api/context/archives` | 工具结果归档统计 |
| GET | `/api/context/breakdown` | 上下文七段占用分析 |

### 4.7 安全 / 审批 / 斜杠 / 主动关怀

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/guard/rules` | 查看护栏规则 |
| POST | `/api/guard/reload` | 强制重载护栏规则 |
| GET | `/api/hitl/pending` | 列出待审批高风险请求 |
| POST | `/api/hitl/decide` | 批准/拒绝审批。body: `{id, approve}` |
| GET | `/api/slash/commands` | 列出斜杠命令 |
| POST | `/api/slash/run` | 执行斜杠命令。body: `{text, session_id?}` |
| GET | `/api/proactive/status` | 主动关怀服务状态 |

### 4.8 认证 / 卡密 / 小程序

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/status` | 登录状态 |
| POST | `/api/auth/login` | 登录 |
| POST | `/api/auth/logout` | 登出 |
| POST | `/api/auth/refresh` | 换发新令牌 |
| POST | `/api/auth/wxlogin` | 微信登录 |
| POST | `/api/auth/cardlogin` | 卡密登录（签发 JWT） |
| POST | `/api/auth/card/verify` | 验证卡密 |
| GET | `/api/card/admin/list` | 列出全部卡密 |
| POST | `/api/card/admin/create` | 生成新卡密 |
| POST | `/api/card/admin/update` | 更新卡密 |
| POST | `/api/card/admin/delete` | 删除卡密 |
| GET | `/api/card/user/config` | 当前卡密用户配置 |
| POST | `/api/card/user/config` | 保存卡密用户配置 |
| GET | `/api/mp/profile` | 小程序用户数据 |
| POST | `/api/mp/profile` | 小程序签到/资料/设置 |
| GET | `/api/mp/bot/status` | 小程序机器人连接状态 |
| POST | `/api/mp/bot/qrcode` | 申请专属二维码 |
| POST | `/api/mp/bot/disconnect` | 断开机器人 |

### 4.9 备份 / 服务令牌 / Agent

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/backups` | 列出备份 |
| POST | `/api/backups/create` | 创建备份 |
| POST | `/api/backups/restore` | 恢复备份 |
| POST | `/api/backups/auto` | 自动备份开关。`action=start\|stop\|status` |
| POST | `/api/service/token` | clawbot 换取长期令牌 |
| GET | `/api/agent/run` | Agent 任务（GET） |
| POST | `/api/agent/run` | 运行 Agent 任务 |

### 4.10 配置面板 / 页面

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 主页面 |
| GET | `/browser` | 浏览器控制台面板 |
| GET | `/connectors` | 连接器管理面板 |
| GET | `/api/opencode/config` / POST | opencode 配置 |
| GET | `/api/opencode/cli-config` / POST | opencode CLI 配置 |
| GET | `/api/opencode/status` | opencode 运行状态 |
| GET | `/api/claude/config` / POST | claude 配置 |
| GET | `/api/codex/cli-config` / POST | codex CLI 配置 |
| GET | `/api/codex/status` | codex 运行状态 |
| POST | `/api/codex/test` | codex 连通性测试 |

### 4.12 平台扩展（/api/v2）与浏览器

> 由 `connector_hub` 与 `platform_ext` 动态挂载，不破坏现有 `/api/*`。以下为当前实际注册路由。

**连接器中心（Connector Hub）**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v2/connectors/catalog` | 连接器市场目录 |
| GET | `/api/v2/connectors/instances` | 已安装实例列表 |
| POST | `/api/v2/connectors/instances` | 创建实例 |
| GET | `/api/v2/connectors/instances/{instance_id}` | 实例详情 |
| PATCH | `/api/v2/connectors/instances/{instance_id}` | 更新实例 |
| DELETE | `/api/v2/connectors/instances/{instance_id}` | 删除实例 |
| POST | `/api/v2/connectors/instances/{instance_id}/test` | 测试实例连通性 |
| POST | `/api/v2/connectors/test-credentials` | 测试凭据 |
| POST | `/api/v2/connectors/oauth2/authorize` | OAuth2 授权 |
| GET | `/api/v2/connectors/oauth2/callback` | OAuth2 回调 |
| POST | `/api/v2/connectors/oauth2/refresh` | 刷新令牌 |
| POST | `/api/v2/connectors/oauth2/manual-token` | 手动填入令牌 |
| GET | `/api/v2/connectors/oauth2/status` | OAuth2 状态 |
| GET | `/api/v2/connectors/gateway/kinds` | 网关类型列表 |
| GET | `/api/v2/connectors/gateway/{kind}/tools` | 网关可用工具 |
| GET | `/api/v2/connectors/cli/status` | 全部 CLI 状态 |
| GET | `/api/v2/connectors/cli/{kind}/status` | 单个 CLI 状态 |
| POST | `/api/v2/connectors/cli/{kind}/install` | 安装 CLI |
| POST | `/api/v2/connectors/sync` | 触发同步 |
| GET/POST | `/api/v2/connectors/internal/mcp/{kind}/{instance_id}` | 内部 MCP 桥接 |

**团队 / 通道 / 存储**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v2/teams` | 团队列表 |
| GET | `/api/v2/teams/template` | 团队模板 |
| POST | `/api/v2/teams` | 创建团队 |
| GET | `/api/v2/teams/{team_id}` | 团队详情 |
| PATCH | `/api/v2/teams/{team_id}` | 更新团队 |
| DELETE | `/api/v2/teams/{team_id}` | 删除团队 |
| GET | `/api/v2/channels` | 通道列表 |
| GET | `/api/v2/channels/types` | 通道类型 |
| POST | `/api/v2/channels/dispatch` | 通道分发 |
| GET | `/api/v2/storage/backends` | 存储后端列表 |
| POST | `/api/v2/storage/backends` | 创建存储后端 |
| GET | `/api/v2/storage/backends/{name}` | 后端详情 |
| PATCH | `/api/v2/storage/backends/{name}` | 更新后端 |
| DELETE | `/api/v2/storage/backends/{name}` | 删除后端 |
| POST | `/api/v2/storage/backends/{name}/probe` | 探测后端 |
| GET | `/api/v2/storage/files` | 存储文件列表 |

**用量 / 网关 / 记忆**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v2/usage/record` | 记录用量 |
| GET | `/api/v2/usage/summary` | 用量汇总 |
| GET | `/api/v2/usage/ledger` | 用量明细账本 |
| GET | `/api/v2/gateway/channels` | 网关通道列表 |
| GET | `/api/v2/gateway/status` | 网关状态 |
| GET | `/api/v2/gateway/templates` | 网关模板 |
| POST | `/api/v2/gateway/channels/{kind}/config` | 保存通道配置 |
| POST | `/api/v2/gateway/channels/{kind}/start` | 启动通道 |
| POST | `/api/v2/gateway/channels/{kind}/stop` | 停止通道 |
| POST | `/api/v2/gateway/channels/{kind}/qrcode/generate` | 生成二维码 |
| POST | `/api/v2/gateway/channels/{kind}/qrcode/poll` | 轮询扫码状态 |
| GET | `/api/v2/gateway/instances` | 实例列表 |
| POST | `/api/v2/gateway/instances` | 创建实例 |
| PATCH | `/api/v2/gateway/instances/{instance_id}` | 更新实例 |
| DELETE | `/api/v2/gateway/instances/{instance_id}` | 删除实例 |
| POST | `/api/v2/gateway/instances/{instance_id}/start` | 启动实例 |
| POST | `/api/v2/gateway/instances/{instance_id}/stop` | 停止实例 |
| GET | `/api/v2/memory/status` | 记忆服务状态 |
| POST | `/api/v2/memory/store` | 存储记忆 |
| GET | `/api/v2/memory/recall` | 召回记忆 |
| GET | `/api/v2/memory/list` | 记忆列表 |
| GET | `/api/v2/memory/get/{node_id}` | 获取单条记忆 |
| DELETE | `/api/v2/memory/delete/{node_id}` | 删除记忆 |
| GET | `/api/v2/memory/export` | 导出记忆 |
| POST | `/api/v2/memory/import` | 导入记忆 |

**浏览器控制（Browser）**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/v2/browser/env-status` | 浏览器环境状态 |
| POST | `/api/v2/browser/install` | 安装浏览器 |
| POST | `/api/v2/browser/uninstall` | 卸载浏览器 |
| GET | `/api/v2/browser/harness-sessions` | 会话列表 |
| POST | `/api/v2/browser/sessions/{session_id}/handoff` | 交接浏览器会话 |
| POST | `/api/v2/browser/close` | 关闭浏览器会话 |
| GET | `/api/v2/browser/proxy` | 获取代理配置 |
| POST | `/api/v2/browser/proxy` | 设置代理 |
| GET | `/api/v2/browser/record-replay/status` | 录制回放状态 |
| GET | `/api/v2/browser/record-replay/recordings` | 录制列表 |
| POST | `/api/v2/browser/record-replay/start` | 开始录制 |
| POST | `/api/v2/browser/record-replay/stop` | 停止录制 |
| POST | `/api/v2/browser/record-replay/stop-and-generate-skill` | 停止并生成技能 |
| POST | `/api/v2/browser/record-replay/skill-content` | 获取技能内容 |
| POST | `/api/v2/browser/record-replay/replay` | 回放录制 |
| WS | `/browser-stream/ws` | 浏览器实时画面流 |

### 4.11 OpenAI 兼容接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/v1/models` | 模型列表 |
| POST | `/v1/chat/completions` | 聊天补全（OpenAI 兼容） |

---

## 五、各模式参数对照表

| 参数 | group | single | opencode | claude | codex |
|------|-------|--------|----------|-------|-------|
| `message` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `session_id` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `mode` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `model` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `template` | — | ✅ | ✅ | ✅ | ✅ |
| `system_prompt` | — | ✅ | ✅ | ✅ | ✅ |
| `workspace` | ✅ | ✅ | ✅ | ✅ | ✅ |
| `tools` | ✅（经理工具） | —（自动全量） | — | — | — |
| `thinking` | ✅ | ✅ | — | — | — |
| `web_search` | ✅ | ✅ | — | — | — |
| `reasoning_effort` | — | — | ✅ | ✅ | ✅ |
| `enable_mcp` | ✅（config） | ✅（config） | — | — | — |
| `strict_tools` | `True` | `False` | — | — | — |
| `code_mode` | `True` | `True` | — | — | — |

> `✅` = 有效参数，`—` = 不适用或走独立通道

---

## 六、配置参数（config.json）

```jsonc
{
  "system_prompt": "运维助手默认提示词...",
  "api_base_url": "http://127.0.0.1:3333/v1",
  "api_key": "...",
  "default_model": "auto",
  "context_window_kb": 512,        // 上下文窗口 (KB)，轮间压缩阈值 90%/95%/97% 渐进
  "inflight_compress": true,       // 单轮内上下文闸门：工具循环中超出预算时精简旧工具结果
  "inflight_keep_recent_tools": 6, // 闸门保留最近 N 条工具结果不精简
  "tool_result_archive": true,     // 工具结果超阈值时精简+归档（可 ReadToolArchive 召回）
  "tool_result_max_chars": 8000,   // 超过此字符数才精简（闸门内强制 ≤4000）
  "tool_result_head_chars": 3000,  // 精简后保留头部字符数
  "tool_result_tail_chars": 1500,  // 精简后保留尾部字符数
  "enable_mcp": true,              // 是否加载 MCP 工具
  "agent": {
    "max_tool_iterations": 999999,  // 最大工具调用轮次
    "temperature": 1.0,
    "top_p": 0.8,
    "reasoning_effort": "high",     // 推理深度
    "api_max_retry": 3,             // API 调用最大重试次数
    "api_retry_base_wait": 1,       // 重试基础等待时间 (秒)
    "api_retry_max_wait": 10        // 重试最大等待时间 (秒)
  },
  "rag": {
    "sliding_window_size": 15,      // 无摘要时保留的对话轮次
    "rag_trigger_threshold": 10,    // RAG 触发阈值
    "rag_retrieval_count": 10,
    "rag_final_count": 5
  },
  "opencode": {
    "default_template": "",         // 默认模板名
    "templates": {}                 // 自定义模板（名称 → 提示词内容）
  }
}
```
