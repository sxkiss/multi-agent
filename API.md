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
| `mode` | string | ✅ | 工作模式：`group` / `single` / `opencode` / `claude` |
| `model` | string | ❌ | 模型名称，默认 `auto` |
| `template` | string | ❌ | 提示词模板名（opencode/claude/single 共用），内置：`default`、`hermes`、`gpt5.5`、`unrestricted_jeli` |
| `system_prompt` | string | ❌ | 自定义系统提示词（优先级高于 template） |
| `workspace` | string | ❌ | 工作目录绝对路径 |
| `tools` | string[] | ❌ | 工具 ID 列表（仅 group 模式有效，其他模式自动全量加载） |
| `thinking` | string | ❌ | 是否启用思考链，默认 `"true"` |
| `web_search` | string | ❌ | 是否启用联网搜索，默认 `"true"` |
| `reasoning_effort` | string | ❌ | 推理深度：`low`/`medium`/`high`/`max`，默认 `"max"`（仅 opencode/claude） |
| `thinking` | string | ❌ | 思考模式开关，默认 `"true"` |

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
| `tool_call` | `{"id":"xxx","function":{"name":"tool_name","arguments":"{...}"}}` | AI 调用了某个工具 |
| `tool_result` | `{"tool_call_id":"xxx","content":"工具返回结果"}` | 工具执行结果 |
| `usage` | `{"usage":{"prompt_tokens":N,"completion_tokens":N}}` | token 用量统计 |
| `meta_info` | `{"user_msg_id":"xxx","ai_msg_id":"xxx"}` | 消息 ID 信息 |
| `compact_summary` | `{"content":"[自动压缩的历史摘要]\n..."}` | 上下文压缩摘要 |
| `error` | `{"msg":"错误信息"}` | 错误 |
| `emit_progress` | `{"msg":"进度信息"}` | 工具执行进度推送 |

---

### 1.3 停止任务

```
POST /api/chat/stop
Content-Type: application/json

{"session_id": "abc-123"}
```

---

### 1.4 查询任务状态

```
GET /api/chat/status?session_id=<sid>&job=<job_key>
```

---

### 1.5 查看历史事件

```
GET /api/chat?session_id=<sid>&job=<job_key>
```

返回该 job 的所有已缓存事件列表。

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

---

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
- `strict_tools = False`，自动加载全部 53 个工具（排除 4 个集团工具）
- 提示词来源优先级：`system_prompt` > `template` > config.json 默认提示词
- 工具集覆盖：文件操作、系统管理、网络检测、网站管理、Docker、数据库、mem0 记忆等
- 启用技能摘要注入（369 个技能，摘要形式）
- 启用 mem0 记忆系统说明

**内置模板（template 字段）：**

| 模板名 | 说明 |
|--------|------|
| `default` | 标准工程助手，闭环工作流 |
| `hermes` | HERMES 行为准则驱动 |
| `gpt5.5` | GPT-5.5 unrestricted 模板 |
| `unrestricted_jeli` | 无限制工程分析 |
| `__none__` | 跳过模板，使用 config.json 默认提示词 |

---

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
- 提示词模板与 single/claude 共用

**额外参数：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `reasoning_effort` | `"max"` | opencode 推理深度 |

---

### 2.4 claude 模式

**特点：** 调用 Claude Code CLI 作为后端，使用 Claude 模型。

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

**行为：**
- 通过 `claude_bridge.py` 启动 Claude Code CLI 子进程
- 与 opencode 模式参数结构一致

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

## 四、辅助接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/config` | 获取全局配置 |
| POST | `/api/config` | 保存全局配置 |
| GET | `/api/models` | 获取可用模型列表 |
| GET | `/api/tools` | 获取所有工具列表（含分类、风险等级） |
| GET | `/api/skills` | 获取所有技能列表 |
| POST | `/api/skills/install` | 安装技能（ZIP） |
| POST | `/api/skills/uninstall` | 卸载技能 |
| GET | `/api/crew/agents` | 获取子代理列表 |
| POST | `/api/crew/save` | 保存/创建子代理 |
| POST | `/api/crew/delete` | 删除子代理 |
| POST | `/api/crew/dept_save` | 创建部门 |
| POST | `/api/crew/dept_delete` | 删除部门 |
| POST | `/api/hotreload` | 热重载工具/技能/子代理 |
| GET | `/mcp/health` | MCP 服务健康检查 |
| GET | `/mcp/tools` | MCP 工具列表 |
| POST | `/mcp/tools/{tool_name}/call` | MCP 工具调用 |

---

## 五、各模式参数对照表

| 参数 | group | single | opencode | claude |
|------|-------|--------|----------|--------|
| `message` | ✅ | ✅ | ✅ | ✅ |
| `session_id` | ✅ | ✅ | ✅ | ✅ |
| `mode` | ✅ | ✅ | ✅ | ✅ |
| `model` | ✅ | ✅ | ✅ | ✅ |
| `template` | — | ✅ | ✅ | ✅ |
| `system_prompt` | — | ✅ | ✅ | ✅ |
| `workspace` | ✅ | ✅ | ✅ | ✅ |
| `tools` | ✅（经理工具） | —（自动全量） | — | — |
| `thinking` | ✅ | ✅ | — | — |
| `web_search` | ✅ | ✅ | — | — |
| `reasoning_effort` | — | — | ✅ | ✅ |
| `enable_mcp` | ✅（config） | ✅（config） | — | — |
| `strict_tools` | `True` | `False` | — | — |
| `code_mode` | `True` | `True` | — | — |

> `✅` = 有效参数，`—` = 不适用或走独立通道

---

## 六、配置参数（config.json）

```jsonc
{
  "system_prompt": "运维助手默认提示词...",
  "api_base_url": "http://127.0.0.1:3333/v1",
  "api_key": "...",
  "default_model": "auto",
  "context_window_kb": 512,        // 上下文窗口 (KB)，触发压缩阈值 = 95%
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
