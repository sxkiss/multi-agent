<template>
  <div class="chat-main">
    <div class="chat-messages" ref="chatMessagesRef">
      <!-- Welcome message -->
      <div v-if="messages.length === 0" class="welcome-message">
        <h2>{{ orgLoaded ? '集团已就位，下达你的目标' : '有什么我能帮你的吗?' }}</h2>

        <!-- 集团组织架构 -->
        <div v-if="orgLoaded" class="org-chart">
          <div class="org-node boss">
            <div class="org-role">Boss</div>
            <div class="org-title">你 · 下达目标</div>
          </div>
          <div class="org-arrow">▼</div>
          <div class="org-node manager">
            <div class="org-role">经理</div>
            <div class="org-title">拆解计划 · 分派 · 汇总交付</div>
          </div>
          <div class="org-arrow">▼</div>
          <div class="org-departments">
            <div v-for="d in orgData.departments" :key="d.name" class="org-dept">
              <div class="org-dept-title">{{ d.title }}</div>
              <div
                v-for="m in d.members"
                :key="d.name + m.name"
                class="org-member"
                :title="m.description + '　工具: ' + (m.tools||[]).join(', ')"
              >
                {{ m.name }}
                <span class="org-cap">
                  <i v-if="(m.tools||[]).includes('Write') || (m.tools||[]).includes('Edit')" class="cap w">写</i>
                  <i v-if="(m.tools||[]).some(t=>['Bash','PythonExecute','NodeExecute'].includes(t))" class="cap e">执</i>
                  <i v-if="(m.tools||[]).includes('WebFetch') || (m.tools||[]).some(t=>t.startsWith('http')||t==='curl_url')" class="cap n">网</i>
                  <i v-if="(m.tools||[]).every(t=>!['Write','Edit','Bash','PythonExecute','NodeExecute'].includes(t))" class="cap r">读</i>
                </span>
              </div>
              <div v-if="d.members.length===0" class="org-member empty">虚位以待</div>
            </div>
            <div v-if="orgData.departments.length===0" class="org-dept">
              <div class="org-dept-title">综合部</div>
              <div class="org-member empty">在设置中创建部门与成员</div>
            </div>
          </div>
        </div>

        <div class="suggestion-cards">
          <button
            v-for="(q, i) in suggestions"
            :key="i"
            class="suggestion-card"
            @click="$emit('suggestionClick', q)"
          >
            {{ typeof q === 'string' ? q : q.question }}
          </button>
        </div>
      </div>

      <!-- Messages -->
      <MessageItem
        v-for="(msg, idx) in messages"
        :key="msg.id || `msg-${idx}`"
        :msg="msg"
        :is-last-assistant="isLastAssistantMessage(idx)"
        @copy="handleCopy"
        @regenerate="(i) => $emit('regenerate', idx)"
        @delete="(i) => $emit('deleteMessage', idx)"
        @toggle-think="toggleThink(idx)"
        @send-continue="$emit('sendContinue')"
      />

      <!-- Streaming content blocks -->
      <div v-if="streamingContentBlocks.length > 0" class="message assistant">
        <div v-for="(block, bi) in streamingContentBlocks" :key="bi" class="streaming-block">
          <!-- Think block -->
          <div
            v-if="block.type === 'think'"
            class="think-section"
            :class="{ collapsed: block.collapsed }"
          >
            <div class="think-title" @click="toggleStreamingThinkBlock(block)">
              <svg class="think-toggle-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="6 9 12 15 18 9"></polyline>
              </svg>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"></path>
                <line x1="12" y1="17" x2="12.01" y2="17"></line>
              </svg>
              思考过程
            </div>
            <div class="think-content">{{ block.content }}</div>
          </div>

          <!-- Message block -->
          <div
            v-else-if="block.type === 'message'"
            class="streaming-message"
            v-html="parseMarkdown(block.content)"
          ></div>

          <!-- Tool call block -->
          <div
            v-else-if="block.type === 'tool_call'"
            class="tool-call"
            :class="{
              collapsed: block.collapsed,
              completed: block.tool === 'TodoWrite' ? isTodoAllDone(block) : block.status === 'completed',
              'todo-card': block.tool === 'TodoWrite'
            }"
          >
            <div class="tool-call-header" @click="toggleStreamingToolBlock(block)">
              <span class="tool-status-icon">
                <svg v-if="block.status === 'calling'" class="tool-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="10"></circle>
                  <polyline points="12 6 12 12 16 14"></polyline>
                </svg>
                <svg v-else class="tool-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="12" cy="12" r="10"></circle>
                  <path d="m9 12 2 2 4-4"></path>
                </svg>
              </span>
              <span class="tool-name">{{ block.tool }}</span>
              <span
                class="tool-badge"
                :class="{ success: block.tool === 'TodoWrite' ? isTodoAllDone(block) : block.status === 'completed' }"
              >
                {{
                  block.tool === 'TodoWrite'
                    ? (block.status === 'calling' ? '更新中' : isTodoAllDone(block) ? '已完成' : '进行中')
                    : (block.status === 'calling' ? '调用中' : '完成')
                }}
              </span>
              <span class="tool-toggle-icon">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
              </span>
            </div>
            <div v-if="block.todoItems && block.todoItems.length > 0" class="todo-list">
              <div v-for="(todo, ti) in block.todoItems" :key="ti" class="todo-item" :class="'todo-' + (todo.status || 'pending')">
                <span class="todo-status-icon">
                  <svg v-if="todo.status === 'completed'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <circle cx="12" cy="12" r="10"></circle>
                    <path d="m9 12 2 2 4-4"></path>
                  </svg>
                  <svg v-else-if="todo.status === 'cancelled'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"></circle>
                    <path d="m15 9-6 6M9 9l6 6"></path>
                  </svg>
                  <svg v-else class="todo-icon-spin" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <circle cx="12" cy="12" r="10"></circle>
                    <path d="M12 6v2M12 16v2M6 12h2M16 12h2"></path>
                  </svg>
                </span>
                <span class="todo-description">{{ todo.description || todo.content || '' }}</span>
              </div>
            </div>
            <div v-else class="tool-call-details" v-show="!block.collapsed">
              <div v-if="block.description" class="tool-call-description">{{ block.description }}</div>
              <div v-if="block.args" class="tool-call-args">
                <pre>{{ formatArgs(block.args) }}</pre>
              </div>
              <div v-if="block.result" class="tool-result-content">
                <pre>{{ formatResult(block.result) }}</pre>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Typing indicator -->
      <div v-if="isTyping" class="typing-indicator">
        <div class="typing-dots">
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
          <span class="typing-dot"></span>
        </div>
      </div>
    </div>
  </div>
