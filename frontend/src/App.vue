<template>
  <div class="ai-chat-container" ref="containerRef">
    <ChatSidebar
      :conversations="conversations"
      :current-conversation-id="currentConversationId"
      :is-sending="isSending"
      :api-configured="!!(globalConfig.api_base_url && globalConfig.api_key)"
      :get-time-ago="getTimeAgo"
      @create-new="createNewConversation"
      @load-conversation="loadConversation"
      @delete-conversation="deleteConversation"
      @delete-conversations="deleteConversations"
      @open-settings="openSettingsDrawer"
      @toggle-fullscreen="toggleFullscreen"
    />
    <div class="chat-main-area">
      <ChatMain
        ref="chatMainRef"
        :messages="currentMessages"
        :is-typing="isTyping"
        :streaming-content-blocks="streamingContentBlocks"
        :streaming-think-collapsed="streamingThinkCollapsed"
        :suggestions="globalConfig.questions || []"
        @update:streaming-think-collapsed="streamingThinkCollapsed = $event"
        @suggestion-click="handleSuggestionClick"
        @regenerate="regenerateMessage"
        @delete-message="handleDeleteMessage"
        @send-continue="handleSendContinue"
      />
      <!-- 任务执行状态栏 -->
      <div v-if="isSending" class="task-status-bar">
        <span class="task-status-dot"></span>
        <span class="task-status-text">任务正在执行，耗时 {{ elapsedSeconds }} 秒</span>
        <button class="task-status-cancel" @click="stopMessage">取消</button>
      </div>
<!-- 消息发送队列：显示在输入框上方 -->
      <div v-if="messageQueue.length > 0" class="msg-queue">
        <div class="msg-queue-header">
          <span class="msg-queue-title">待发送队列（{{ messageQueue.length }}）</span>
          <span v-if="isSending" class="msg-queue-tip">回复完成后自动依次发送</span>
          <button class="msg-queue-clear" @click="clearQueue">全部取消</button>
        </div>
        <div class="msg-queue-items">
          <div
            v-for="q in messageQueue"
            :key="q.id"
            class="msg-queue-item"
            :class="{ inactive: q.sessionId && q.sessionId !== currentConversationId }"
          >
            <span v-if="q.sessionId && q.sessionId !== currentConversationId" class="msg-queue-session">{{ sessionLabel(q.sessionId) }}</span>
            <span class="msg-queue-text">{{ q.text }}</span>
            <button class="msg-queue-cancel" title="取消该条" @click="removeFromQueue(q.id)">&times;</button>
          </div>
        </div>
      </div>
    <!-- 工作模式切换：集团模式 / opencode / claude -->
    <div class="mode-switch">
      <button type="button" :class="{ active: chatMode === 'group' }" @click="chatMode = 'group'">集团模式</button>
      <button type="button" :class="{ active: chatMode === 'opencode' }" @click="onOpencodeMode">opencode</button>
      <button type="button" :class="{ active: chatMode === 'claude' }" @click="chatMode = 'claude'">claude</button>
      <button type="button" :class="{ active: chatMode === 'single' }" @click="chatMode = 'single'">单 Agent</button>
    </div>
    <!-- 模式状态指示器 -->
    <div class="mode-status-bar">
      <span class="mode-badge" :class="chatMode">
        {{ chatMode === 'group' ? '集团' : chatMode }}
      </span>
      <span class="mode-info">
        {{ chatMode === 'group' ? 'AGENTS: 全局 + 项目' : chatMode === 'single' ? 'Agent 直连 · 全套工具' : `${chatMode} CLI` }}
      </span>
      <span class="mode-info">Skills: {{ skillsCount }} 个可用</span>
    </div>
    <!-- opencode / claude / 单 Agent 模式：目录 / 提示词模板 / 自定义指令 -->
    <div class="codex-bar" v-if="['opencode', 'claude', 'single'].includes(chatMode)">
      <div class="codex-field">
        <label>工作目录</label>
        <input v-model="ocWorkspace" placeholder="填入目标项目路径，如 /path/to/project" @change="onWorkspaceChange">
        <span class="small" v-if="ocWorkspace">{{ ocWorkspace }}</span>
        <span class="small" v-else>留空则使用默认目录</span>
      </div>
      <div class="codex-field">
        <label>提示词模板</label>
        <select v-model="ocTemplate">
          <option v-for="t in ocTemplateOptions" :key="t.value" :value="t.value">{{ t.label }}</option>
        </select>
      </div>
      <details class="codex-instr">
        <summary>自定义系统提示词（可选，覆盖模板）</summary>
        <textarea v-model="ocInstructions" placeholder="留空则使用所选模板；填写则作为系统提示词完全自定义"></textarea>
      </details>
      <a class="codex-manage" href="/static/opencode_panel.html" target="_blank">管理面板</a>
    </div>
    <ChatInput
      ref="chatInputRef"
        v-model:input-message="inputMessage"
        :show-model-popover="showModelPopover"
        v-model:show-model-popover="showModelPopover"
        :show-tool-popover="showToolPopover"
        v-model:show-tool-popover="showToolPopover"
        :web-search-enabled="webSearchEnabled"
        v-model:web-search-enabled="webSearchEnabled"
        :selected-tools="selectedTools"
        :models-list="modelOptions"
        :current-model="currentModel"
        :tools-list="toolsList"
        :models-loading="modelsLoading"
        :tools-loading="toolsLoading"
        :is-sending="isSending"
        @send-message="sendMessage"
        @stop-message="stopMessage"
        @remove-selected-tool="removeSelectedTool"
        @select-model="selectModel"
        @toggle-tool-selection="toggleToolSelection"
        @batch-toggle-tools="batchToggleTools"
        @open-settings="openSettingsDrawer"
      />
      <!-- 集团实时运行状态网（单 Agent 模式隐藏） -->
      <div class="org-live-panel" v-if="orgLoaded && chatMode === 'group'">
        <div class="olp-header">
            <span class="olp-pulse" :class="{ active: hasRunningMember }"></span>
            <span class="olp-label">集团指挥中心</span>
            <span v-if="hasRunningMember" class="olp-running-count">{{ runningCount }} 个任务执行中</span>
          </div>
        <div class="olp-depts">
          <div v-for="dep in orgData.departments" :key="dep.name"
              class="olp-dept" :class="{ active: deptRunning(dep.name) }">
            <div class="olp-dept-header">{{ dep.title }}</div>
            <div class="olp-dept-members">
              <div v-for="m in dep.members" :key="dep.name + m.name"
                  class="olp-member" :class="getMemberStatus(m.name)" :title="m.description">
                  <span class="olp-m-dot" :class="getMemberStatus(m.name)"></span>
                  <span class="olp-m-name">{{ m.name }}</span>
                  <span v-if="getMemberStatus(m.name) === 'running'" class="olp-m-gear">⚙</span>
                  <span v-else-if="getMemberStatus(m.name) === 'done'" class="olp-m-check">✓</span>
                </div>
              <div v-if="dep.members.length === 0" class="olp-m-empty">虚位以待</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    <SettingsDrawer
      :visible="settingsDrawerVisible"
      :config="globalConfig"
      @close="closeSettingsDrawer"
      @save="handleSaveConfig"
    />
  </div>
</template>

<script setup>
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import ChatSidebar from './components/ChatSidebar.vue'
import ChatMain from './components/ChatMain.vue'
import ChatInput from './components/ChatInput.vue'
import SettingsDrawer from './components/SettingsDrawer.vue'

// ==================== 独立部署 API 封装（不依赖宝塔面板 window.ai_tools）====================
// 将原本走 /plugin?action=a&name=ai_agent&s=xxx 的面板代理请求，统一改为标准 REST /api/* 调用。
// 解析 JSON 后以 {status, data, msg} 结构回调，兼容原 ai_tools.send 的回调语义。
async function apiCall(method, url, body, cb) {
  try {
    const opts = { method, headers: { 'Content-Type': 'application/json' } }
    if (body && method.toUpperCase() !== 'GET') opts.body = JSON.stringify(body)
    const r = await fetch(url, opts)
    let res = {}
    try { res = await r.json() } catch { res = { status: false, msg: '响应解析失败' } }
    cb && cb(res)
  } catch (e) {
    cb && cb({ status: false, msg: String(e && e.message || e) })
  }
}
function apiGet(url, cb)  { return apiCall('GET', url, null, cb) }
function apiPost(url, body, cb) { return apiCall('POST', url, body, cb) }

// ==================== State ====================
const inputMessage = ref('')
const currentMessages = ref([])
const conversations = ref([])
const currentConversationId = ref(null)
const isTyping = ref(false)
const isSending = ref(false)
const streamingContentBlocks = ref([])
const streamingThinkCollapsed = ref(false)
const modelsList = ref([])
const skillsCount = ref(0)
const agentsMdStatus = ref('')
const currentModel = ref(null)
const CURRENT_MODEL_KEY = 'ai_current_model'

// 从后端配置校准当前模型：本地选择 > default_model > 列表第一个；本地选择已不在配置列表中则回退
function applyConfiguredModel(cfg) {
  const list = Array.isArray(cfg?.models) ? cfg.models.filter(Boolean) : []
  if (!list.length) return
  const saved = localStorage.getItem(CURRENT_MODEL_KEY)
  const candidate =
    (saved && list.includes(saved) && saved) ||
    (cfg?.default_model && list.includes(cfg.default_model) && cfg.default_model) ||
    list[0]
  if (candidate) currentModel.value = candidate
}

// 模型下拉选项：优先展示「设置中勾选的模型」，为空时回退服务商动态列表
// 实时状态按部门分组
const groupedMemberStatus = computed(() => {
  const groups = {}
  for (const [name, info] of Object.entries(memberStatus.value)) {
    const dn = info.dept || '综合部'
    if (!groups[dn]) groups[dn] = []
    groups[dn].push({ name, ...info })
  }
  return groups
})

const hasRunningMember = computed(() =>
  Object.values(memberStatus.value).some(m => m.status === 'running')
)
const runningCount = computed(() =>
  Object.values(memberStatus.value).filter(m => m.status === 'running').length
)

function deptRunning(deptName) {
  return orgData.value.departments
    .find(d => d.name === deptName)?.members
    ?.some(m => getMemberStatus(m.name) === 'running') || false
}

