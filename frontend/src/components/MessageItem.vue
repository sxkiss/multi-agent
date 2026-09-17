<template>
  <div class="message" :class="msg.role">
    <!-- User message -->
    <template v-if="msg.role === 'user'">
      <div class="message-content">
        <!-- User attachments (image / video / audio / file) -->
        <div v-if="getUserAttachments().length > 0" class="user-attachments">
          <template v-for="(att, i) in getUserAttachments()" :key="i">
            <img
              v-if="att.kind === 'image'"
              :src="att.url"
              alt="图片"
              class="user-image"
              @click="previewImage(att.url)"
            />
            <video
              v-else-if="att.kind === 'video'"
              :src="att.url"
              class="user-media"
              controls
              preload="metadata"
            ></video>
            <audio
              v-else-if="att.kind === 'audio'"
              :src="att.url"
              class="user-media"
              controls
              preload="metadata"
            ></audio>
            <iframe
              v-else-if="isPreviewable(att)"
              :src="att.url"
              class="user-pdf"
              frameborder="0"
            ></iframe>
            <a
              v-else
              class="user-file-link"
              :href="att.url"
              target="_blank"
              :download="att.name"
              :title="att.name"
            >{{ att.name }}</a>
          </template>
        </div>
        <!-- User text with file tags -->
        <div
          v-if="getUserMessageText()"
          class="user-text"
          v-html="renderTextWithFileTags(getUserMessageText())"
        ></div>
        <!-- legacy: user images from content (old messages) -->
        <div v-if="!getUserAttachments().length && getUserMessageImages().length > 0" class="user-images">
          <img
            v-for="(src, i) in getUserMessageImages()"
            :key="i"
            :src="src"
            alt="图片"
            class="user-image"
            @click="previewImage(src)"
          />
        </div>
        <!-- User actions -->
        <div class="message-actions user-actions">
          <button class="message-action-btn" title="复制" @click="handleUserCopy">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
          </button>
          <button class="message-action-btn" title="删除" @click="$emit('delete')">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
              <line x1="10" y1="11" x2="10" y2="17"></line>
              <line x1="14" y1="11" x2="14" y2="17"></line>
            </svg>
          </button>
        </div>
      </div>
    </template>

    <!-- Assistant message -->
    <template v-else-if="msg.role === 'assistant'">
      <div class="message-content assistant-content">
        <!-- Content blocks -->
        <template v-if="msg.contentBlocks && msg.contentBlocks.length > 0">
          <div v-for="(block, bi) in msg.contentBlocks" :key="bi">
            <!-- Think block -->
            <div
              v-if="block.type === 'think'"
              class="think-section"
              :class="{ collapsed: block.collapsed }"
            >
              <div class="think-title" @click="toggleBlockCollapse(block)">
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
              class="assistant-text"
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
              <div class="tool-call-header" @click="toggleToolBlockCollapse(block)">
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

              <!-- Todo list -->
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

              <!-- Tool details -->
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
        </template>

        <!-- Fallback text -->
        <div
          v-else-if="msg.content"
          class="assistant-text"
          v-html="parseMarkdown(getMessageText())"
        ></div>

        <!-- Assistant attachments (same display rules as user) -->
        <div v-if="getUserAttachments().length > 0" class="user-attachments">
          <template v-for="(att, i) in getUserAttachments()" :key="i">
            <img
              v-if="att.kind === 'image'"
              :src="att.url"
              alt="图片"
              class="user-image"
              @click="previewImage(att.url)"
            />
            <video
              v-else-if="att.kind === 'video'"
              :src="att.url"
              class="user-media"
              controls
              preload="metadata"
            ></video>
            <audio
              v-else-if="att.kind === 'audio'"
              :src="att.url"
              class="user-media"
              controls
              preload="metadata"
            ></audio>
            <iframe
              v-else-if="isPreviewable(att)"
              :src="att.url"
              class="user-pdf"
              frameborder="0"
            ></iframe>
            <a
              v-else
              class="user-file-link"
              :href="att.url"
              target="_blank"
              :download="att.name"
              :title="att.name"
            >{{ att.name }}</a>
          </template>
        </div>

        <!-- Assistant actions -->
        <div v-if="isLastAssistant" class="message-actions">
          <button
            v-if="hasToolLimitNote"
            class="message-action-btn continue-btn"
            title="继续"
            @click="$emit('sendContinue')"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="5 3 19 12 5 21 5 3"></polygon>
            </svg>
            继续
          </button>
          <button class="message-action-btn" title="重新生成" @click="$emit('regenerate')">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="1 4 1 10 7 10"></polyline>
              <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"></path>
            </svg>
          </button>
          <button class="message-action-btn" title="复制" @click="handleCopy">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
              <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
            </svg>
          </button>
          <button class="message-action-btn" title="删除" @click="$emit('delete')">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
          </button>
        </div>
      </div>
    </template>

    <!-- Image preview modal -->
    <div v-if="showPreview" class="image-preview-overlay" @click="closePreview">
      <img :src="previewSrc" alt="预览" class="image-preview" />
    </div>
  </div>
