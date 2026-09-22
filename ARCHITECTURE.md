<!-- AUTO-DOC: Update me when project structure or architecture changes -->

# 系统开发文档

> 项目：Multi-agent —— 通用型多智能体协作平台（集团模式）
> 版本基准：2026-08-23 · 服务端口 9876 · systemd 单元 `bt-agent.service`
> 配套文档：`REVIEW.md`（历次审核与修复记录）

---

## 1. 项目定位与总体架构

通用型 AI Agent 平台。核心交互模型为**「一人一集团」**：

```
Boss（用户）——只下达目标
   └─ 经理 Manager——拆解计划 · 分派 · 汇总交付（主对话）
        ├─ 各部门 Departments（调研/研发/运维/测试/审核/财务…可自定义）
        │    └─ 成员 Members——每个成员自带专属工具集与系统提示词
        └─ 能力底座：142+ 工具 / 技能(Skill) / MCP / RAG / 定时任务
```

技术栈：FastAPI + uvicorn（后端）· Vue3 + Vite（前端）· OpenAI 兼容协议（任意网关）。

### 分层

| 层 | 文件 | 职责 | 热加载 |
|---|---|---|---|
| API 层 | `web_server.py` | 全部 HTTP/SSE 路由、参数解析、集团模式装配 | ❌ 核心 |
| 业务层 | `chat_client/agent.py` | 主循环、流式重试、工具执行编排 | ❌ 核心 |
| 工具层 | `chat_client/tools/*` | registry 统一封装：schema/校验/超时/审计/护栏 | ✅ |
| 多智能体 | `chat_client/tools/task.py` | Task 子代理 + RunCrew 集团模式 + 组织扩张工具 | ✅ |
| 记忆/RAG | `memory.py` `retrieval.py` | 会话持久化、向量检索 | ❌ 核心 |
| MCP | `mcp_client.py` | stdio MCP 接入，注册为普通工具 | ✅(显式) |
| 技能 | `skills.py` + `skills/*/SKILL.md` | 动态扫描，无需注册 | ✅ 天然 |
| 定时任务 | `tools/scheduler.py` | 调度线程 + 4 个工具 | ⚠️ 数据热、代码冷 |
| 热加载中心 | `hotreload.py` | 见 §9 | — |

---

## 2. 目录结构

```
├── web_server.py            # FastAPI 入口（全部路由）
├── public.py                # 面板 public 兼容 shim
├── config.json              # 运行配置（唯一事实来源）
├── crew_agents.json         # 自定义成员 + 部门
├── scheduled_tasks.json     # 定时任务
├── agents.json              # 面板级预置代理（run_agent 路由用）
├── deploy.sh                # 部署脚本
├── favicon.png              # 网站图标
├── requirements.txt         # Python 依赖
├── chat_client/
│   ├── agent.py             # 主 Agent 循环（流式/重试/续写/工具执行）
│   ├── memory.py            # 会话历史（原子写）
│   ├── retrieval.py         # SimpleVectorDB + RAG + ExternalRAG
│   ├── mcp_client.py        # MCP stdio 客户端（单例+锁）
│   ├── skills.py            # 技能扫描/安装/卸载
│   ├── hotreload.py         # 热加载中心
│   └── tools/
│       ├── __init__.py      # ToolRegistry（封装核心）
│       ├── base.py          # _xml_response
│       ├── agent_tools.py   # 文件/系统/网络/网站/数据库工具
│       ├── terminal.py      # RunCommand 后台命令管理
│       ├── sys_ops.py       # 运维增强 13 件套
│       ├── scheduler.py     # 定时任务系统
│       ├── task.py          # Task/RunCrew/CreateDepartment/RecruitMember
│       ├── edit.py py_exec.py node_exec.py webfetch.py todo.py summary.py skill.py
├── frontend/src             # Vue3 源码（App.vue + 5 组件）
├── static/vue               # 构建产物部署位
├── prompts/ skills/ mcp/ sessions/ logs/
│   ├── [prompts/](./prompts/INDEX.md)      # 系统提示词模板
│   └── [mcp/](./mcp/INDEX.md)              # MCP 配置文件
├── [chat_client/](./chat_client/INDEX.md)  # Agent 核心运行时
│   └── [tools/](./chat_client/tools/INDEX.md) # 142+ 工具实现
├── [agents/](./agents/INDEX.md)            # 预置 Agent（面板兼容）
├── [frontend/src/](./frontend/src/INDEX.md) # Vue3 前端源码
│   └── [components/](./frontend/src/components/INDEX.md) # UI 组件
└── ARCHITECTURE.md          # 本文
```

---

## 3. 核心流程

### 3.1 Boss 对话流（后台任务式，刷新/断开不中断）

```
POST /api/chat/start {session_id, message, model, tools}
  → ChatJobManager.create_if_idle() 原子占位（防双开）
  → _build_chat_agent(): config 装配（workspace/strict_tools/code_mode）
  → 后台线程 _run_chat_job: agent.chat() chunk → job.events[seq]
GET  /api/chat/events?session_id&last_id   ← SSE 回放+跟随，断线按 id 续传
GET  /api/chat/status                       ← {running,status,last_id,elapsed}
POST /api/chat/stop                         ← 置 stopped + agent.close()
```