function getMemberStatus(memberName) {
  return memberStatus.value[memberName]?.status || 'idle'
}

const modelOptions = computed(() => {
  const cfgList = (globalConfig.value.models || []).filter(Boolean)
  const provider = (modelsList.value || []).map(m => (typeof m === 'string' ? m : m?.id)).filter(Boolean)
  return [...new Set([...cfgList, ...provider])]
})
const toolsList = ref([])
const selectedTools = ref([])
// 事件流游标：标签页恢复时用于续播未播完的事件
let lastSeenEventId = -1
let pendingReconnect = false
// 工作模式：group=集团模式（经理只编排）/ opencode=opencode CLI 模式（可配提示词模板）
// 默认集团模式，避免 localStorage 残留 opencode/claude/single 导致集团模式失灵
const chatMode = ref((localStorage.getItem('ai_chat_mode') || 'group') === 'group' ? 'group' : localStorage.getItem('ai_chat_mode'))
watch(chatMode, (v) => { try { if (v) localStorage.setItem('ai_chat_mode', v) } catch (e) {} })

// opencode 模式状态：工作目录 / 提示词模板 / 自定义指令
const ocWorkspace = ref(localStorage.getItem('ai_oc_workspace') || '')
const ocTemplate = ref('')
const ocInstructions = ref('')
const ocBuiltinTemplates = ref([])
const ocConfig = ref({ templates: {}, default_template: '' })
const ocTemplateOptions = computed(() => {
  const none = [{ value: '__none__', label: '无提示词' }]
  const builtin = (ocBuiltinTemplates.value || []).map(t => ({ value: t, label: '内置:' + t }))
  const custom = Object.keys(ocConfig.value.templates || {}).map(t => ({ value: t, label: '自定义:' + t }))
  return [...none, ...builtin, ...custom]
})
watch(ocWorkspace, (v) => { try { localStorage.setItem('ai_oc_workspace', v) } catch (e) {} })

function onWorkspaceChange() {
  // 目录切换后重新拉取该目录下的历史会话
  loadConversations()
}

async function loadOpencodeConfig() {
  try {
    const r = await fetch('/api/opencode/config')
    const res = await r.json()
    if (res.status) {
      ocConfig.value = res.data.opencode || { templates: {}, default_template: '' }
      ocBuiltinTemplates.value = res.data.builtin_templates || []
      if (!ocTemplate.value) ocTemplate.value = ocConfig.value.default_template || '__none__'
    }
  } catch (e) { console.warn('加载 opencode 配置失败:', e) }
}

function onOpencodeMode() {
  chatMode.value = 'opencode'
  loadOpencodeConfig()
}

// 页面加载时如果已是 opencode/claude/single 模式，立即加载配置
if (['opencode', 'claude', 'single'].includes(chatMode.value)) {
  loadOpencodeConfig()
}
watch(chatMode, (v) => { if (['opencode', 'claude', 'single'].includes(v) && !ocBuiltinTemplates.value.length) loadOpencodeConfig() })
const showModelPopover = ref(false)
const showToolPopover = ref(false)
const modelsLoading = ref(false)
const toolsLoading = ref(false)
const webSearchEnabled = ref(true)
const containerRef = ref(null)
const chatMainRef = ref(null)
const chatInputRef = ref(null)
const settingsDrawerVisible = ref(false)

// ==================== 消息发送队列 ====================
// 发送中再输入的消息进入队列，显示在输入框上方；可单条取消/清空；
// 持久化到 localStorage，刷新不丢失；当前回复完成后自动按序发送。
const messageQueue = ref([])
// 实时成员运行状态（从 crew_step 事件更新）
const memberStatus = ref({})   // {memberName: {status, dept, task, ts}}
const orgData = ref({ departments: [], manager: {} })
const orgLoaded = ref(false)

async function fetchOrgData() {
  try {
    const r = await fetch('/api/org')
    const res = await r.json()
    if (res.status && Array.isArray(res.data?.departments)) {
      orgData.value = res.data
      orgLoaded.value = true
    }
  } catch {}
}

const QUEUE_KEY = 'ai_message_queue'
const queueStarting = ref(false)

function saveQueue() {
  try {
    localStorage.setItem(QUEUE_KEY, JSON.stringify(messageQueue.value))
  } catch {}
}

function restoreQueue() {
  try {
    const raw = localStorage.getItem(QUEUE_KEY)
    if (raw) {
      const arr = JSON.parse(raw)
      if (Array.isArray(arr)) messageQueue.value = arr.filter(q => q && q.id && typeof q.text === 'string')
    }
  } catch {}
}

function enqueueMessage(text) {
  const t = String(text || '').trim()
  if (!t) return
  messageQueue.value.push({
    id: Date.now().toString() + Math.random().toString(36).slice(2, 6),
    sessionId: currentConversationId.value || '',
    text: t,
    time: new Date().toISOString(),
  })
  saveQueue()
}

function removeFromQueue(id) {
  messageQueue.value = messageQueue.value.filter(q => q.id !== id)
  saveQueue()
}

function clearQueue() {
  messageQueue.value = []
  saveQueue()
}

function sessionLabel(sessionId) {
  const c = conversations.value.find(c => c.id === sessionId)
  if (c && c.title) return c.title.slice(0, 12)
  if (sessionId === currentConversationId.value && currentMessages.value.length) {
    const firstUser = currentMessages.value.find(m => m.role === 'user')
    if (firstUser) {
      const c0 = Array.isArray(firstUser.content) ? (firstUser.content.find(i => i.type === 'text')?.text || '') : firstUser.content
      if (typeof c0 === 'string' && c0) return c0.slice(0, 12)
    }
  }
  return sessionId ? String(sessionId).slice(0, 8) : '新对话'
}

// 出队：仅当空闲且队首存在属于当前会话的待发消息时自动发送
// 初始恢复是否完成（会话/运行中任务状态确认后）才允许自动出队，避免刷新瞬间与恢复逻辑竞态
let initialRestoreDone = false
let queueRetryCount = 0

function markInitialRestoreDone() {
  initialRestoreDone = true
}

function processQueue() {
  if (!initialRestoreDone || isSending.value || queueStarting.value) return
  const sid = currentConversationId.value
  const qIdx = messageQueue.value.findIndex(q => q.sessionId === sid)
  if (qIdx === -1) return
  const item = messageQueue.value.splice(qIdx, 1)[0]
  saveQueue()
  nextTick(() => {
    inputMessage.value = item.text
    sendMessage(item.text, qIdx)
  })
}

// 启动被拒（如"已有任务"）时把消息放回队列原位置并延时重试；超过上限则提示放弃
function requeueFailedStart(text, queueIndex, reason) {
  queueRetryCount++
  const item = {
    id: Date.now().toString() + Math.random().toString(36).slice(2, 6),
    sessionId: currentConversationId.value || '',
    text,
    time: new Date().toISOString(),
    _retry: queueRetryCount,
  }
  const at = typeof queueIndex === 'number' && queueIndex >= 0
    ? Math.min(queueIndex, messageQueue.value.length)
    : messageQueue.value.length
  messageQueue.value.splice(at, 0, item)
  saveQueue()
  console.warn(`[队列] 发送被拒（${reason}），已放回队列，2.5s 后重试（第 ${queueRetryCount} 次）`)
  setTimeout(processQueue, 2500)
}
const globalConfig = ref({
  system_prompt: '',
  api_base_url: '',
  api_key: '',
  models: [],
  embedding: {
    embedding_api_key: '',
    embedding_base_url: '',
    embedding_model_name: ''
  },
  rag: {
    sliding_window_size: 10,
    rag_trigger_threshold: 3,
    rag_retrieval_count: 10,
    rag_final_count: 5
  },
  context_window_kb: 512,
  workspace: '',
  global_kb_agent_id: 'ai-agent',
  mem0_api_url: 'http://localhost:8000',
  use_global_rag: false,
  use_external_kb: false,
  agent: {
    max_tool_iterations: 10,
    temperature: 1,
    top_p: 1,
    max_retries: 5
  },
  default_headers: {},
  questions: []
})

// 思考定时器
let thinkingTimer = null

// 任务耗时计时器：isSending 期间每秒更新，驱动状态栏显示
// 注意：此处不重置计数——归零由各发起点（发送/重新生成）显式设置，
// 断线恢复（resumeRunningJobIfAny）则以服务端返回的真实耗时为准
const elapsedSeconds = ref(0)
let taskTimerInterval = null

watch(isSending, (running) => {
  if (running) {
    if (!taskTimerInterval) {
      taskTimerInterval = setInterval(() => { elapsedSeconds.value++ }, 1000)
    }
  } else {
    if (taskTimerInterval) {
      clearInterval(taskTimerInterval)
      taskTimerInterval = null
    }
  }
})

// 还原后端 sse_pack 对字符串的转义（\n -> 真实换行等），避免 marked 渲染出字面 \n
function unescapeSSEString(str) {
  if (typeof str !== 'string') return str
  return str
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\r')
    .replace(/\\t/g, '\t')
}

// ==================== 后台任务式对话 ====================
// 对话在后端线程中运行，与前端连接解耦：
// - 发送消息 → POST /api/chat/start 立即返回
// - 前端通过 GET /api/chat/events 订阅事件流（可随时断开/刷新，任务不受影响）
// - 重进页面 → 查询 /api/chat/status，若任务进行中则从事件起点回放续播
let currentStreamController = null
let eventSourceInstance = null