</template>

<script>
import { ref, computed } from 'vue'

export default {
  name: 'MessageItem',
  props: {
    msg: { type: Object, default: () => ({}) },
    isLastAssistant: { type: Boolean, default: false }
  },
  emits: ['copy', 'regenerate', 'delete', 'toggleThink', 'sendContinue'],
  setup(props, { emit }) {
    const showPreview = ref(false)
    const previewSrc = ref('')

    // Check for max_tool_iterations hint (agent finished with note)
    const hasToolLimitNote = computed(() => {
      if (props.msg.role !== 'assistant') return false
      if (props.msg.contentBlocks && props.msg.contentBlocks.length > 0) {
        return props.msg.contentBlocks.some(
          b => b.type === 'message' && b.content && b.content.includes('max_tool_iterations')
        )
      }
      return typeof props.msg.content === 'string' && props.msg.content.includes('max_tool_iterations')
    })

    function getMessageText() {
      if (typeof props.msg.content === 'string') return props.msg.content
      if (Array.isArray(props.msg.content)) {
        return props.msg.content.find(i => i.type === 'text')?.text || ''
      }
      return ''
    }

    function getUserFiles() {
      if (!Array.isArray(props.msg.content)) return []
      return props.msg.content
        .filter(i => i.type === 'file' && i.source && i.path)
        .map(i => ({ value: i.source.value, path: i.path, start: i.source.start, end: i.source.end }))
    }

    function getUserMessageText() {
      let t = ''
      if (typeof props.msg.content === 'string') t = props.msg.content
      else if (Array.isArray(props.msg.content)) {
        t = props.msg.content.find(i => i.type === 'text')?.text || ''
      }
      // 隐藏内部附件路径提示行（仅供 agent，不在气泡中显示）
      t = t.split('\n').filter(l => !l.trim().startsWith('附件路径:')).join('\n').trim()
      return t
    }

    function getUserMessageImages() {
      if (!Array.isArray(props.msg.content)) return []
      return props.msg.content
        .filter(i => i.type === 'image_url' && i.image_url && i.image_url.url)
        .map(i => i.image_url.url)
    }

    function getUserAttachments() {
      const m = props.msg
      if (m && Array.isArray(m.attachments) && m.attachments.length) {
        return m.attachments
      }
      // 旧消息兼容：从 content 还原附件
      if (!Array.isArray(m?.content)) return []
      const list = []
      for (const i of m.content) {
        if (!i || typeof i !== 'object') continue
        if (i.type === 'image_url' && i.image_url?.url) {
          list.push({ kind: 'image', name: '图片', url: i.image_url.url })
        } else if (i.type === 'file' && i.path) {
          const name = (i.source?.value || i.path).replace(/^@/, '')
          list.push({ kind: 'file', name, url: i.path, path: i.path })
        }
      }
      return list
    }

    function isPreviewable(att) {
      if (!att) return false
      const name = (att.name || '').toLowerCase()
      const mime = (att.mime || '').toLowerCase()
      return /\.pdf$/i.test(name) || mime === 'application/pdf'
    }

    function renderTextWithFileTags(text) {
      if (!text) return ''
      // Escape HTML first, then insert file tags
      const escapeHtml = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      let safe = escapeHtml(text)
      const files = getUserFiles()
      for (const f of files) {
        // tag 也需要先做 HTML 转义再匹配，否则转义后的文本中匹配不到
        const tag = escapeHtml(f.value || '')
        if (!tag) continue
        safe = safe.replace(new RegExp(tag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g'), '')
      }
      // Apply markdown for remaining text
      try {
        const md = window.marked?.parse(safe) || safe
        return sanitizeHTML(md)
      } catch {
        return safe
      }
    }

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
      // 将图像/链接地址转为安全可访问的 URL
      function makeSafeUrl(raw, isImg) {
        const s = String(raw || '').trim()
        if (!s) return ''
        // 协议相对 URL（//evil.com）视为危险，拒绝
        if (s.startsWith('//')) return ''
        if (/^https?:\/\//i.test(s)) return s
        if (/^mailto:/i.test(s)) return s
        if (s.startsWith('#')) return s
        // 图片：允许 base64 data URL
        if (isImg && /^data:image\//i.test(s)) return s
        // 图片：服务器侧文件（相对或绝对路径）→ 经 /api/files/raw 提供，浏览器才能加载
        if (isImg) {
          return '/api/files/raw?path=' + encodeURIComponent(s)
        }
        // 非图片链接：仅允许站内相对/绝对路径
        if (s.startsWith('/') || s.startsWith('./')) return s
        return ''
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
                  const safe = makeSafeUrl(attr.value, attr.name === 'src')
                  if (safe) newEl.setAttribute(attr.name, safe)
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

    function parseMarkdown(content) {
      try {
        const md = window.marked?.parse(content) || content
        return sanitizeHTML(md)
      } catch {
        console.error('Markdown parse error')
        // 解析失败也必须消毒后再进 v-html
        return sanitizeHTML(content)
      }
    }

    function formatArgs(args) {
      try {
        const parsed = JSON.parse(args)
        return JSON.stringify(parsed, null, 2)
      } catch {
        return args
      }
    }

    function formatResult(result) {
      try {
        const parsed = JSON.parse(result)
        return typeof parsed === 'string' ? parsed : JSON.stringify(parsed, null, 2)
      } catch {
        return result
      }
    }

    function toggleBlockCollapse(block) {
      block.collapsed = !block.collapsed
    }

    function toggleToolBlockCollapse(block) {
      block.collapsed = !block.collapsed
    }

    function isTodoAllDone(block) {
      if (!block.todoItems || block.todoItems.length === 0) return false
      return block.todoItems.every(t => t.status === 'completed' || t.status === 'cancelled')
    }

    function handleCopy() {
      let text = ''
      if (props.msg.contentBlocks && props.msg.contentBlocks.length > 0) {
        text = props.msg.contentBlocks
          .filter(b => b.type === 'message')
          .map(b => b.content)
          .join('\n')
      } else {
        text = getMessageText()
      }
      emit('copy', text)
    }

    function handleUserCopy() {
      emit('copy', getUserMessageText())
    }

    function previewImage(src) {
      previewSrc.value = src
      showPreview.value = true
    }

    function closePreview() {
      showPreview.value = false
      previewSrc.value = ''
    }

    return {
      showPreview,
      previewSrc,
      hasToolLimitNote,
      getMessageText,
      getUserFiles,
      getUserMessageText,
      getUserMessageImages,
      getUserAttachments,
      isPreviewable,
      renderTextWithFileTags,
      parseMarkdown,
      formatArgs,
      formatResult,
      toggleBlockCollapse,
      toggleToolBlockCollapse,
      isTodoAllDone,
      handleCopy,
      handleUserCopy,
      previewImage,
      closePreview
    }
  }
}
</script>

<style scoped>
.message {
  display: flex;
  flex-direction: column;
}

.message.user {
  align-items: flex-end;
}

/* 历史消息：头像在侧、内容在旁 */
.message {
  flex-direction: row;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 18px;
}

.message.user {
  flex-direction: row-reverse;
}

/* 头像圆形 */
.message::before {
  content: "";
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  margin-top: 2px;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
}

.message.assistant::before {
  content: "AI";
  background: linear-gradient(135deg, #20a53a, #16a34a);
}

.message.user::before {
  content: "我";
  background: linear-gradient(135deg, #3b82f6, #2563eb);
}

.message-content {
  padding: 8px 14px;
  border-radius: 10px;
  font-size: 14px;
  line-height: 1.6;
  word-wrap: break-word;
  max-width: calc(100% - 50px);
  background: #f4f5f7;
  color: #1f2937;
}

.message-content.assistant-content {
  width: 100%;
  padding: 10px 14px;
  background: #f7f8fa;
  border: 1px solid #eceef1;
  border-radius: 12px;
}

.message-content.assistant-content::before {
  content: "AI 助手";
  display: block;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 6px;
}

.user-images {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}

.user-image {
  max-width: 200px;
  max-height: 200px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid #e5e7eb;
}

.user-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
  align-items: flex-start;
}

.user-attachments .user-image {
  margin-bottom: 0;
}

.user-media {
  max-width: 320px;
  max-height: 260px;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #000;
}

.user-pdf {
  width: 100%;
  max-width: 520px;
  height: 420px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
}

.user-audio {
  width: 280px;
  max-width: 100%;
  padding: 6px 10px;
  background: #f3f4f6;
  border-radius: 8px;
}

.user-audio audio {
  width: 100%;
}

.user-file-link {
  display: inline-block;
  max-width: 260px;
  margin: 2px 6px 2px 0;
  padding: 3px 8px;
  background: #f3f4f6;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  color: #2563eb;
  text-decoration: none;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}

.user-file-link:hover {
  background: #eef2ff;
  border-color: #c7d2fe;
}

.message-actions {
  display: flex;
  gap: 4px;
  margin-top: 8px;
  opacity: 0;
  transition: opacity 0.2s;
}

.message:hover .message-actions,
.message-actions:hover {
  opacity: 1;
}

.user-actions {
  justify-content: flex-end;
}

.message-action-btn {
  width: 28px;
  height: 28px;
  border-radius: 6px;
  border: none;
  background: #f3f4f6;
  color: #6b7280;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}

.message-action-btn:hover {
  background: #e5e7eb;
  color: #374151;
}

.continue-btn {
  width: auto;
  padding: 0 10px;
  gap: 4px;
  font-size: 12px;
  color: #20a53a;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
}

.continue-btn:hover {
  background: #dcfce7;
  border-color: #20a53a;
  color: #166534;
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

.assistant-text {
  max-width: 100%;
  font-size: 14px;
  line-height: 1.6;
  margin-bottom: 4px;
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

.image-preview-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  cursor: pointer;
}

.image-preview {
  max-width: 90%;
  max-height: 90%;
  border-radius: 8px;
}
</style>