</template>

<script>
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import { authOpts } from '../auth.js'
import MessageItem from './MessageItem.vue'

export default {
  name: 'ChatMain',
  components: { MessageItem },
  props: {
    messages: { type: Array, default: () => [] },
    isTyping: { type: Boolean, default: false },
    streamingContentBlocks: { type: Array, default: () => [] },
    streamingThinkCollapsed: { type: Boolean, default: false },
    suggestions: { type: Array, default: () => [] }
  },
  emits: ['update:streamingThinkCollapsed', 'suggestionClick', 'regenerate', 'deleteMessage', 'sendContinue', 'copy', 'toggleThink'],
  setup(props, { expose, emit }) {
    const chatMessagesRef = ref(null)
    const userIsScrolling = ref(false)

    // 集团组织架构（首页展示）
    const orgData = ref({ departments: [] })
    const orgLoaded = ref(false)
    async function fetchOrg() {
      try {
        const r = await fetch('/api/org', authOpts())
        const res = await r.json()
        if (res.status && Array.isArray(res.data?.departments)) {
          orgData.value = res.data
          orgLoaded.value = true
        }
      } catch {}
    }
    let scrollTimeout = null

    function sanitizeHTML(html) {
      const allowedTags = ['p', 'br', 'strong', 'em', 'u', 's', 'code', 'pre', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote', 'a', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'span', 'div', 'hr']
      const allowedAttrs = {
        a: ['href', 'title', 'target'],
        img: ['src', 'alt', 'title', 'width', 'height'],
        code: ['class'],
        pre: ['class'],
        span: ['class'],
        div: ['class']
      }
      try {
        const doc = new DOMParser().parseFromString(html, 'text/html')
        function clean(node) {
          if (node.nodeType === Node.TEXT_NODE) return node.cloneNode(true)
          if (node.nodeType === Node.ELEMENT_NODE) {
            const el = node
            const tag = el.tagName.toLowerCase()
            if (!allowedTags.includes(tag)) return null
            const newEl = document.createElement(tag)
            const attrs = allowedAttrs[tag] || []
            for (const attr of Array.from(el.attributes)) {
              if (attrs.includes(attr.name)) {
                if (attr.name === 'href' || attr.name === 'src') {
                  const val = attr.value.trim().toLowerCase()
                  // 拒绝协议相对 URL（//evil.com）与 javascript: 等危险协议
                  if (!val.startsWith('//') && (val.startsWith('http://') || val.startsWith('https://') || val.startsWith('mailto:') || val.startsWith('/') || val.startsWith('./') || val.startsWith('#'))) {
                    newEl.setAttribute(attr.name, attr.value)
                  }
                } else {
                  newEl.setAttribute(attr.name, attr.value)
                }
              }
            }
            for (const child of Array.from(el.childNodes)) {
              const cleanChild = clean(child)
              if (cleanChild) newEl.appendChild(cleanChild)
            }
            return newEl
          }
          return null
        }
        const out = document.createElement('div')
        for (const child of Array.from(doc.body.childNodes)) {
          const cleaned = clean(child)
          if (cleaned) out.appendChild(cleaned)
        }
        return out.innerHTML
      } catch {
        return html
      }
    }

    function handleUserScroll() {
      userIsScrolling.value = true
      if (scrollTimeout) clearTimeout(scrollTimeout)
      scrollTimeout = setTimeout(() => {
        userIsScrolling.value = false
      }, 500)
    }

    function isNearBottom() {
      if (!chatMessagesRef.value) return true
      const { scrollTop, scrollHeight, clientHeight } = chatMessagesRef.value
      return scrollHeight - scrollTop - clientHeight < 150
    }

    function scrollToBottom() {
      if (userIsScrolling.value) return
      nextTick(() => {
        if (chatMessagesRef.value && isNearBottom()) {
          chatMessagesRef.value.scrollTo({ top: chatMessagesRef.value.scrollHeight, behavior: 'smooth' })
        }
      })
    }

    function forceScrollToBottom() {
      userIsScrolling.value = false
      nextTick(() => {
        if (chatMessagesRef.value) {
          chatMessagesRef.value.scrollTo({ top: chatMessagesRef.value.scrollHeight, behavior: 'smooth' })
        }
      })
    }

    onMounted(() => {
      if (chatMessagesRef.value) {
        chatMessagesRef.value.addEventListener('scroll', handleUserScroll, { passive: true })
      }
      fetchOrg()
    })

    onUnmounted(() => {
      if (chatMessagesRef.value) {
        chatMessagesRef.value.removeEventListener('scroll', handleUserScroll)
      }
      if (scrollTimeout) clearTimeout(scrollTimeout)
    })

    const parseMarkdown = (content) => {
      try {
        const md = window.marked?.parse(content)
        return sanitizeHTML(md)
      } catch {
        console.error('Markdown parse error')
        // 解析失败也必须消毒后再进 v-html
        return sanitizeHTML(content)
      }
    }

    const formatArgs = (args) => {
      try {
        const parsed = JSON.parse(args)
        return JSON.stringify(parsed, null, 2)
      } catch {
        return args
      }
    }

    const formatResult = (result) => {
      try {
        const parsed = JSON.parse(result)
        return typeof parsed === 'string' ? parsed : JSON.stringify(parsed, null, 2)
      } catch {
        return result
      }
    }

    function isLastAssistantMessage(idx) {
      for (let i = props.messages.length - 1; i >= 0; i--) {
        if (props.messages[i].role === 'assistant') return i === idx
      }
      return false
    }

    async function copyContent(text) {
      try {
        if (navigator.clipboard) {
          await navigator.clipboard.writeText(text)
        } else {
          const ta = document.createElement('textarea')
          ta.value = text
          ta.style.position = 'fixed'
          ta.style.opacity = '0'
          document.body.appendChild(ta)
          ta.select()
          document.execCommand('copy')
          document.body.removeChild(ta)
        }
        if (window.layer) window.layer.msg('复制成功', { icon: 1 })
      } catch (e) {
        if (window.layer) window.layer.msg('复制失败', { icon: 2 })
        console.error('复制失败:', e)
      }
    }

    function toggleThink(idx) {
      const msg = props.messages[idx]
      if (msg && msg.contentBlocks) {
        msg.contentBlocks.forEach(b => {
          if (b.type === 'think') b.collapsed = !b.collapsed
        })
      }
    }

    function toggleStreamingThinkBlock(block) {
      block.collapsed = !block.collapsed
    }

    function toggleStreamingToolBlock(block) {
      block.collapsed = !block.collapsed
    }

    function isTodoAllDone(block) {
      if (!block.todoItems || block.todoItems.length === 0) return false
      return block.todoItems.every(t => t.status === 'completed' || t.status === 'cancelled')
    }

    function handleCopy(text) {
      copyContent(text)
    }

    expose({
      orgData, orgLoaded, fetchOrg,
      scrollToBottom,
      forceScrollToBottom,
      chatMessagesRef,
      userIsScrolling,
      sanitizeHTML,
      parseMarkdown,
      formatArgs,
      formatResult,
      isLastAssistantMessage,
      copyContent,
      toggleThink,
      toggleStreamingThinkBlock,
      toggleStreamingToolBlock,
      isTodoAllDone,
      handleUserScroll,
      isNearBottom,
      scrollTimeout
    })

    return {
      orgData, orgLoaded,
      chatMessagesRef,
      parseMarkdown,
      formatArgs,
      formatResult,
      isLastAssistantMessage,
      copyContent,
      toggleThink,
      toggleStreamingThinkBlock,
      toggleStreamingToolBlock,
      isTodoAllDone,
      handleCopy
    }
  }
}
</script>

<style scoped>
/* ===== 集团组织架构 ===== */
.org-chart {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  margin: 0 auto 26px;
  max-width: 100%;
}
.org-node {
  border-radius: 10px;
  padding: 10px 22px;
  text-align: center;
  min-width: 180px;
}
.org-node.boss {
  background: linear-gradient(135deg,#1f2937,#374151);
  color:#fff;
}
.org-role { font-size:12px; opacity:.75; letter-spacing:.1em; text-transform:uppercase; }
.org-title { font-size:14px; font-weight:600; margin-top:2px; }
.org-node.manager { background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; }
.org-arrow { color:#cbd5e1; font-size:12px; line-height:1; }
.org-departments {
  display:flex; flex-wrap:wrap; gap:12px; justify-content:center;
}
.org-dept {
  background:#fff; border:1px solid #e5e7eb; border-radius:10px;
  padding:8px 12px; min-width:150px;
}
.org-dept-title {
  font-size:11px; font-weight:700; color:#20a53a;
  letter-spacing:.06em; margin-bottom:4px; text-align:left;
}
.org-member {
  font-size:12px; color:#4b5563; padding:2px 0; text-align:left;
  cursor:default;
}
.org-member.empty { color:#c7cdd4; font-style:italic; }
.org-cap { margin-left:4px; display:inline-flex; gap:2px; }
.org-cap .cap {
  font-size:9px; font-style:normal; padding:0 3px;
  border-radius:3px; line-height:14px;
}
.org-cap .w { background:#ecfdf5; color:#059669; }
.org-cap .e { background:#eff6ff; color:#2563eb; }
.org-cap .n { background:#fff7ed; color:#ea580c; }
.org-cap .r { background:#f3f4f6; color:#6b7280; }

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #fff;
}

.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 20px 10px;
  display: flex;
  flex-direction: column;
  gap: 24px;
  background: #fff;
}

.chat-messages::-webkit-scrollbar {
  width: 10px;
}
.chat-messages::-webkit-scrollbar-track {
  background: transparent;
  margin: 4px;
}
.chat-messages::-webkit-scrollbar-thumb {
  background: #d1d5db;
  border-radius: 4px;
  border: 2px solid transparent;
  background-clip: content-box;
}
.chat-messages::-webkit-scrollbar-thumb:hover {
  background: #9ca3af;
  border-radius: 4px;
  border: 2px solid transparent;
  background-clip: content-box;
}

.welcome-message {
  text-align: center;
  padding: 30px 20px 20px; width: 100%;
  animation: welcomeFadeIn 0.8s ease-out;
  margin: auto;
}

@keyframes welcomeFadeIn {
  0% { opacity: 0; transform: translateY(20px); }
  100% { opacity: 1; transform: translateY(0); }
}

.welcome-message h2 {
  font-size: 24px;
  font-weight: 600;
  color: #1f2937;
  margin: 0 0 32px;
}

.suggestion-cards {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  justify-content: center;
}

.suggestion-card {
  padding: 12px 16px;
  background: #f7f8fa;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  font-size: 14px;
  color: #4b5563;
  text-align: left;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.suggestion-card:hover {
  background: #f0f1f3;
  border-color: #d1d5db;
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.message {
  display: flex;
  flex-direction: column;
}

.message.assistant {
  align-items: flex-start;
  max-width: 100%;
  margin-bottom: 18px;
}

/* 流式助手消息：在顶部显示头像 + 名字行 */
.message.assistant::before {
  content: "AI 助手";
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 4px;
}
.message.assistant::before::before {
  content: "AI";
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: linear-gradient(135deg, #20a53a, #16a34a);
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  flex-shrink: 0;
}

.message-content {
  padding: 0;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.6;
  word-wrap: break-word;
  max-width: 90%;
  background: transparent;
  color: #1f2937;
}

.assistant-content {
  max-width: 85%;
}

.think-section {
  margin-bottom: 12px;
  padding: 12px;
  background: #f8f9fa;
  border-radius: 4px;
  font-size: 13px;
  width: 100%;
  color: #666;
}

.think-title {
  font-weight: 600;
  color: #6366f1;
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  user-select: none;
}

.think-toggle-icon {
  transition: transform 0.2s;
}

.think-section.collapsed .think-toggle-icon {
  transform: rotate(-90deg);
}

.think-section.collapsed .think-content {
  display: none;
}

.think-content {
  padding-top: 8px;
  line-height: 1.6;
  white-space: pre-wrap;
  transition: all 0.3s ease;
}

.streaming-message {
  max-width: 100%;
  font-size: 14px;
  line-height: 1.6;
}

.tool-call {
  margin: 8px 0;
  border: 1px solid #ffd699;
  border-radius: 8px;
  background: #fff4e6;
  overflow: hidden;
  width: 50%;
  max-width: 90%;
  transition: all 0.3s;
}

.tool-call.completed {
  background: #e6f7ff;
  border-color: #91d5ff;
}

.tool-call-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
  user-select: none;
}

.tool-status-icon {
  display: flex;
  align-items: center;
}

.tool-icon {
  flex-shrink: 0;
  color: #f59e0b;
  transition: color 0.3s;
}

.tool-call.completed .tool-icon {
  color: #10b981;
}

.tool-name {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: #1f2937;
}

.tool-badge {
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 11px;
  font-weight: 500;
  background: #fbbf24;
  color: #78350f;
  transition: all 0.3s;
}

.tool-badge.success {
  background: #34d399;
  color: #065f46;
}

.tool-toggle-icon {
  display: flex;
  align-items: center;
  color: #9ca3af;
  transition: transform 0.2s;
  flex-shrink: 0;
}

.tool-call.collapsed .tool-toggle-icon {
  transform: rotate(-90deg);
}

.tool-call-details {
  margin-top: 8px;
  padding: 0 12px 12px;
  transition: all 0.3s ease;
}

.tool-call-description {
  font-size: 13px;
  color: #6b7280;
  margin-bottom: 8px;
}

.tool-call-args pre,
.tool-result-content pre {
  background: #f4f5f7;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
}

.tool-result-content {
  margin-top: 8px;
}

.tool-call.todo-card {
  width: auto;
  min-width: 400px;
  max-width: 480px;
  background: #f0f4ff;
  border: 1px solid #c7d7fd;
}

.tool-call.todo-card.completed {
  background: #f0faf4;
  border-color: #a7e3c0;
}

.todo-list {
  padding: 0 12px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.todo-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 5px 6px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.4;
  transition: background 0.2s;
}

.todo-item.todo-in_progress {
  background: rgba(99, 102, 241, 0.08);
}

.todo-status-icon {
  display: flex;
  align-items: center;
  color: #20a53a;
  flex-shrink: 0;
}

.todo-item.todo-pending .todo-status-icon {
  color: #9ca3af;
}

.todo-item.todo-in_progress .todo-status-icon {
  color: #6366f1;
}

.todo-item.todo-completed .todo-status-icon {
  color: #10b981;
}

.todo-item.todo-cancelled .todo-status-icon {
  color: #d1d5db;
}

.todo-icon-spin {
  animation: todoSpin 1s linear infinite;
}

@keyframes todoSpin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.todo-description {
  flex: 1;
  color: #374151;
  line-height: 1.4;
}

.todo-item.todo-completed .todo-description {
  color: #6b7280;
  text-decoration: line-through;
}

.todo-item.todo-cancelled .todo-description {
  color: #9ca3af;
  text-decoration: line-through;
}

.typing-indicator {
  display: flex;
  gap: 12px;
  padding: 12px 0;
  align-items: flex-start;
}

.typing-dots {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 12px 16px;
  background: #f4f5f7;
  border-radius: 8px;
}

.typing-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #9ca3af;
  animation: typingBounce 1.4s infinite ease-in-out;
}

.typing-dot:nth-child(2) {
  animation-delay: 0.2s;
}
.typing-dot:nth-child(3) {
  animation-delay: 0.4s;
}

@keyframes typingBounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.4; }
  40% { transform: scale(1); opacity: 1; }
}
</style>