// 通用 SSE 行处理器：缓冲跨 chunk 残余行；捕获 id:（断点续传）/ event: / data:
function createSSEProcessor(handlers) {
  let buffer = ''
  let currentEventType = null
  let lastEventId = -1
  let completed = false
  // 追踪当前事件是否处于未完整状态（如只有 id: 或 event: 行，等待后续 data:）
  let pendingEvent = null

  const safeComplete = () => {
    if (completed) return
    completed = true
    handlers.onComplete?.()
  }

  const processLine = (rawLine) => {
    const line = rawLine.replace(/\r$/, '')
    if (!line) {
      // 空行表示事件结束：先处理累积的 pending 事件数据，再重置
      if (pendingEvent && pendingEvent.dataLines.length > 0) {
        const ev = pendingEvent.event || currentEventType || 'message'
        const eventData = pendingEvent.dataLines.join('\n')
        switch (ev) {
          case 'message_think':
            handlers.onThink?.(unescapeSSEString(eventData))
            break
          case 'message':
            handlers.onChunk?.(unescapeSSEString(eventData))
            break
          case 'tool_call':
            try {
              const tcData = JSON.parse(eventData)
              handlers.onToolCall?.(tcData)
              if (tcData.tool === 'Task' && tcData.args) {
                try {
                  const args = typeof tcData.args === 'string' ? JSON.parse(tcData.args) : tcData.args
                  if (args.subagent_type) {
                    memberStatus.value = { ...memberStatus.value,
                      [args.subagent_type]: { status: 'running', dept: '', task: String(args.description||'').slice(0,60), ts: Date.now() }
                    }
                  }
                } catch {}
              }
            } catch (e) { console.error('parse tool_call:', e) }
            break
          case 'tool_result':
            try { handlers.onToolResult?.(JSON.parse(eventData)) } catch (e) { console.error('parse tool_result:', e) }
            break
          case 'meta_info':
            try { handlers.onMetaInfo?.(JSON.parse(eventData)) } catch (e) { console.error('parse meta_info:', e) }
            break
          case 'usage':
            break
          case 'crew_step':
            try {
              const d = JSON.parse(eventData)
              memberStatus.value = {
                ...memberStatus.value,
                [d.agent]: { status: d.status, dept: d.dept, task: d.task, ts: Date.now() }
              }
            } catch {}
            break
          case 'crew_plan':
            try {
              const d = JSON.parse(eventData)
              const init = {}
              for (const s of d.steps || []) {
                init[s.agent] = { status: 'pending', dept: s.dept, task: s.task, ts: Date.now() }
              }
              memberStatus.value = init
            } catch {}
            break
          default:
            break
        }
      }
      pendingEvent = null
      return
    }

    if (line.startsWith('id:')) {
      const n = parseInt(line.substring(3).trim(), 10)
      if (!Number.isNaN(n)) {
        lastEventId = n
        handlers.onEventId?.(n)
      }
      // id 行后可能还有 event/data，暂存 pending
      pendingEvent = { id: n, event: currentEventType, dataLines: [] }
      return
    }
    if (line.startsWith('event:')) {
      currentEventType = line.substring(6).trim()
      if (pendingEvent) pendingEvent.event = currentEventType
      return
    }
    if (!line.startsWith('data:')) return

    const data = line.substring(5).trim()
    // 累积 data 行（支持多行 data 事件）
    if (pendingEvent) {
      pendingEvent.dataLines.push(data)
      return
    }

    // 直接处理 data 事件
    const eventData = pendingEvent?.dataLines?.join('\n') || data
    switch (currentEventType || 'message') {
      case 'message_think':
        handlers.onThink?.(unescapeSSEString(eventData))
        break
      case 'message':
        handlers.onChunk?.(unescapeSSEString(eventData))
        break
      case 'tool_call':
        try {
          const tcData = JSON.parse(eventData)
          handlers.onToolCall?.(tcData)
          if (tcData.tool === 'Task' && tcData.args) {
            try {
              const args = typeof tcData.args === 'string' ? JSON.parse(tcData.args) : tcData.args
              if (args.subagent_type) {
                memberStatus.value = { ...memberStatus.value,
                  [args.subagent_type]: { status: 'running', dept: '', task: String(args.description||'').slice(0,60), ts: Date.now() }
                }
              }
            } catch {}
          }
        } catch (e) { console.error('parse tool_call:', e) }
        break
      case 'tool_result':
        try { handlers.onToolResult?.(JSON.parse(eventData)) } catch (e) { console.error('parse tool_result:', e) }
        break
      case 'meta_info':
        try { handlers.onMetaInfo?.(JSON.parse(eventData)) } catch (e) { console.error('parse meta_info:', e) }
        break
      case 'usage':
        break
      case 'crew_step':
        try {
          const d = JSON.parse(eventData)
          memberStatus.value = {
            ...memberStatus.value,
            [d.agent]: { status: d.status, dept: d.dept, task: d.task, ts: Date.now() }
          }
        } catch {}
        break
      case 'crew_plan':
        try {
          const d = JSON.parse(eventData)
          // 初始化所有成员为 pending
          const init = {}
          for (const s of d.steps || []) {
            init[s.agent] = { status: 'pending', dept: s.dept, task: s.task, ts: Date.now() }
          }
          memberStatus.value = init
        } catch {}
        break
      case 'message_end':
        safeComplete()
        break
      case 'error':
        handlers.onError?.(unescapeSSEString(eventData))
        break
    }
    currentEventType = null
    pendingEvent = null
  }

  return {
    push(text) {
      buffer += text
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''
      // 如果 buffer 非空且不以完整事件结束，保留以待下一块
      for (const line of lines) processLine(line)
    },
    flush() {
      if (buffer) {
        // 确保最后一行被处理
        processLine(buffer + '\n')
        buffer = ''
      }
      // 若仍有 pending 事件未完成，补充空 data 触发处理
      if (pendingEvent) {
        const eventData = pendingEvent.dataLines.join('\n')
        currentEventType = pendingEvent.event || 'message'
        switch (currentEventType) {
          case 'message_think': handlers.onThink?.(unescapeSSEString(eventData)); break
          case 'message': handlers.onChunk?.(unescapeSSEString(eventData)); break
          case 'tool_call':
            try {
              const tcData = JSON.parse(eventData)
              handlers.onToolCall?.(tcData)
            } catch {}
            break
          case 'tool_result':
            try { handlers.onToolResult?.(JSON.parse(eventData)) } catch {}
            break
          case 'meta_info':
            try { handlers.onMetaInfo?.(JSON.parse(eventData)) } catch {}
            break
          case 'compact_summary':
            try { handlers.onCompactSummary?.(JSON.parse(eventData)) } catch {}
            break
          case 'error': handlers.onError?.(unescapeSSEString(eventData)); break
        }
        pendingEvent = null
      }
      safeComplete()
    },
    getLastId: () => lastEventId,
  }
}

// 订阅某会话的事件流（回放 + 实时跟随）。仅本地断开，后端任务继续运行。
// 网络意外断开时自动重连续播（从已收到的 last_id 继续），最多 5 次。
async function followChatEvents(sessionId, fromLastId, handlers) {
  if (currentStreamController) {
    currentStreamController.abort()
    currentStreamController = null
  }

  const processor = createSSEProcessor(handlers)
  const MAX_RESUME = 5
  let resumeCount = 0
  let cursor = fromLastId

  while (true) {
    try {
      const response = await fetch(
        `/api/chat/events?session_id=${encodeURIComponent(sessionId)}&last_id=${cursor}`,
        {
          signal: (() => {
            const ac = new AbortController()
            currentStreamController = ac
            return ac.signal
          })(),
        }
      )
      if (!response.body) throw new Error('无法获取事件流')

      // 订阅成功即重置重连计数（每次断开都有完整重试预算）
      resumeCount = 0
      currentStreamController = null

      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      while (true) {
        const { done, value } = await reader.read()
        if (done) {
          processor.flush()
          return
        }
        processor.push(decoder.decode(value, { stream: true }))
        cursor = Math.max(cursor, processor.getLastId())
        lastSeenEventId = Math.max(lastSeenEventId, processor.getLastId())
      }
    } catch (err) {
      if (err.name === 'AbortError') {
        console.log('已断开事件流订阅（后端任务不受影响）')
        return
      }
      resumeCount++
      if (resumeCount > MAX_RESUME) {
        console.error('Event stream error:', err)
        handlers.onError?.('与后端事件流的连接中断，请刷新重试。')
        return
      }
      const waitMs = Math.min(1000 * resumeCount, 5000)
      console.warn(`事件流断开，${waitMs}ms 后第 ${resumeCount}/${MAX_RESUME} 次重连（续传自 id=${cursor}）`)
      await new Promise(r => setTimeout(r, waitMs))
    }
  }
}

// 启动后台聊天任务并订阅其事件流
async function getAIResponseStream(message, sessionId, model, tools, webSearch, onChunk, onComplete, onError, onThink, onToolCall, onToolResult, onMetaInfo, queueIndex = -1) {
  const isOpencode = ['opencode', 'claude', 'single'].includes(chatMode.value || 'group')
  const activeMode = (chatMode.value || 'group') === 'group' ? 'group' : (chatMode.value || 'group')
  const endpoint = '/api/chat/start'
  const payload = {
    message: typeof message === 'string' ? message : JSON.stringify(message),
    session_id: sessionId,
    mode: isOpencode ? activeMode : (chatMode.value || 'group'),
    ...(isOpencode
      ? {
          // claude/opencode 模式：走模型网关，提示词由模板/自定义决定，workspace 指定工作目录
          model: model || 'auto',
          ...(ocWorkspace.value.trim() ? { workspace: ocWorkspace.value.trim() } : {}),
          ...(ocInstructions.value.trim()
            ? { system_prompt: ocInstructions.value.trim() }
            : { template: ocTemplate.value || '__none__' }),
        }
      : {
          model,
          tools: Array.isArray(tools) ? tools.map(t => (t && t.id) || t).filter(Boolean) : [],
        }),
    ...(webSearch ? { web_search: true } : {}),
    // 传递工作目录（仅非 opencode/claude 模式使用全局 workspace，避免覆盖用户设置）
    ...((chatMode.value !== 'opencode' && chatMode.value !== 'claude' && globalConfig.value?.workspace)
      ? { workspace: globalConfig.value.workspace }
      : {})
  }

  queueStarting.value = true
  let startResult
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    startResult = await res.json()
  } catch (err) {
    queueStarting.value = false
    console.error('启动后台任务失败:', err)
    if (queueIndex >= 0) {
      const text = typeof message === 'string' ? message : (Array.isArray(message) ? (message.find(i => i.type === 'text')?.text || '') : String(message))
      requeueFailedStart(text, queueIndex, String(err && err.message || err))
      return
    }
    onError('抱歉,连接AI服务时出现错误,请稍后再试。')
    return
  }

  queueStarting.value = false
  if (!startResult.status) {
    const reason = startResult.msg || '启动任务失败'
    if (queueIndex >= 0 && reason.includes('已有任务')) {
      // 队列消息被拒（恢复竞态/重复触发）：放回原位置稍后重试，不打扰用户
      requeueFailedStart(
        typeof message === 'string'
          ? message
          : (Array.isArray(message)
              ? (message.find(i => i.type === 'text')?.text || '')
              : String(message)),
        queueIndex,
        reason,
      )
      return
    }
    if (queueIndex >= 0) {
      // 其他启动失败：同样回队保留内容，但直接提示用户
      requeueFailedStart(
        typeof message === 'string' ? message : String(message),
        queueIndex,
        reason,
      )
      onError(reason)
      return
    }
    onError(reason)
    return
  }
  queueRetryCount = 0

  // 从头订阅本任务的全部事件（含启动瞬间已产生的事件）
  await followChatEvents(sessionId, -1, {
    onChunk, onComplete, onError, onThink, onToolCall, onToolResult, onMetaInfo,
  })
}

