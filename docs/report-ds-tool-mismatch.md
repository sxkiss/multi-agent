# 调查报告：DS 系列模型「tool calls and tool results do not match」（11148）

> 日期：2026-09-25 | 状态：**仅调查，未修复** | 影响模式：单 Agent / 集团模式（共用同一 `Agent` 类，消息路径一致）

## 1. 现象

使用 DS（DeepSeek）系列模型时，对话报错并提示"工具记录不完整，请新建任务"，
上游网关返回 code `11148` / `tool calls and tool results do not match`。
GPT/Claude 系模型较少出现——**DS 对消息序列的校验更严格**（OpenAI 官方接口对
悬空 tool_calls 有一定容忍度，DS 网关则直接 400）。

## 2. 结论（一句话）

**会话历史中残留了"已声明但无结果"的 tool_calls（悬空工具调用），
`_sanitize_messages_for_api` 只清理非法 JSON、不补全缺失结果，
悬空声明被原样发给上游 API → DS 网关严格校验 → 400（11148）。**

## 3. 证据链

### 3.1 实证扫描（全量 77 个会话）

模拟真实发送路径（`MemoryManager.get_sliding_window()` → `_build_messages` 重建 → `_sanitize_messages_for_api`）后扫描全部会话：

| 项目 | 结果 |
|---|---|
| 有效会话 | 77 |
| 存在悬空 tool_calls 的会话 | 1（`1789831265425`，窗口 788 条中 1 个悬空） |
| sanitize 正常拦截的截断 JSON 用例 | 多处（工作正常，非问题源） |

### 3.2 悬空调用的确切内容（决定性证据）

`1789831265425` 会话窗口位置 [195] 的悬空 tool_call：

```json
{"command": "OLD=$(systemctl show bt-agent.service -p MainPID --value); ...\nkill $OLD 2>/dev/null && echo \"已发送 SIGTERM\"\n..."}
```

**这是 agent 用 Bash 工具执行"杀掉 bt-agent 服务进程触发 systemd 自动重启"的命令。**

因果链：
1. `agent.py:792` 先把 assistant 消息（含 tool_calls 声明）落库
2. 接着执行工具（`agent.py:804+`）→ 执行 `kill <自己的 MainPID>`
3. **进程被杀死 → 后续 `add_message("tool", ...)`（agent.py:942）永远执行不到**
4. 历史留下"声明 1 个工具、0 个结果"的悬空记录
5. 下一轮请求：`_sanitize` 的 `all_tc_ids` 预扫描发现该 ID 存在 → 保留声明
6. 但没有任何 tool 结果与之配对 → **API 400**

同类触发场景（所有"声明后、回填前进程中断"的情形）：
- 工具命令杀死/重启自身进程（如本例 kill MainPID）
- `systemctl restart` / `pkill -f` 波及自身
- 进程 OOM、崩溃、被外部 kill
- 服务部署重启发生在工具执行间隙

### 3.3 辅助缺陷：空参数 tool_call 被提前丢弃

`agent.py:1361`（`_build_messages` 重建历史时）：

```python
if name and args:        # ← 空 args 的 tool_call 被整个丢弃
    tc.append(c)
```

- 无参数工具（如 `get_system_resources`）声明 `arguments=""` 时，
  **tool_calls 被静默丢弃**，其后的 tool 结果因"无对应声明"也被 sanitize 跳过
- 更矛盾的是：`_sanitize_messages_for_api:1047-1054` 明明有
  "空参数 → 替换为 `{}`，保留 tool_call"的正确逻辑，但**永远走不到**
  （被前面的 `if name and args` 提前拦截）
- 影响：无参数工具的历史记录整段消失（对模型上下文是信息丢失，
  但不直接产生 400；属于**同一处的第二重缺陷**）

### 3.4 无效重试放大问题

`chat_client/api_retry.py:38`：

```python
RETRYABLE_STATUS_CODES = (400, 408, 409, 425, 429, 499, 500, 502, 503, 504)
```