- **事件类型**：meta_info / message / message_think / tool_call / tool_result / usage / error / message_end
- **AI API 断流重试**（agent.py 内）：连接类/429/5xx 可重试；无输出→整体重来；已有输出→**续写模式**（partial 作为上下文 + system-note 要求无缝继续），最终持久化文本 = 用户实际所见

### 3.2 集团模式 RunCrew（hierarchical）

```
RunCrew(objective, agents?, max_steps?)
 1. 经理(parent LLM) → JSON 计划 [{agent,task,expected_output}]
 2. 顺序执行：每步创建子 Agent(def.allowed_tools 严格工具集)，
    前序产出注入后续 system 上下文
 3. 经理汇总 → <crew_report><plan/><final_answer/>
```
组织感知：roster 按**部门分组**呈现给经理。

### 3.3 组织自我扩张

主对话经理默认持有：`Task` `RunCrew` `CreateDepartment` `RecruitMember`。
- CreateDepartment(name,title,desc)：去重、上限 20
- RecruitMember(name,department,description,backstory,tools)：工具过滤到真实注册项、上限 50、招聘即热生效

### 3.4 定时任务

`scheduled_tasks.json` 持久化；调度线程 15s tick：
- schedule_type：`interval`(每N分钟) / `daily`(HH:MM，跨天正确)
- 到点 → `chat_start` 独立会话（sched_*）；running 标志防重叠
- 工具：ScheduleTask / ListScheduledTasks / CancelScheduledTask / RunScheduledTaskNow

---

## 4. 工具体系（ToolRegistry）

所有工具统一经装饰器注册，自动获得：

| 能力 | 实现 |
|---|---|
| OpenAI Schema 生成 | 类型提示 + docstring `Args:` 描述提取 + default 注入 |
| 危险命令黑名单 | rm -rf / mkfs / dd 写盘 / fork bomb / shutdown… **始终强制** |
| 三级风险护栏 | high 执行前拦截要求确认（env `AI_AGENT_RISK_GUARD=off` 关闭）；medium 记日志 |
| 参数前置校验 | 必填缺失/类型错误 → 带修正指引的错误返回给模型自我纠错 |
| 统一执行超时 | 默认 600s（env `AI_AGENT_TOOL_TIMEOUT`），注册时可覆盖（Task=1800, RunCommand=3600）；超时返回可读错误 |
| 全链路审计 | `logs/tool_audit.jsonl`：ts/tool/risk/duration/status/args/result_head；>5MB 轮转 .1 |
| 热重载回填 | 注册表快照恢复外部模块注册（见 §9） |

新增工具模板：
```python
from . import register_tool
from .base import _xml_response

@register_tool(category="分类", name_cn="中文名", risk_level="low", timeout=120)
def my_tool(arg1: str, count: int = 10) -> str:
    """一句话功能描述（模型据此选择）

    Args:
        arg1: 参数说明
        count: 数量说明
    """
    try:
        ...
        return _xml_response("done", 结果文本)
    except Exception as e:
        return _xml_response("error", str(e))
```
写完调 HotReload("tools") 即生效。

### 敏感与边界约束
- Read/tail_log/Grep：`/etc/shadow` `/etc/sudoers` `.ssh 私钥` 黑名单
- Write/DeleteFile/SearchReplace/extract_archive/compress：`_is_path_allowed` 白名单（项目目录/home/tmp），Zip-Slip 预检
- WebFetch/curl_url/http_check：仅 http/https
- extract_archive：tar 'data' filter + 成员路径穿越双重防护

---

## 5. 多智能体与部门

- 内置角色：search/planner/coder（task.py `_register_default_agents`，已归部）
- 自定义成员 + 部门：`crew_agents.json`

```json
{
  "departments": [{"name":"dev","title":"研发部","description":"..."}],
  "agents": [{"name":"coder","department":"dev","description":"Goal",
              "backstory":"系统提示词","tools":["Read","RunCommand"],"model":""}]
}
```

- 接口：GET `/api/org`（首页架构树）、GET `/api/crew/agents`、POST `/api/crew/save|delete`、POST `/api/crew/dept_save|dept_delete`
- 热更新：保存/删除即调 `reload_custom_agents()`；手动改文件则由 watcher 自动触发
- Boss 模式：主对话 `strict_tools=true`，仅 Task/RunCrew/CreateDepartment/RecruitMember；成员各自 strict 于自身 tools

---

## 6. 技能系统

- 规范：`skills/<name>/SKILL.md`，YAML frontmatter（name/description）
- 安装：ZIP 上传（base64）/ URL（直链 zip 或 GitHub 仓库自动转 codeload）/ 手动放置
- 防护：路径穿越、≤30MB 下载、≤50MB 解压、成员数 ≤500、失败回滚、同名需 overwrite
- 接口：`/api/skills/install|install_url|uninstall`、GET `/api/skills`