// 断开本地事件流订阅（不影响后端任务）
function abortCurrentRequest() {
  if (currentStreamController) {
    currentStreamController.abort()
    currentStreamController = null
  }
}

// ==================== Conversation management ====================
const resetConversation = () => {
  currentConversationId.value = null
  currentMessages.value = []
  streamingContentBlocks.value = []
}

// saveConversation - updates local list only
function saveConversation() {
  if (!currentConversationId.value || currentMessages.value.length === 0) return
  const firstUserMsg = currentMessages.value.find(m => m.role === 'user')
  let title = '新对话'
  if (firstUserMsg) {
    if (typeof firstUserMsg.content === 'string') {
      title = firstUserMsg.content.substring(0, 30)
    } else if (Array.isArray(firstUserMsg.content)) {
      const textItem = firstUserMsg.content.find(item => item.type === 'text')
      title = textItem ? (textItem.text ? textItem.text.substring(0, 30) : '[图片消息]') : '[图片消息]'
    }
  }
  // 收集所有已完成（status=completed）的工具调用 ID，用于历史回放时恢复完成状态
  const completedToolIds = []
  for (const msg of currentMessages.value) {
    if (msg.contentBlocks) {
      for (const b of msg.contentBlocks) {
        if (b.type === 'tool_call' && b.status === 'completed') {
          completedToolIds.push(b.id)
        }
      }
    }
  }
  const convo = {
    id: currentConversationId.value,
    title,
    messages: currentMessages.value,
    completedToolIds: [...new Set(completedToolIds)],
    updatedAt: new Date().toISOString()
  }
  const existingIndex = conversations.value.findIndex(c => c.id === currentConversationId.value)
  if (existingIndex >= 0) {
    conversations.value[existingIndex] = convo
  } else {
    // 追加到末尾（新会话在底部），保持时间正序
    conversations.value.push(convo)
  }
}

function loadConversations() {
  const ws = ((chatMode.value === 'opencode' || chatMode.value === 'claude' || chatMode.value === 'single') && ocWorkspace.value.trim()) ? ocWorkspace.value.trim() : ''
  const url = '/api/chat/history' + (ws ? '?workspace=' + encodeURIComponent(ws) : '')
  apiGet(url, (result) => {
    if (result.status && result.data) {
      conversations.value = result.data.map(e => {
        // 从本地已缓存的消息中恢复工具完成状态，避免刷新后显示"未完成"
        const existing = conversations.value.find(c => c.id === e.session_id)
        const completedToolIds = (existing?.completedToolIds || [])
        return {
          id: e.session_id,
          title: Array.isArray(e.title)
            ? (e.title.find(t => t.type === 'text')?.text || '图片信息')
            : e.title,
          messages: [],
          workspace: e.workspace || '',
          source: e.source || '',
          is_group: !!e.is_group,
          completedToolIds,
          updatedAt: e.updated_at || '',
          timeStr: e.time_str || '',
        }
      })
    }
  })
}

// loadConversation 请求序号，防止快速切换会话时旧响应覆盖新会话
let loadConversationSeq = 0

function loadConversation(id) {
  // 仅本地断开旧订阅（后端任务不受影响）
  if (isSending.value) {
    abortCurrentRequest()
    clearThinkingTimer()
    isSending.value = false
  }
  streamingContentBlocks.value = []

  const seq = ++loadConversationSeq
  const idx = conversations.value.findIndex(c => c.id === id)
  // opencode / claude 会话：自动切换到对应模式并回填工作目录
  if (idx >= 0 && conversations.value[idx]) {
    const convo = conversations.value[idx]
    if (convo.workspace) {
      ocWorkspace.value = convo.workspace
    }
    // 注意：不根据会话 source 自动切换 chatMode —— 模式完全由顶部按钮控制，
    // 避免点开历史会话时被强制切回 opencode/claude/single，导致集团模式失灵。
    const src = convo.source || ''
    // 若 workspace 已回填或会话源为 opencode/claude/single，按当前模式+目录重查历史
    if (ocWorkspace.value.trim() || (src === 'opencode' || src === 'claude' || src === 'single')) {
      loadConversations()
    }
  }
  if (idx >= 0 && conversations.value[idx].messages.length > 0) {
    currentConversationId.value = id
    currentMessages.value = [...conversations.value[idx].messages]
    localStorage.setItem('ai_last_conversation_id', String(id))
    markInitialRestoreDone()
    resumeRunningJobIfAny(id, seq)
    processQueue()
    nextTick(() => { if (chatMainRef.value?.forceScrollToBottom) chatMainRef.value.forceScrollToBottom() })
    return
  }

  apiGet('/api/chat/messages?session_id=' + encodeURIComponent(id), (result) => {
    // 竞态保护：只应用最新一次请求的结果
    if (seq !== loadConversationSeq) return
    if (result.status && result.data) {
      currentConversationId.value = id
      const convoData = conversations.value.find(c => c.id === id)
      currentMessages.value = parseChatHistory(result.data, convoData?.completedToolIds || [])
      localStorage.setItem('ai_last_conversation_id', String(id))
      markInitialRestoreDone()
      resumeRunningJobIfAny(id, seq)
      processQueue()
      nextTick(() => { if (chatMainRef.value?.forceScrollToBottom) chatMainRef.value.forceScrollToBottom() })
    } else {
      markInitialRestoreDone()
      console.warn('加载会话历史失败:', result && result.msg)
      const tip = window.layer?.msg
      if (tip) tip(result?.msg || '加载会话历史失败', { icon: 2 })
    }
  })
}

// 重进页面时：若该会话的后台任务仍在运行，订阅事件流续播剩余输出
async function resumeRunningJobIfAny(sessionId, seq) {
  try {
    const res = await fetch(`/api/chat/status?session_id=${encodeURIComponent(sessionId)}`)
    const result = await res.json()
    if (!result.status || !result.data?.running) return
    if (seq !== loadConversationSeq || isSending.value) return

    // 以服务端记录的任务起始时间校准耗时（刷新不归零）
    // 优先取顶层 elapsed；兼容旧后端时回退到 jobs 中仍在运行任务的 elapsed
    const _st = result.data || {}
    const _jobs = Array.isArray(_st.jobs) ? _st.jobs : []
    const _running = _jobs.find(j => j && j.running)
    const _elapsed = _st.elapsed ?? (_running ? _running.elapsed : 0)
    elapsedSeconds.value = Math.max(0, parseInt(_elapsed ?? 0, 10) || 0)
    isSending.value = true
    streamingContentBlocks.value = []
    streamingThinkCollapsed.value = false
    resetThinkingTimer()
    markInitialRestoreDone()
    await followChatEvents(sessionId, -1, buildStreamHandlers(sessionId))
  } catch (e) {
    console.error('恢复后台任务失败:', e)
  }
}

function parseChatHistory(data, completedToolIds = []) {
  const MAX_DISPLAY_BYTES = 200 * 1024  // 200KB 显示上限
  const messages = []
  if (Array.isArray(data.chats)) {
    const msgs = []
    for (const row of data.chats) {
      if (row.role === 'user' || row.role === 'assistant') {
        const content = Array.isArray(row.content)
          ? row.content.find(c => c.type === 'text')?.text || ''
          : row.content
        msgs.push({ role: row.role, content, time: row.time || new Date().toISOString(), contentBlocks: [] })
      }
    }
    return _truncateHistory(msgs, MAX_DISPLAY_BYTES)
  }
  if (Array.isArray(data)) {
    for (const row of data) {
      if (row.role === 'tool' && row.tool_call_id && row.content) {
        const resultText = Array.isArray(row.content)
          ? (row.content.find(c => c.type === 'text')?.text || '')
          : (typeof row.content === 'string' ? row.content : JSON.stringify(row.content))
        // 从最近的 assistant 消息向前搜索匹配的 tool_call 块
        for (let mi = messages.length - 1; mi >= 0; mi--) {
          const msg = messages[mi]
          if (msg.role !== 'assistant' || !msg.contentBlocks) continue
          const tc = msg.contentBlocks.find(
            b => b.type === 'tool_call' && b.id === row.tool_call_id && b.status !== 'completed'
          )
          if (tc) {
            tc.result = resultText
            tc.status = 'completed'
            break
          }
        }
        continue
      }
      if (row.role === 'user' || row.role === 'assistant') {
        if (row.role === 'tool') continue
        const content = Array.isArray(row.content)
          ? (row.content.find(c => c.type === 'text')?.text || '')
          : row.content

        // 提取思维链（reasoning_content），与流式结构 {type:'think',content,collapsed} 保持一致
        let thinkText = ''
        if (row.role === 'assistant' && row.reasoning_content) {
          thinkText = typeof row.reasoning_content === 'string'
            ? row.reasoning_content
            : (Array.isArray(row.reasoning_content)
              ? row.reasoning_content.map(p => (p && p.type === 'text' ? (p.text || '') : '')).join('')
              : String(row.reasoning_content || ''))
        }

        // 重建 tool_call 块（assistant 带 tool_calls 时）
        if (row.role === 'assistant' && Array.isArray(row.tool_calls) && row.tool_calls.length > 0) {
          const blocks = []
          if (thinkText) blocks.push({ type: 'think', content: thinkText, collapsed: true })
          if (content) blocks.push({ type: 'message', content })
          for (const tc of row.tool_calls) {
            blocks.push({
              type: 'tool_call',
              tool: tc.function?.name || '',
              args: tc.function?.arguments || '',
              id: tc.id || '',
              status: completedToolIds.includes(tc.id) ? 'completed' : 'calling',
              collapsed: true,
            })
          }
          messages.push({ role: 'assistant', content: content || '', time: row.time || '', contentBlocks: blocks })
          continue
        }

        if (row.role === 'assistant' && (content || thinkText)) {
          const blocks = []
          if (thinkText) blocks.push({ type: 'think', content: thinkText, collapsed: true })
          if (content) blocks.push({ type: 'message', content })
          messages.push({ role: 'assistant', content, time: row.time || '', contentBlocks: blocks })
        } else {
          messages.push({ role: row.role, content, time: row.time || new Date().toISOString(), contentBlocks: [] })
        }
      }
    }
  }
  return _truncateHistory(messages, MAX_DISPLAY_BYTES)
}