- 400 被列入可重试（注释说明是"代理常见瞬时错误"）
- 但上述 400 是**消息内容非法**导致，**重试必然再次失败**
- 后果：白等 3 次退避重试（默认 `api_max_retry=3`）才把错误抛给用户，
  用户等待时间被放大；且错误提示"直接重试无效"与代码行为自相矛盾

### 3.5 防护缺口的位置

`_mark_unexecuted_tools`（agent.py:990）**只覆盖两个场景**：

| 调用点 | 场景 |
|---|---|
| agent.py:807 | 工具执行前检测到 `_is_cancelled()`（用户点停止） |
| agent.py:881 | 参数连续非法触发熔断 |

**未覆盖**：进程被杀（本例）、崩溃、断电、服务重启等"来不及执行任何收尾逻辑"
的场景——因为进程已死，任何补偿代码都无机会运行。

## 4. 根因归类

| # | 缺陷 | 位置 | 严重度 |
|---|---|---|---|
| 1 | 悬空 tool_calls 无兜底：进程中断时"声明已落库、结果未回填"，`_sanitize` 只清 JSON 不补结果 | agent.py:1014-1086 | **高（直接 400）** |
| 2 | 请求前无"配对自检"：发送前未校验 `tool_calls` 与 `tool` 结果一一匹配 | agent.py:645（sanitize 调用点） | **高** |
| 3 | `_build_messages` 丢弃空参数 tool_call，与 sanitize 的修复逻辑矛盾 | agent.py:1361 | 中 |
| 4 | 400 被误列可重试 → 无效重试、延迟报错 | api_retry.py:38 | 中 |
| 5 | 进程内写库顺序（先声明后执行）在"进程被杀"场景下必然产生悬空 | agent.py:792/942 | 中（架构层面，需兜底而非改顺序） |

## 5. 建议修复方向（待确认后再动手）

1. **请求前配对自检（推荐，最小侵入）**：在 `_sanitize_messages_for_api` 末尾增加
   一步：对"已声明但无结果"的 tool_call，**在内存中补一条合成 tool 结果**
   （内容如 `<toolcall_status>interrupted</toolcall_status>`），保证发给 API 的
   序列自洽。落库与否可另行决定（不落库也可，仅请求期修复）。
2. **`_build_messages` 空参数修复**：`if name and args` → `if name`（空 args 交给
   sanitize 的 `{}` 替换逻辑），与既有修复逻辑对齐。
3. **400 重试策略**：把 400 从 `RETRYABLE_STATUS_CODES` 移除，或仅在错误信息
   匹配"瞬时/代理"特征时可重试；消息非法类 400 直接失败并给出"新建任务"引导。
4. **可选**：在 `_mark_unexecuted_tools` 之外，把"启动时扫描末轮悬空声明"作为
   兜底（进程重启后修复历史）。

## 6. 影响范围与复现条件

- **触发条件**：任一轮"assistant 声明 tool_calls 后、tool 结果回填前"进程中断
  - 高危诱因：让 agent 执行 `kill/restart` 自身服务的命令（本例实证）
- **影响模式**：单 Agent 与集团模式**共用** `Agent` 类与同一消息路径，
  理论上都会触发；DS 模型因校验严格率先暴露
- **跨会话残留**：悬空记录**永久留在历史**里，后续每次请求（窗口包含该段时）
  都会 400 → 用户只能"新建任务"（这正是错误提示的由来）

## 7. 附：数据统计

- 扫描会话：77 个有效会话（`sessions.json` 快照 + `sessions.journal.jsonl` 合并视图）
- 悬空会话：1 个（`1789831265425`，窗口内 1 处悬空声明）
- sanitize 正常拦截的非法 JSON：多处（现有防护对"截断 JSON"有效）
- 注：早期单看 journal 的扫描会误报"孤儿 tool 结果"，原因是 journal 只是增量日志、
  真实历史需与快照合并（`memory.py:_load_session_locked`）；**结论以合并视图为准**