---

## 7. 安全模型汇总

| 类别 | 机制 |
|---|---|
| 执行拦截 | 危险命令黑名单（全工具参数扫描）、high 风险确认、参数 Schema 校验 |
| 文件 | 读敏感黑名单(shadow/sudoers/私钥)；写/删白名单 realpath 校验；原子写防损坏 |
| 网络 | http/https 强制；内网 SSRF 拦截（crew URL 安装可用 env 放行）|
| 归档 | Zip-Slip 成员预检 + tar data filter |
| 组织 | builtin 保护、名称白名单、数量上限 |
| 审计 | tool_audit.jsonl 全量留痕 + RiskGuard 日志 |

---

## 8. 前端（frontend/src）

- **App.vue**：全局状态机——SSE 处理器(createSSEProcessor：id/event/data 行、跨chunk缓冲)、后台任务发送/续播(resumeRunningJobIfAny)、消息队列(localStorage 持久化+processQueue 门控)、任务状态栏(elapsed 计时)、模型选择(applyConfiguredModel 校准)
- **ChatMain.vue**：消息/流式块/工具卡片渲染 + **首页集团架构图**(fetch /api/org) + 滚动锁
- **ChatInput.vue**：输入/粘贴图片/文件标签（发送中提交=入队）
- **SettingsDrawer.vue**：四区设置(AI接口与模型/MCP/Crew/技能) + 热重载按钮
- **MessageItem.vue / ChatSidebar.vue**
构建部署：`cd frontend && npm run build` → cp dist/assets/* → static/vue/ → 更新 index.html hash（或 `./deploy.sh`）

---

## 9. 热加载中心（hotreload.py）

| target | 内容 | 触发 |
|---|---|---|
| tools | 11 个工具模块 importlib.reload 重注册（快照失败回滚 + 外部注册回填保 MCP/scheduler）| watcher/API/HotReload 工具 |
| config | DEFAULT 合并重建 am.config | watcher(API/工具) |
| crew | reload_custom_agents（成员+部门）| 同上 |
| mcp | close→重连（显式指定才执行）| 仅 API/tool 显式 |

watcher：10s 轮询各 watch 文件 mtime，变更自动重载对应目标。

---

## 10. API 一览（29 端点）

```
对话     POST /api/chat/start · GET /api/chat/events · GET /api/chat/status · POST /api/chat/stop
         GET  /api/chat/history · /api/chat/messages · POST /api/chat/delete
          GET,POST /api/chat (旧同步) · /api/agent/run
组织     GET /api/org · GET /api/crew/agents · POST /api/crew/save|delete|dept_save|dept_delete
         GET /api/agents（面板预置）
工具     GET /api/tools · POST /api/hotreload
技能     GET /api/skills · POST /api/skills/install|install_url|uninstall
定时     （引擎+工具已就绪；REST: 见 scheduler op_* 可按需暴露）
配置     GET,POST /api/config · GET /api/models
兼容     GET,POST /plugin（面板兼容格式）· GET,POST /sse_panel（旧SSE）
页面     GET /
```

---

## 11. 配置与数据文件

| 文件 | 说明 | 热载 |
|---|---|---|
| config.json | api_base_url/api_key/models/default_model/workspace/embedding/mcp/rag/agent… | ✅ watcher+API |
| crew_agents.json | departments[] + agents[]（自定义成员）| ✅ |
| scheduled_tasks.json | 定时任务 | ✅ 每 tick |
| tools_state.json / skills_state.yaml | 显示与启停状态 | ✅ |
| sessions/<id>/sessions.json | 会话历史（原子写；损坏自动备份 .corrupt）| — |
| logs/tool_audit.jsonl | 工具审计（5MB 轮转）| — |

环境变量：`AI_AGENT_RISK_GUARD`(off 关严格拦截) `AI_AGENT_TOOL_TIMEOUT` `AI_AGENT_SKILL_ALLOW_PRIVATE` `AI_AGENT_SCHEDULE_FILE` `AI_AGENT_TOOL_AUDIT` `AI_AGENT_CREW_AGENTS`

---

## 12. 部署运维

```bash
# 构建+部署前端
./deploy.sh
# 重启服务（改了 Python 后必须）
sudo systemctl restart bt-agent.service
# 无 sudo 替代（Restart=always）
kill $(systemctl show bt-agent.service -p MainPID --value)   # 5s 后自动拉起
```
日志：`journalctl -u bt-agent.service`（需权限）。健康检查：`curl :9876/api/config`。

---

## 13. 已知限制与路线图

- 无依赖工具并行执行（当前串行，需设计前端事件顺序）
- 熔断半开机制未实现
- 工具场景化自动裁剪（142 全下发致首 token 延迟偏高；可在设置隐藏冗余 MCP 缓解）
- hierarchical 模式为单层经理；多层嵌套待定
- 定时任务的 REST/UI 未接前端（工具已可用）
- `ai_agent_legacy_*` 已删除；`.server.pid` 为历史残留可忽略