/** 截断历史：从前面移除旧消息，保留总内容 ≤ maxBytes 的最新消息 */
function _truncateHistory(messages, maxBytes) {
  if (messages.length === 0) return messages
  let totalBytes = 0
  for (const m of messages) {
    totalBytes += (m.content || '').length * 2  // UTF-16 近似字节数
    if (m.contentBlocks) {
      for (const b of m.contentBlocks) {
        totalBytes += (b.content || '').length * 2
        totalBytes += (b.args || '').length * 2
        totalBytes += (b.result || '').length * 2
      }
    }
  }
  if (totalBytes <= maxBytes) return messages
  // 从前面开始移除，直到总大小 ≤ maxBytes
  let removed = 0
  while (removed < messages.length && totalBytes > maxBytes) {
    const m = messages[removed]
    let msgBytes = (m.content || '').length * 2
    if (m.contentBlocks) {
      for (const b of m.contentBlocks) {
        msgBytes += (b.content || '').length * 2
        msgBytes += (b.args || '').length * 2
        msgBytes += (b.result || '').length * 2
      }
    }
    totalBytes -= msgBytes
    removed++
  }
  if (removed > 0) {
    messages.splice(0, removed)
    const skipped = removed
    messages.unshift({
      role: 'system',
      content: `（已省略 ${skipped} 条早期消息，共 ${(totalBytes / 1024).toFixed(0)}KB）`,
      time: new Date().toISOString(),
      contentBlocks: []
    })
  }
  return messages
}

function deleteConversation(id) {
  if (!id) return
  if (!window.confirm('确定要删除这个对话吗?')) return
  apiPost('/api/chat/delete', { session_id: id }, (result) => {
    if (result.status) {
      conversations.value = conversations.value.filter(c => c.id !== id)
      if (currentConversationId.value === id) {
        resetConversation()
        localStorage.removeItem('ai_last_conversation_id')
      }
    } else if (window.layer) {
      window.layer.msg(result.msg || '删除失败', { icon: 2 })
    }
  })
}

function deleteConversations(ids) {
  if (!Array.isArray(ids) || ids.length === 0) return
  if (!window.confirm(`确定要删除选中的 ${ids.length} 个对话吗?`)) return
  apiPost('/api/chat/delete_batch', { session_ids: ids }, (result) => {
    if (result.status) {
      const removed = new Set(ids)
      conversations.value = conversations.value.filter(c => !removed.has(c.id))
      if (removed.has(currentConversationId.value)) {
        resetConversation()
        localStorage.removeItem('ai_last_conversation_id')
      }
      loadConversations()
      } else if (window.layer) {
        window.layer.msg(result.msg || '批量删除失败', { icon: 2 })
      }
  })
}

function createNewConversation() {
  // 仅本地断开订阅（后端任务不受影响）
  if (isSending.value) {
    abortCurrentRequest()
    clearThinkingTimer()
    isSending.value = false
  }
  resetConversation()
  inputMessage.value = ''
  streamingContentBlocks.value = []
  localStorage.removeItem('ai_last_conversation_id')
}

function getTimeAgo(time) {
  if (!time) return ''
  const now = new Date()
  const date = new Date(time)
  const diff = Math.floor((now.getTime() - date.getTime()) / 1000)
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`
  if (diff < 604800) return `${Math.floor(diff / 86400)}天前`
  return `${date.getMonth() + 1}月${date.getDate()}日`
}

// ==================== Thinking timer ====================
function resetThinkingTimer() {
  isTyping.value = false
  if (thinkingTimer) {
    clearTimeout(thinkingTimer)
    thinkingTimer = null
  }
  thinkingTimer = setTimeout(() => {
    if (isSending.value) {
      isTyping.value = true
    }
  }, 3000)
}

function clearThinkingTimer() {
  if (thinkingTimer) {
    clearTimeout(thinkingTimer)
    thinkingTimer = null
  }
  isTyping.value = false
}

// ==================== 流式 UI 回调工厂 ====================
// 发送 / 重新生成 / 断线重连续播 共用同一套事件处理逻辑
function buildStreamHandlers(sessionId) {
  // 会话隔离：只有当 handler 绑定的 sessionId 匹配当前查看的会话时，才操作 UI 状态
  const isMine = () => sessionId === currentConversationId.value
  return {
    onChunk: (chunk) => {
      if (!isMine()) return
      resetThinkingTimer()
      const blocks = streamingContentBlocks.value
      // If thinking block exists, set collapsed flag
      if (blocks.some(b => b.type === 'think')) {
        streamingThinkCollapsed.value = true
      }
      const lastBlock = blocks[blocks.length - 1]
      if (lastBlock && lastBlock.type === 'message') {
        lastBlock.content += chunk
      } else {
        blocks.push({ type: 'message', content: chunk })
      }
      scrollToBottom()
    },
    onComplete: () => {
      if (!isMine()) return
      clearThinkingTimer()
      const blocks = streamingContentBlocks.value
      // Collapse thinking blocks
      blocks.forEach(b => {
        if (b.type === 'think') b.collapsed = true
      })
      streamingThinkCollapsed.value = true

      // Push assistant message with the blocks
      currentMessages.value.push({
        role: 'assistant',
        content: blocks.filter(b => b.type === 'message').map(b => b.content).join(''),
        time: new Date().toISOString(),
        contentBlocks: [...blocks]
      })
      streamingContentBlocks.value = []
      isSending.value = false
      saveConversation()
      updateQuota()
      processQueue()
    },
    onError: (error) => {
      if (!isMine()) return
      clearThinkingTimer()
      const blocks = streamingContentBlocks.value
      blocks.push({ type: 'message', content: error })
      currentMessages.value.push({
        role: 'assistant',
        content: error,
        time: new Date().toISOString(),
        contentBlocks: [...blocks]
      })
      streamingContentBlocks.value = []
      isSending.value = false
      saveConversation()
      updateQuota()
      processQueue()
    },
    onThink: (thinkContent) => {
      if (!isMine()) return
      resetThinkingTimer()
      const blocks = streamingContentBlocks.value
      const lastBlock = blocks[blocks.length - 1]
      if (lastBlock && lastBlock.type === 'think') {
        lastBlock.content += thinkContent
      } else {
        // Collapse previous think if exists
        blocks.forEach(b => {
          if (b.type === 'think') b.collapsed = true
        })
        blocks.push({ type: 'think', content: thinkContent, collapsed: false })
      }
      streamingThinkCollapsed.value = false
      scrollToBottom()
    },
    onToolCall: (toolData) => {
      if (!isMine()) return
      resetThinkingTimer()
      const blocks = streamingContentBlocks.value
      // 兼容后端字段：chunk 用 "tool"（部分实现用 "name"）
      const { id, args } = toolData
      const toolName = toolData.name || toolData.tool || ''
      let parsedArgs = ''
      try {
        const parsed = JSON.parse(args)
        parsedArgs = typeof parsed === 'string' ? parsed : JSON.stringify(parsed, null, 2)
      } catch {
        parsedArgs = args || ''
      }

      // TodoWrite special handling
      let todoItems = null
      try {
        const parsed = JSON.parse(args)
        if (parsed.todos && Array.isArray(parsed.todos)) {
          todoItems = parsed.todos
        }
      } catch {}

      // 替换语义：后端会先发占位事件(args="")，执行前再发完整参数事件。
      // 已存在同 id 块时用新参数整体替换，避免旧版"追加拼接"造成的 args 损坏。
      if (todoItems) {
        const existingIdx = blocks.findIndex(b => b.type === 'tool_call' && b.todoItems)
        if (existingIdx >= 0) {
          if (args) {
            blocks[existingIdx].args = args
            blocks[existingIdx].todoItems = todoItems
          }
          blocks[existingIdx].description = parsedArgs
          blocks[existingIdx].status = 'calling'
        } else {
          blocks.push({
            type: 'tool_call',
            tool: toolName,
            args: parsedArgs,
            id: id,
            status: 'calling',
            collapsed: true,
            description: parsedArgs,
            todoItems
          })
        }
      } else {
        const existingIdx = blocks.findIndex(b => b.type === 'tool_call' && b.id === id)
        if (existingIdx >= 0) {
          if (args && args.trim()) blocks[existingIdx].args = args
          blocks[existingIdx].status = 'calling'
        } else {
          blocks.push({
            type: 'tool_call',
            tool: toolName,
            args: parsedArgs,
            id: id,
            status: 'calling',
            collapsed: true
          })
        }
      }
      scrollToBottom()
    },
    onToolResult: (resultData) => {
      if (!isMine()) return
      const blocks = streamingContentBlocks.value
      const { id, result } = resultData
      const blockIdx = blocks.findIndex(b => b.type === 'tool_call' && b.id === id)
      if (blockIdx >= 0) {
        const block = blocks[blockIdx]
        block.result = result
        block.status = 'completed'
        // 保持折叠状态，不自动展开详情（默认折叠，点击 header 展开）
      }
    },
    onMetaInfo: (meta) => {
      if (!isMine()) return
      window.__temp_ai_msg_id = meta.ai_msg_id
      window.__temp_ai_msg_id_regen = meta.user_msg_id
      // The assistant message is pushed on completion; store the id for later use
      const lastMsg = currentMessages.value[currentMessages.value.length - 1]
      if (lastMsg && lastMsg.role === 'assistant') {
        lastMsg.id = meta.user_msg_id
      }
      // ── claude 模式续聊修复 ──
      // claude CLI 每次新建进程，真实会话 UUID 由后端生成并通过 meta_info 回传。
      // 必须把它写回 currentConversationId，否则下一轮 sendMessage 仍用旧的
      // Date.now() 字符串（非 UUID），后端判定 reuse_sid 为空 → 新建会话 → 上下文丢失。
      if (chatMode.value === 'claude' && meta.claude_session_id) {
        currentConversationId.value = meta.claude_session_id
        localStorage.setItem('ai_last_conversation_id', String(meta.claude_session_id))
      }
      // ── opencode 模式续聊修复 ──
      // opencode serve 生成真实 ses_ 前缀的会话 ID，通过 meta_info 回传。
      // 前端更新 currentConversationId，后续对话才能复用同一 opencode 会话。
      if (chatMode.value === 'opencode' && meta.opencode_session_id) {
        currentConversationId.value = meta.opencode_session_id
        localStorage.setItem('ai_last_conversation_id', String(meta.opencode_session_id))
      }
    },
    onCompactSummary: (data) => {
      if (!isMine()) return
      // 将压缩摘要作为 assistant 消息插入对话窗口
      currentMessages.value.push({
        id: data.msg_id,
        role: 'assistant',
        content: data.content,
        time: data.timestamp ? new Date(data.timestamp * 1000).toISOString() : new Date().toISOString(),
        contentBlocks: [{ type: 'compact_summary', content: data.content }]
      })
      scrollToBottom()
    },
  }
}

// ==================== Send message ====================
async function sendMessage(text, queueIndex = -1) {
  // Get pending attachments from ChatInput (unified: image/video/audio/file)
  const attachments = getPendingAttachments()

  // 集团模式：发送中不再入队，直接并行派发新任务（后端 MAX_PARALLEL=3）；单 Agent 模式不走此分支
  if (isSending.value && chatMode.value === 'group') {
    const t = String(text || '').trim()
    if (!t) return
    const sid = currentConversationId.value
    fetch('/api/chat/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        session_id: sid,
        message: t,
        model: currentModel.value || '',
        mode: chatMode.value || 'group',
      })
    }).then(r => r.json()).then(res => {
      if (res.status) {
        if (window.layer) window.layer.msg('任务已并行派发 ✓', { icon: 1 })
      } else {
        if (window.layer) window.layer.msg(res.msg || '派发失败', { icon: 2 })
      }
    }).catch(() => {
      if (window.layer) window.layer.msg('派发请求失败', { icon: 2 })
    })
    inputMessage.value = ''
    return
  }

  // Guard: no text and no pending attachments
  if ((!text && attachments.length === 0)) return

  // Generate session_id on first send
  if (!currentConversationId.value) {
    currentConversationId.value = Date.now().toString()
  }
  localStorage.setItem('ai_last_conversation_id', String(currentConversationId.value))

  elapsedSeconds.value = 0
  isSending.value = true
  inputMessage.value = ''
  // Clear pending attachments after collecting them
  if (chatInputRef.value && typeof chatInputRef.value.clearAll === 'function') {
    chatInputRef.value.clearAll()
  }
  streamingContentBlocks.value = []
  streamingThinkCollapsed.value = false

  // Build content array for the LLM + attachments for display
  const content = []
  const displayAttachments = []
  const pathRefs = []
  for (const att of attachments) {
    const url = att.url || att.previewUrl
    // 展示用附件（对话中显示图片/视频/音频/文件）
    if (url) displayAttachments.push({ kind: att.kind, name: att.name, url, path: att.path })

    if (att.path) {
      // 上传的资源：仅把服务器路径附加到消息，由 agent 自行按路径读取
      pathRefs.push(att.path)
    } else if (att.kind === 'image' && url && url.startsWith('data:')) {
      // 粘贴/截图的图片（无服务器路径）：内联 dataURL，供视觉模型直接读取
      content.push({ type: 'image_url', image_url: { url } })
    }
  }
  // 将附件路径作为文本写进消息，交给 agent 处理（不做任何转换/读取）
  if (pathRefs.length > 0) {
    text = (text ? text + '\n' : '') + pathRefs.map(p => '附件路径: ' + p).join('\n')
  }
  if (text) content.push({ type: 'text', text })

  // Push user message
  const userMsg = {
    role: 'user',
    content,
    attachments: displayAttachments,
    time: new Date().toISOString()
  }
  currentMessages.value.push(userMsg)

  clearThinkingTimer()
  elapsedSeconds.value = 0
  isSending.value = true

  // Note: assistant message is NOT pushed during streaming.
  // Streaming blocks render directly from streamingContentBlocks,
  // and the assistant message is pushed on completion/error/stop.

  const h = buildStreamHandlers(currentConversationId.value)
  await getAIResponseStream(
    content,
    currentConversationId.value,
    currentModel.value,
    selectedTools.value,
    webSearchEnabled.value,
    h.onChunk,
    h.onComplete,
    h.onError,
    h.onThink,
    h.onToolCall,
    h.onToolResult,
    h.onMetaInfo,
    queueIndex
  )
}

function stopMessage() {
  // 通知后端停止任务（后台线程被中断，与前端连接无关）
  const sid = currentConversationId.value
  if (sid) {
    fetch('/api/chat/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sid })
    }).catch(() => {})
  }
  abortCurrentRequest()
  clearThinkingTimer()
  const blocks = streamingContentBlocks.value
  if (blocks.length > 0) {
    const lastBlock = blocks[blocks.length - 1]
    lastBlock.collapsed = true
  }
  // Close think blocks
  blocks.forEach(b => {
    if (b.type === 'think') b.collapsed = true
  })
  streamingThinkCollapsed.value = true

  // Push the partial blocks as an assistant message
  if (blocks.length > 0) {
    currentMessages.value.push({
      role: 'assistant',
      content: blocks.filter(b => b.type === 'message').map(b => b.content).join(''),
      time: new Date().toISOString(),
      contentBlocks: [...blocks]
    })
  }
  streamingContentBlocks.value = []
  isSending.value = false
  saveConversation()
  updateQuota()
  processQueue()
}

function regenerateMessage(index) {
  // 守卫：流式进行中禁止再次触发，避免并发流互相干扰
  if (isSending.value) return
  // Find the user message before this assistant message
  let userContent = ''
  for (let i = index - 1; i >= 0; i--) {
    if (currentMessages.value[i].role === 'user') {
      userContent = currentMessages.value[i].content
      break
    }
  }
  if (!userContent) return

  // Remove the current assistant message
  currentMessages.value.splice(index, 1)
  elapsedSeconds.value = 0
  isSending.value = true
  streamingContentBlocks.value = []

  const sessionId = currentConversationId.value || Date.now().toString()
  currentConversationId.value = sessionId
  localStorage.setItem('ai_last_conversation_id', String(sessionId))

  // Assistant message is pushed on completion (same as sendMessage)

  const h = buildStreamHandlers(sessionId)
  getAIResponseStream(
    userContent,
    sessionId,
    currentModel.value,
    selectedTools.value,
    webSearchEnabled.value,
    h.onChunk,
    h.onComplete,
    h.onError,
    h.onThink,
    h.onToolCall,
    h.onToolResult,
    h.onMetaInfo
  )
}

function handleDeleteMessage(index) {
  if (!currentConversationId.value) return
  if (!window.confirm('确定要删除这条消息吗?')) return
  const msgId = currentMessages.value[index]?.id || ''
  apiPost('/api/chat/delete_message', { session_id: currentConversationId.value, id: msgId }, (result) => {
    if (result.status) {
      currentMessages.value.splice(index, 1)
    } else if (window.layer) {
      window.layer.msg(result.msg || '删除失败', { icon: 2 })
    }
  })
}

function handleSendContinue() {
  inputMessage.value = '继续'
  // Trigger send on next tick
  nextTick(() => sendMessage(inputMessage.value))
}

function handleSuggestionClick(suggestion) {
  if (typeof suggestion === 'string') {
    inputMessage.value = suggestion
  } else {
    inputMessage.value = suggestion.question || suggestion
    if (suggestion.tools && Array.isArray(suggestion.tools)) {
      selectedTools.value = suggestion.tools.map(t => ({ id: t }))
    }
  }
}

// ==================== Tools ====================
function getPendingAttachments() {
  // Get actual pending attachments from ChatInput component
  const input = chatInputRef.value
  if (input && typeof input.getPendingAttachments === 'function') {
    return input.getPendingAttachments()
  }
  return []
}

function removeSelectedTool(id) {
  selectedTools.value = selectedTools.value.filter(t => t.id !== id)
}

function selectModel(model) {
  const id = typeof model === 'string' ? model : model?.id
  if (!id) return
  currentModel.value = id
  localStorage.setItem(CURRENT_MODEL_KEY, id)
  showModelPopover.value = false
}

function toggleToolSelection(tool) {
  const idx = selectedTools.value.findIndex(t => t.id === tool.id)
  if (idx >= 0) {
    selectedTools.value.splice(idx, 1)
  } else {
    selectedTools.value.push(tool)
  }
}

function batchToggleTools(enabled) {
  if (enabled) {
    selectedTools.value = toolsList.value.map(t => ({ id: t.id }))
  } else {
    selectedTools.value = []
  }
}

function openSettingsDrawer() {
  settingsDrawerVisible.value = true
  loadTools()
}

function closeSettingsDrawer() {
  settingsDrawerVisible.value = false
  loadTools()
}

function handleSaveConfig(newConfig) {
  // 先关闭面板
  settingsDrawerVisible.value = false
  // 保存成功后重新从后端读取最新配置（后端可能合并了默认值）
  apiGet('/api/config', (result) => {
    if (result.status && result.data) {
      const e = result.data
      globalConfig.value = {
        system_prompt: e.config?.system_prompt || '',
        api_base_url: e.config?.api_base_url || '',
        api_key: e.config?.api_key || '',
        models: e.config?.models || [],
        embedding: {
          embedding_api_key: e.config?.embedding?.embedding_api_key || '',
          embedding_base_url: e.config?.embedding?.embedding_base_url || '',
          embedding_model_name: e.config?.embedding?.embedding_model_name || ''
        },
        rag: {
          sliding_window_size: e.config?.rag?.sliding_window_size || 10,
          rag_trigger_threshold: e.config?.rag?.rag_trigger_threshold || 3,
          rag_retrieval_count: e.config?.rag?.rag_retrieval_count || 10,
          rag_final_count: e.config?.rag?.rag_final_count || 5
        },
        context_window_kb: e.config?.context_window_kb || 512,
      workspace: e.config?.workspace || '',
        agent: {
          max_tool_iterations: e.config?.agent?.max_tool_iterations || 10,
          temperature: e.config?.agent?.temperature ?? 1,
          top_p: e.config?.agent?.top_p ?? 1,
          max_retries: e.config?.agent?.max_retries ?? 5
        },
        default_headers: e.config?.default_headers || {},
        questions: e.questions || []
      }
    }
  })
  loadTools()
}

function loadTools() {
  if (!globalConfig.value.api_base_url) return
  toolsLoading.value = true
  apiGet('/api/tools', (result) => {
    toolsLoading.value = false
    if (result.status && Array.isArray(result.data)) {
      toolsList.value = result.data
    }
  })
}

// ==================== Quota ====================
function updateQuota() {
  apiGet('/api/config', (result) => {
    if (result.status && result.data) {
      const e = result.data
      globalConfig.value = {
        system_prompt: e.config?.system_prompt || '',
        api_base_url: e.config?.api_base_url || '',
        api_key: e.config?.api_key || '',
        models: e.config?.models || [],
        embedding: {
          embedding_api_key: e.config?.embedding?.embedding_api_key || '',
          embedding_base_url: e.config?.embedding?.embedding_base_url || '',
          embedding_model_name: e.config?.embedding?.embedding_model_name || ''
        },
        rag: {
          sliding_window_size: e.config?.rag?.sliding_window_size || 10,
          rag_trigger_threshold: e.config?.rag?.rag_trigger_threshold || 3,
          rag_retrieval_count: e.config?.rag?.rag_retrieval_count || 10,
          rag_final_count: e.config?.rag?.rag_final_count || 5
        },
        context_window_kb: e.config?.context_window_kb || 512,
        workspace: e.config?.workspace || '',
        global_kb_agent_id: e.config?.global_kb_agent_id || 'ai-agent',
        mem0_api_url: e.config?.mem0_api_url || 'http://localhost:8000',
        use_global_rag: e.config?.use_global_rag === true || e.config?.use_global_rag === 'true',
        use_external_kb: e.config?.use_external_kb === true || e.config?.use_external_kb === 'true',
        agent: {
          max_tool_iterations: e.config?.agent?.max_tool_iterations || 10,
          temperature: e.config?.agent?.temperature ?? 1,
          top_p: e.config?.agent?.top_p ?? 1,
          max_retries: e.config?.agent?.max_retries ?? 5
        },
        default_headers: e.config?.default_headers || {},
        questions: e.questions || []
      }
      applyConfiguredModel(e.config)
      const dailyQuota = result.data.daily_quota
      if (dailyQuota) {
        const event = new CustomEvent('update-sidebar-info', { detail: { data: result.data } })
        window.dispatchEvent(event)
      }
    }
  })
}

function scrollToBottom() {
  // 走 ChatMain 暴露的方法，尊重 userIsScrolling 用户滚动锁
  nextTick(() => {
    if (chatMainRef.value && typeof chatMainRef.value.scrollToBottom === 'function') {
      chatMainRef.value.scrollToBottom()
    }
  })
}

// ==================== Mounted ====================
onMounted(() => {
  // Load config
  apiGet('/api/config', (result) => {
    if (result.status && result.data) {
      const e = result.data
      globalConfig.value = {
        system_prompt: e.config?.system_prompt || '',
        api_base_url: e.config?.api_base_url || '',
        api_key: e.config?.api_key || '',
        models: e.config?.models || [],
        embedding: {
          embedding_api_key: e.config?.embedding?.embedding_api_key || '',
          embedding_base_url: e.config?.embedding?.embedding_base_url || '',
          embedding_model_name: e.config?.embedding?.embedding_model_name || ''
        },
        rag: {
          sliding_window_size: e.config?.rag?.sliding_window_size || 10,
          rag_trigger_threshold: e.config?.rag?.rag_trigger_threshold || 3,
          rag_retrieval_count: e.config?.rag?.rag_retrieval_count || 10,
          rag_final_count: e.config?.rag?.rag_final_count || 5
        },
        context_window_kb: e.config?.context_window_kb || 512,
        workspace: e.config?.workspace || '',
        global_kb_agent_id: e.config?.global_kb_agent_id || 'ai-agent',
        mem0_api_url: e.config?.mem0_api_url || 'http://localhost:8000',
        use_global_rag: e.config?.use_global_rag === true || e.config?.use_global_rag === 'true',
        use_external_kb: e.config?.use_external_kb === true || e.config?.use_external_kb === 'true',
        agent: {
          max_tool_iterations: e.config?.agent?.max_tool_iterations || 10,
          temperature: e.config?.agent?.temperature ?? 1,
          top_p: e.config?.agent?.top_p ?? 1,
          max_retries: e.config?.agent?.max_retries ?? 5
        },
        default_headers: e.config?.default_headers || {},
        questions: e.questions || []
      }
      applyConfiguredModel(e.config)
      // 加载模型列表（在配置读取成功后调用）
      if (globalConfig.value.api_base_url && globalConfig.value.api_key) {
        apiGet('/api/models?base_url=' + encodeURIComponent(globalConfig.value.api_base_url) + '&key=' + encodeURIComponent(globalConfig.value.api_key), (r) => {
          modelsList.value = r.data || []
        })
      }
    }
  })

  // Load tool list
  apiGet('/api/tools', (result) => {
    toolsLoading.value = false
    if (result.status && Array.isArray(result.data)) {
      toolsList.value = result.data
      // 集团模式：主对话(经理)固定使用编排工具，前端不再选择具体工具
    }
  })

  // Load skill list
  apiGet('/api/skills', (result) => {
    if (result.status && result.data) {
      skillsCount.value = result.data.total || 0
    }
  })

  // Load conversation history
  loadConversations()

  // 恢复刷新前未发送的消息队列，空闲后自动继续发送
  restoreQueue()
  fetchOrgData()

  // 恢复上次会话：若该会话后台任务仍在运行，会自动续播（刷新/重进不丢消息）
  const lastId = localStorage.getItem('ai_last_conversation_id')
  if (!lastId) {
    markInitialRestoreDone()
  } else {
    loadConversation(lastId)
  }
  setTimeout(markInitialRestoreDone, 6000)
  setTimeout(processQueue, 1500)

  // 标签页恢复时续播未完成的后台任务事件流
  const onVisibilityChange = () => {
    if (document.hidden) { pendingReconnect = false; return }
    if (!currentConversationId.value || !isSending.value) { pendingReconnect = false; return }
    // 任务完成后不重连
    fetch('/api/chat/status?session_id=' + encodeURIComponent(currentConversationId.value))
      .then(r => r.json())
      .then(data => {
        if (!data?.status || data.status === 'done' || data.status === 'error' || data.status === 'stopped') {
          pendingReconnect = false
          return
        }
        pendingReconnect = true
        console.log('[visibility] 页面恢复，续播事件流 cursor=' + lastSeenEventId)
        followChatEvents(currentConversationId.value, lastSeenEventId, buildStreamHandlers(currentConversationId.value))
      })
      .catch(() => { pendingReconnect = false })
  }
  document.addEventListener('visibilitychange', onVisibilityChange)
  // 保留引用，用于 onUnmounted 清理
  window.__btOnVisibilityChange = onVisibilityChange
})

onUnmounted(() => {
  if (window.__btOnVisibilityChange) {
    document.removeEventListener('visibilitychange', window.__btOnVisibilityChange)
    window.__btOnVisibilityChange = null
  }
  abortCurrentRequest()
  clearThinkingTimer()
  if (taskTimerInterval) {
    clearInterval(taskTimerInterval)
    taskTimerInterval = null
  }
  if (eventSourceInstance) {
    eventSourceInstance.close()
    eventSourceInstance = null
  }
})

// ==================== Exposed ====================
function scrollToBottomExposed() {
  nextTick(() => {
    if (chatMainRef.value && typeof chatMainRef.value.forceScrollToBottom === 'function') {
      chatMainRef.value.forceScrollToBottom()
    }
  })
}

// ==================== Fullscreen ====================
const isFullscreen = ref(false)

function toggleFullscreen() {
  const container = containerRef.value
  if (!container) {
    console.warn('未找到 .ai-chat-container 元素')
    return
  }

  // 使用独立状态追踪全屏，保证进入/退出对称切换
  isFullscreen.value = !isFullscreen.value

  const appMain = document.querySelector('.app-w-main')
  const overlay = document.querySelector('.settings-drawer-overlay')
  const drawer = document.querySelector('.settings-drawer')

  if (isFullscreen.value) {
    container.classList.add('browser-fullscreen')
    if (appMain) {
      appMain.style.position = 'fixed'
      appMain.style.overflow = 'hidden'
      appMain.style.top = '0'
      appMain.style.left = '0'
      appMain.style.width = '100%'
      appMain.style.height = '100%'
      appMain.style.zIndex = '999999'
      appMain.style.transition = 'none'
    }
    if (overlay) overlay.style.zIndex = '999999'
    if (drawer) drawer.style.zIndex = '999999'
    container.style.transition = 'none'
  } else {
    container.classList.remove('browser-fullscreen')
    if (appMain) {
      appMain.style.position = ''
      appMain.style.overflow = ''
      appMain.style.top = ''
      appMain.style.left = ''
      appMain.style.width = ''
      appMain.style.height = ''
      appMain.style.zIndex = ''
      appMain.style.transition = ''
    }
  }
}
</script>

<style>
.ai-chat-container {
  display: flex;
  min-height: 100vh;
  height: 100vh;
  background: #fff;
  padding-bottom: 52px; /* 留出底部状态网空间 */
}
.chat-main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  min-width: 0;
}
.chat-main-area .chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.chat-main-area .chat-messages {
  flex: 1;
  overflow-y: auto;
}

/* ===== 任务执行状态栏 ===== */
.task-status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  background: #f0f9ff;
  border-top: 1px solid #bae6fd;
}

.task-status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #38bdf8;
  animation: statusPulse 1.2s ease-in-out infinite;
  flex-shrink: 0;
}

@keyframes statusPulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.4; transform: scale(0.75); }
}

.task-status-text {
  flex: 1;
  font-size: 12px;
  color: #0369a1;
}

.task-status-cancel {
  border: 1px solid #fecaca;
  background: #fff;
  color: #ef4444;
  font-size: 12px;
  padding: 3px 14px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
}

.task-status-cancel:hover {
  background: #fef2f2;
  border-color: #ef4444;
}

/* ===== 集团实时运行状态网（正常文档流，紧跟输入框下方） ===== */
.org-live-panel {
  position: relative;
  left: 0;
  right: 0;
  z-index: 100;
  border-top: 1px solid #dbeafe;
  background: linear-gradient(180deg, #f0f7ff, #f8fafc);
  padding: 8px 14px 6px;
  box-shadow: 0 -2px 8px rgba(0,0,0,0.06);
}
.olp-header {
  display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
}
.olp-pulse {
  width: 10px; height: 10px; border-radius: 50%; background: #94a3b8;
}
.olp-pulse.active {
  background: #22c55e;
  animation: olpPulse 1.2s ease-in-out infinite;
  box-shadow: 0 0 6px rgba(34,197,94,0.4);
}
@keyframes olpPulse {
  0%,100% { opacity:1; transform:scale(1); }
  50% { opacity:0.4; transform:scale(1.4); }
}
.olp-label {
  font-size: 11px; font-weight: 800; color: #1e40af;
  letter-spacing: 0.08em; text-transform: uppercase;
}
.olp-running-count {
  font-size: 11px; color: #16a34a; font-weight: 600;
  margin-left: auto;
}
.olp-depts {
  display: flex; flex-wrap: nowrap; gap: 8px; overflow-x: auto;
  padding-bottom: 4px;
}
.olp-dept {
  background: #fff; border: 1px solid #e2e8f0; border-radius: 10px;
  padding: 6px 10px; min-width: 130px; flex: 1; max-width: 200px;
  transition: all 0.3s ease;
}
.olp-dept.active {
  border-color: #22c55e;
  box-shadow: 0 0 10px rgba(34,197,94,0.12);
  background: #f0fdf4;
}
.olp-dept-header {
  font-size: 10px; font-weight: 800; color: #475569;
  text-transform: uppercase; letter-spacing: 0.05em;
  margin-bottom: 4px; border-bottom: 1px solid #f1f5f9; padding-bottom: 3px;
}
.olp-dept.active .olp-dept-header { color: #16a34a; border-color: #bbf7d0; }
.olp-dept-members { display: flex; flex-direction: column; gap: 2px; }
.olp-member {
  display: flex; align-items: center; gap: 5px;
  font-size: 11px; padding: 2px 0; color: #94a3b8;
  transition: color 0.3s;
}
.olp-member.running { color: #16a34a; font-weight: 700; }
.olp-member.done { color: #059669; }
.olp-m-dot {
  width: 7px; height: 7px; border-radius: 50%; background: #cbd5e1;
  flex-shrink: 0; transition: background 0.3s;
}
.olp-m-dot.running {
  background: #22c55e;
  animation: olpPulse 1s ease-in-out infinite;
}
.olp-m-dot.done { background: #059669; }
.olp-m-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.olp-m-gear { font-size: 10px; animation: olpSpin 2s linear infinite; }
@keyframes olpSpin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
.olp-m-check { font-size: 10px; color: #059669; font-weight: 700; }
.olp-m-empty { font-size: 10px; color: #cbd5e1; font-style: italic; }

/* ===== 消息发送队列 ===== *//* ===== 消息发送队列 ===== */
.msg-queue {
  border-top: 1px dashed #e5e7eb;
  background: #f9fafb;
  padding: 6px 10px 4px;
}

.msg-queue-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 5px;
}

.msg-queue-title {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
}

.msg-queue-tip {
  flex: 1;
  font-size: 11px;
  color: #9ca3af;
}

.msg-queue-clear {
  margin-left: auto;
  border: none;
  background: transparent;
  color: #9ca3af;
  font-size: 12px;
  cursor: pointer;
}

.msg-queue-clear:hover {
  color: #ef4444;
}

.msg-queue-items {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 96px;
  overflow-y: auto;
}

.msg-queue-item {
  display: flex;
  align-items: center;
  gap: 6px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 4px 8px;
  font-size: 12px;
  color: #374151;
}

.msg-queue-item.inactive {
  opacity: 0.55;
}

.msg-queue-session {
  flex-shrink: 0;
  max-width: 90px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #20a53a;
  background: #f0fdf4;
  border-radius: 4px;
  padding: 1px 6px;
  font-size: 11px;
}

.msg-queue-text {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.msg-queue-cancel {
  flex-shrink: 0;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: #9ca3af;
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.msg-queue-cancel:hover {
  background: #fee2e2;
  color: #ef4444;
}


.ai-chat-container.browser-fullscreen .welcome-message {
  margin: auto;
}
.ai-chat-container.browser-fullscreen .chat-messages {
  padding: 20px 15%;
}
.ai-chat-container.browser-fullscreen .history-panel {
  max-height: 100vh;
}
.ai-chat-container.browser-fullscreen .history-list {
  height: 600px;
}

/* ===== 手机适配 ===== */
@media (max-width: 768px) {
  .ai-chat-container { flex-direction: column !important; height: 100dvh; }

  /* 侧栏变为顶部抽屉 */
  .chat-sidebar {
    width: 100% !important;
    height: auto !important;
    flex-direction: column !important;
    border-right: none;
    border-bottom: 1px solid #e5e7eb;
    padding: 4px 8px;
  }
  .chat-sidebar .top-actions { width: 100%; padding: 4px 0; }
  .chat-sidebar .history-list { display: none; }
  .chat-sidebar .sidebar-body { display: none; }

  /* 主区域全宽 */
  .chat-main-area { flex: 1 !important; min-width: 0 !important; }

  /* 消息区域 */
  .chat-messages { padding: 8px 10px !important; gap: 12px !important; }
  .message-content { max-width: 95% !important; }
  .assistant-content { max-width: 95% !important; }

  /* 工具卡片全宽 */
  .tool-call { width: 100% !important; max-width: 100% !important; }

  /* 输入区 */
  .chat-input { padding: 6px 8px 4px !important; }
  .input-textarea { font-size: 16px !important; /* 防 iOS 缩放 */ }
  .toolbar-button { font-size: 11px !important; padding: 3px 5px !important; }
  .send-btn { width: 36px !important; height: 36px !important; }

  /* 状态栏 */
  .task-status-bar { padding: 4px 8px !important; }
  .task-status-text { font-size: 11px !important; }

  /* 集团状态网 */
  .org-live-panel { padding: 6px 8px 4px !important; }
  .olp-depts { gap: 4px !important; }
  .olp-dept { min-width: 80px !important; padding: 4px 6px !important; }
  .olp-member { font-size: 10px !important; }
  .olp-dept-header { font-size: 9px !important; }

  /* 队列 */
  .msg-queue { padding: 4px 8px 3px !important; }
  .msg-queue-text { font-size: 11px !important; }

  /* 设置面板 */
  .settings-drawer { width: 100% !important; max-width: 100% !important; }
  .drawer-content { padding: 12px !important; }
  .form-input, .form-textarea, .form-select { font-size: 14px !important; }

  /* 欢迎页 */
  .welcome-message { padding: 16px 10px !important; }
  .welcome-message h2 { font-size: 18px !important; }
  .org-departments { gap: 6px !important; }
  .org-dept { min-width: 100px !important; padding: 6px 8px !important; }
  .org-member { font-size: 10px !important; }
  .suggestion-cards { gap: 6px !important; }
  .suggestion-card { font-size: 12px !important; padding: 8px 10px !important; }
}

@media (max-width: 480px) {
  .chat-sidebar .top-btn { font-size: 11px !important; padding: 4px 6px !important; }
  .olp-dept { min-width: 70px !important; }
  .lsn-dept-name { font-size: 9px !important; }
  .toolbar-button svg { width: 12px !important; height: 12px !important; }
}

/* 工作模式切换（集团模式 / 单 Agent） */
.mode-switch {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin: 0 12px 8px;
  padding: 3px;
  background: #f1f2f4;
  border-radius: 8px;
  align-self: flex-start;
}
.mode-switch button {
  border: none;
  background: transparent;
  color: #5b6472;
  font-size: 13px;
  padding: 5px 14px;
  border-radius: 6px;
  cursor: pointer;
  transition: all .15s;
}
.mode-switch button:hover { color: #1f2937; }
/* 模式状态指示器 */
.mode-status-bar {
  display: flex;
  gap: 12px;
  align-items: center;
  padding: 6px 12px;
  background: var(--bg-secondary);
  border-radius: 8px;
  font-size: 12px;
  margin-bottom: 8px;
}

.mode-badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-weight: 600;
}

.mode-badge.group { background: #667eea20; color: #667eea; }
.mode-badge.single { background: #10b98120; color: #10b981; }
.mode-badge.opencode { background: #06b6d420; color: #06b6d4; }

.mode-info {
  color: var(--text-secondary);
}

.mode-info code {
  background: var(--bg-tertiary);
  padding: 1px 4px;
  border-radius: 3px;
}

.mode-switch button.active {
  background: #fff;
  color: #2563eb;
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0,0,0,.12);
}
.codex-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 10px;
  margin: 8px 12px 0;
  padding: 8px 10px;
  background: #f6f7fb;
  border: 1px solid #e3e6ee;
  border-radius: 8px;
}
.codex-field { display: flex; flex-direction: column; gap: 2px; }
.codex-field label { font-size: 11px; color: #6b7280; }
.codex-field select { min-width: 160px; padding: 4px 8px; border: 1px solid #d3d8e2; border-radius: 6px; font-size: 13px; }
.codex-instr { align-self: center; font-size: 12px; color: #4b5563; }
.codex-instr textarea { width: 320px; min-height: 44px; margin-top: 4px; }
.codex-manage { align-self: center; margin-left: auto; font-size: 12px; color: #2563eb; text-decoration: none; }
.codex-manage:hover { text-decoration: underline; }
</style>

<script>
// Attach toggleFullscreen to window for component access
window.aiApp = window.aiApp || {}
</script>
