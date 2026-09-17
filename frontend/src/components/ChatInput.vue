<template>
  <div class="chat-input">
    <!-- Attachment preview row (image / video / audio / file) -->
    <div v-if="pendingAttachments.length > 0" class="attachment-preview-row">
      <div
        v-for="(att, i) in pendingAttachments"
        :key="att.id"
        class="att-thumb"
        :class="'att-' + att.kind"
      >
        <!-- image -->
        <img v-if="att.kind === 'image'" :src="att.previewUrl || att.url" alt="preview" />
        <!-- video -->
        <video v-else-if="att.kind === 'video'" :src="att.previewUrl || att.url" controls preload="metadata"></video>
        <!-- audio -->
        <div v-else-if="att.kind === 'audio'" class="att-audio">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M9 18V5l12-2v13"></path>
            <circle cx="6" cy="18" r="3"></circle>
            <circle cx="18" cy="16" r="3"></circle>
          </svg>
          <audio :src="att.previewUrl || att.url" controls preload="metadata"></audio>
        </div>
        <!-- generic file -->
        <div v-else class="att-file">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
          </svg>
          <span class="att-file-name truncate">{{ att.name }}</span>
        </div>
        <button class="att-remove" @click="removePendingAttachment(i)">&times;</button>
        <!-- 上传进度遮罩 -->
        <div v-if="att.uploading" class="att-progress">
          <div class="att-progress-bar"><span :style="{ width: att.progress + '%' }"></span></div>
          <div class="att-progress-text">{{ att.progress }}%</div>
        </div>
        <!-- 上传失败 -->
        <div v-else-if="att.failed" class="att-failed">
          <span>失败</span>
          <button class="att-retry" @click="retryUpload(att.id)">重试</button>
        </div>
      </div>
    </div>

    <!-- Textarea -->
    <div class="input-wrapper">
      <textarea
        ref="textareaRef"
        v-model="localInput"
        class="input-textarea"
        rows="1"
        placeholder="输入消息..."
        @input="autoResizeTextarea"
        @keydown.enter.exact.prevent="handleSendKey"
        @keydown.esc="closePopovers"
        @paste="handlePaste"
        @drop="onDrop"
        @dragover.prevent
        @dragleave="onDragleave"
      ></textarea>

      <div class="footer-actions">
        <!-- Left actions: tool selector, model selector, web search, settings -->
        <div class="footer-left">
          <button
            class="toolbar-button"
            title="选择模型"
            @click="toggleModelPopover"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
              <polyline points="2 17 12 22 22 17"></polyline>
              <polyline points="2 12 12 17 22 12"></polyline>
            </svg>
            {{ currentModel || '选择模型' }}
          </button>

          <button
            class="toolbar-button"
            :class="{ active: webSearchEnabled }"
            title="网页搜索"
            @click="toggleWebSearch"
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
            搜索
          </button>

          <button class="toolbar-button" title="添加文件" @click="toggleUploadPopover">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path>
            </svg>
            文件
          </button>

          <button class="toolbar-button" title="设置" @click="$emit('openSettings')">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="3"></circle>
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
            </svg>
          </button>
        </div>

        <!-- Right: send/stop -->
        <div class="footer-right">
          <button
            class="send-btn"
            title="发送消息"
            :disabled="(!localInput.trim() && pendingAttachments.length === 0) || pendingAttachments.some(a => a.uploading)"
            @click="handleSend()"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="22" y1="2" x2="11" y2="13"></line>
              <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
            </svg>
          </button>
        </div>
      </div>
    </div>
    <div class="ai-disclaimer">AI 生成内容仅供参考</div>

    <!-- Model popover -->
    <div v-if="showModelPopover" class="custom-popover model-popover" @click.stop>
      <div class="popover-header">
        <span>选择模型</span>
        <button class="popover-close" @click="closePopovers">&times;</button>
      </div>
      <div class="popover-body">
        <div v-if="modelsLoading" class="popover-loading">加载中...</div>
        <div v-else-if="modelsList.length === 0" class="popover-empty">暂无模型，请在设置中配置</div>
        <div
          v-for="(model, mi) in modelsList"
          :key="typeof model === 'string' ? model : model.id"
          class="model-option"
          :class="{ selected: currentModel === (typeof model === 'string' ? model : model.id) }"
          @click="selectModel(model)"
        >
          <span class="model-option-name">{{ typeof model === 'string' ? model : model.id }}</span>
        </div>
      </div>
    </div>

    <!-- Upload popover -->
    <div v-if="showUploadPopover" class="custom-popover upload-popover" @click.stop>
      <div class="popover-header">
        <span>添加文件</span>
        <button class="popover-close" @click="showUploadPopover = false">&times;</button>
      </div>
      <div class="popover-body">
        <div class="upload-option" @click="selectLocalFile">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="17 8 12 3 7 8"></polyline>
            <line x1="12" y1="3" x2="12" y2="15"></line>
          </svg>
          上传本地文件
        </div>
        <div class="upload-option" @click="selectServerFile">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="2" y="2" width="20" height="8" rx="2" ry="2"></rect>
            <rect x="2" y="14" width="20" height="8" rx="2" ry="2"></rect>
            <line x1="6" y1="6" x2="6.01" y2="6"></line>
            <line x1="6" y1="18" x2="6.01" y2="18"></line>
          </svg>
          从服务器选择文件
        </div>
      </div>
    </div>

    <!-- Hidden file inputs -->
    <input
      ref="fileInputRef"
      type="file"
      multiple
      style="display:none"
      @change="handleFileSelect"
    />

    <!-- Tool tip (first time hint) -->
    <div v-if="showToolTip" class="tool-tooltip">
      <div class="tool-tooltip-content">
        点击"工具"按钮可以选择可用的工具
      </div>
      <button class="tool-tooltip-close" @click="dismissToolTip">&times;</button>
    </div>
  </div>
</template>

<script>
import { ref, watch, onMounted, nextTick } from 'vue'

export default {
  name: 'ChatInput',
  props: {
    inputMessage: { type: String, default: '' },
    showModelPopover: { type: Boolean, default: false },
    showToolPopover: { type: Boolean, default: false },
    webSearchEnabled: { type: Boolean, default: false },
    selectedTools: { type: Array, default: () => [] },
    modelsList: { type: Array, default: () => [] },
    currentModel: { type: String, default: null },
    toolsList: { type: Array, default: () => [] },
    modelsLoading: { type: Boolean, default: false },
    toolsLoading: { type: Boolean, default: false },
    isSending: { type: Boolean, default: false }
  },
  emits: [
    'update:inputMessage',
    'update:showModelPopover',
    'update:showToolPopover',
    'update:webSearchEnabled',
    'sendMessage',
    'stopMessage',
    'removeSelectedTool',
    'selectModel',
    'toggleToolSelection',
    'batchToggleTools',
    'openSettings'
  ],
  setup(props, { emit, expose }) {
    const textareaRef = ref(null)
    const localInput = ref(props.inputMessage)
    const pendingAttachments = ref([])
    const showToolTip = ref(false)
    const showUploadPopover = ref(false)
    const fileInputRef = ref(null)

    watch(() => props.inputMessage, (val) => {
      localInput.value = val
      autoResizeTextarea()
    })

    // First-time tooltip
    onMounted(() => {
      if (!localStorage.getItem('showToolTooltip')) {
        showToolTip.value = true
      }
    })

    function dismissToolTip() {
      showToolTip.value = false
      localStorage.setItem('showToolTooltip', '1')
    }

    function autoResizeTextarea() {
      nextTick(() => {
        const el = textareaRef.value
        if (!el) return
        el.style.height = 'auto'
        el.style.height = Math.min(el.scrollHeight, 200) + 'px'
      })
    }

    function handleSendKey() {
      handleSend()
    }

    function handleSend() {
      // 发送中不再拦截：文本交给父组件进入待发送队列
      if ((!localInput.value.trim() && pendingAttachments.value.length === 0)) return
      emit('sendMessage', localInput.value)
      localInput.value = ''
      emit('update:inputMessage', '')
      clearAll()
    }

    function closePopovers() {
      emit('update:showModelPopover', false)
      emit('update:showToolPopover', false)
    }

    function toggleModelPopover() {
      emit('update:showModelPopover', !props.showModelPopover)
      emit('update:showToolPopover', false)
    }

    function toggleToolPopover() {
      emit('update:showToolPopover', !props.showToolPopover)
      emit('update:showModelPopover', false)
    }

    function toggleWebSearch() {
      emit('update:webSearchEnabled', !props.webSearchEnabled)
    }

    function selectModel(model) {
      emit('selectModel', model)
      emit('update:showModelPopover', false)
    }

    function toggleToolSelection(tool) {
      // Risk level check
      if (tool.risk_level && tool.risk_level !== 'low' && !localStorage.getItem('isRiskConfirm')) {
        const open = window.ai_tools?.open
        if (open) {
          open({
            title: '工具使用风险提示',
            btn: ['确定', '取消'],
            area: '440px',
            content: '<div class="clear_log_confirm pd20">此工具存在风险，是否继续使用？<br/><br/><label style="display:flex;align-items:center;gap:6px;"><input type="checkbox" class="isClose"> 不再提示</label></div>',
            yes: (idx, layero) => {
              const checked = layero?.find('.isClose')?.is(':checked')
              if (checked) localStorage.setItem('isRiskConfirm', 'true')
              emit('toggleToolSelection', tool)
            }
          })
          return
        }
      }
      emit('toggleToolSelection', tool)
    }

    function batchToggleTools(enabled) {
      emit('batchToggleTools', enabled)
    }

    function isToolSelected(tool) {
      return props.selectedTools.some(t => t.id === tool.id)
    }

    function removeSelectedTool(id) {
      emit('removeSelectedTool', id)
    }

    function handlePaste(e) {
      // Handle image paste
      const items = e.clipboardData?.items
      if (!items) return
      for (const item of items) {
        if (item.type.startsWith('image/')) {
          const file = item.getAsFile()
          if (file) {
            const reader = new FileReader()
            reader.onload = (ev) => {
              pushAttachment({
                kind: 'image',
                name: 'pasted-image.png',
                mime: 'image/png',
                previewUrl: ev.target.result,
                url: ev.target.result,
              })
            }
            reader.readAsDataURL(file)
          }
        }
      }
    }

    function onDrop(e) {
      e.preventDefault()
      const files = e.dataTransfer?.files
      if (!files || files.length === 0) return
      for (const file of files) {
        handleOneFile(file)
      }
    }

    function onDragleave() {
      // No-op for now
    }

    function fileKind(file) {
      const t = (file.type || '').toLowerCase()
      const ext = (file.name.split('.').pop() || '').toLowerCase()
      if (t.startsWith('image/')) return 'image'
      if (t.startsWith('video/')) return 'video'
      if (t.startsWith('audio/')) return 'audio'
      if (['mp4', 'webm', 'ogg', 'mov', 'mkv', 'avi'].includes(ext)) return 'video'
      if (['mp3', 'wav', 'flac', 'aac', 'm4a'].includes(ext)) return 'audio'
      return 'file'
    }

    function pushAttachment(att) {
      const item = {
        id: Date.now().toString() + Math.random().toString(36).slice(2, 6),
        kind: att.kind,
        name: att.name || 'file',
        mime: att.mime || '',
        previewUrl: att.previewUrl || '',
        url: att.url || '',
        path: att.path || '',
        file: att.file || null,
        uploading: false,
        progress: 0,
        failed: false,
      }
      pendingAttachments.value.push(item)
      return item
    }

    function removePendingAttachment(idx) {
      const att = pendingAttachments.value[idx]
      if (att && att.previewUrl && att.previewUrl.startsWith('blob:')) {
        URL.revokeObjectURL(att.previewUrl)
      }
      pendingAttachments.value.splice(idx, 1)
    }

    function getPendingAttachments() {
      return pendingAttachments.value
    }

    function clearAll() {
      for (const att of pendingAttachments.value) {
        if (att.previewUrl && att.previewUrl.startsWith('blob:')) {
          URL.revokeObjectURL(att.previewUrl)
        }
      }
      pendingAttachments.value = []
    }

    // ==================== File upload ====================
    function toggleUploadPopover() {
      showUploadPopover.value = !showUploadPopover.value
      closePopovers()
    }

    function selectLocalFile() {
      showUploadPopover.value = false
      fileInputRef.value?.click()
    }

    function selectServerFile() {
      showUploadPopover.value = false
      if (window.aiApp && typeof window.aiApp.select_path === 'function') {
        window.aiApp.select_path('aa22', 'all', (path) => {
          if (path) addServerFile(path)
        }, '/')
      } else {
        if (window.layer) window.layer.msg('文件选择器不可用', { icon: 2 })
      }
    }

    function addServerFile(path) {
      const name = path.split('/').pop() || path
      const ext = (name.split('.').pop() || '').toLowerCase()
      let kind = 'file'
      if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg'].includes(ext)) kind = 'image'
      else if (['mp4', 'webm', 'ogg', 'mov', 'mkv', 'avi'].includes(ext)) kind = 'video'
      else if (['mp3', 'wav', 'flac', 'aac', 'm4a'].includes(ext)) kind = 'audio'
      const url = '/api/files/raw?path=' + encodeURIComponent(path)
      pushAttachment({ kind, name, path, url })
    }

    function handleOneFile(file) {
      const kind = fileKind(file)
      // 本地预览（图片/视频/音频可即时预览）
      let previewUrl = ''
      if (kind === 'image' || kind === 'video' || kind === 'audio') {
        try { previewUrl = URL.createObjectURL(file) } catch (e) { previewUrl = '' }
      }
      const att = { kind, name: file.name, mime: file.type, previewUrl, file }
      const item = pushAttachment(att)
      // 上传到服务器，拿到可访问的 url 与 path
      uploadFileToServer(file, item.id)
    }

    function handleFileSelect(e) {
      const files = e.target.files
      if (!files) return
      for (const file of Array.from(files)) {
        handleOneFile(file)
      }
      e.target.value = ''
    }

    function uploadFileToServer(file, attId) {
      const att = pendingAttachments.value.find(a => a.id === attId)
      if (att) { att.uploading = true; att.progress = 0; att.failed = false }

      const fd = new FormData()
      fd.append('file', file)
      const xhr = new XMLHttpRequest()
      xhr.open('POST', '/api/files/upload')

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          const p = Math.round((e.loaded / e.total) * 100)
          const a = pendingAttachments.value.find(x => x.id === attId)
          if (a) a.progress = p
        }
      }
      xhr.onload = () => {
        const a = pendingAttachments.value.find(x => x.id === attId)
        if (!a) return
        a.uploading = false
        try {
          const r = JSON.parse(xhr.responseText)
          if (r.status && r.data) {
            a.url = r.data.url
            a.path = r.data.path
            a.name = r.data.name
            a.progress = 100
            a.failed = false
          } else {
            a.failed = true
            if (window.layer) window.layer.msg(r.msg || '上传失败', { icon: 2 })
          }
        } catch (err) {
          a.failed = true
          if (window.layer) window.layer.msg('上传响应解析失败', { icon: 2 })
        }
      }
      xhr.onerror = () => {
        const a = pendingAttachments.value.find(x => x.id === attId)
        if (a) {
          a.uploading = false
          a.failed = true
          if (window.layer) window.layer.msg('上传请求失败', { icon: 2 })
        }
      }
      xhr.send(fd)
    }

    function retryUpload(attId) {
      const a = pendingAttachments.value.find(x => x.id === attId)
      if (a && a.file) uploadFileToServer(a.file, attId)
    }

    // Expose methods to parent (App) via ref so it can read pending images/files
    expose({
      getPendingAttachments,
      clearAll,
      autoResizeTextarea
    })

    return {
      textareaRef,
      localInput,
      pendingAttachments,
      showToolTip,
      autoResizeTextarea,
      handleSend,
      handleSendKey,
      closePopovers,
      toggleModelPopover,
      toggleToolPopover,
      toggleWebSearch,
      selectModel,
      toggleToolSelection,
      batchToggleTools,
      isToolSelected,
      removeSelectedTool,
      handlePaste,
      onDrop,
      onDragleave,
      removePendingAttachment,
      retryUpload,
      getPendingAttachments,
      clearAll,
      dismissToolTip,
      toggleUploadPopover,
      selectLocalFile,
      selectServerFile,
      showUploadPopover,
      fileInputRef,
      handleFileSelect
    }
  }
}
</script>

<style scoped>
.chat-input {
  padding: 10px 10px 4px;
  background: #fff;
  position: relative;
  z-index: 101;
}

.truncate {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attachment-preview-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 12px 0;
}

.att-thumb {
  position: relative;
  width: 64px;
  height: 64px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  display: flex;
  align-items: center;
  justify-content: center;
}

.att-thumb img,
.att-thumb video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.att-audio {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 100%;
  color: #6b7280;
}

.att-audio audio {
  width: 150px;
  height: 28px;
}

.att-file {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  width: 100%;
  padding: 4px;
  color: #4b5563;
}

.att-file-name {
  font-size: 10px;
  max-width: 58px;
  text-align: center;
}

.att-remove {
  position: absolute;
  top: 2px;
  right: 2px;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.att-remove:hover {
  background: #ef4444;
}

.att-progress {
  position: absolute;
  inset: 0;
  background: rgba(255, 255, 255, 0.82);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  border-radius: 8px;
}

.att-progress-bar {
  width: 80%;
  height: 5px;
  border-radius: 3px;
  background: #e5e7eb;
  overflow: hidden;
}

.att-progress-bar span {
  display: block;
  height: 100%;
  background: #20a53a;
  transition: width 0.15s ease;
}

.att-progress-text {
  font-size: 11px;
  color: #374151;
}

.att-failed {
  position: absolute;
  inset: 0;
  background: rgba(254, 226, 226, 0.92);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  border-radius: 8px;
  font-size: 11px;
  color: #b91c1c;
}

.att-retry {
  border: none;
  background: #ef4444;
  color: #fff;
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 4px;
  cursor: pointer;
}

.att-retry:hover {
  background: #dc2626;
}

.input-wrapper {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 8px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 10px 12px;
  transition: all 0.2s;
}

.input-wrapper:focus-within {
  border-color: #a0aec0;
  box-shadow: 0 0 0 3px rgba(160, 174, 192, 0.1);
}

.input-textarea {
  width: 100%;
  border: none;
  background: transparent;
  outline: none;
  resize: none;
  font-size: 14px;
  line-height: 1.5;
  color: #1f2937;
  max-height: 120px;
  font-family: inherit;
  overflow-y: auto;
  word-wrap: break-word;
  white-space: pre-wrap;
}

.input-textarea:empty::before {
  content: attr(data-placeholder);
  color: #9ca3af;
  pointer-events: none;
}

.input-textarea .file-tag {
  display: inline-block;
  padding: 2px 6px;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: 4px;
  color: #20a53a;
  font-size: 13px;
  margin: 0 2px;
  cursor: default;
  vertical-align: bottom;
}

.footer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.footer-left {
  display: flex;
  gap: 4px;
  align-items: center;
}

.footer-right {
  display: flex;
  align-items: center;
}

.toolbar-button {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  background: transparent;
  border: none;
  border-radius: 6px;
  font-size: 12px;
  color: #6b7280;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}

.toolbar-button:hover {
  background: #f3f4f6;
  color: #374151;
}

.toolbar-button.active {
  color: #20a53a;
}

.toolbar-button.active:hover {
  color: #16a34a;
}

.toolbar-button svg {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
}

.badge-count {
  background: #20a53a;
  color: #fff;
  border-radius: 10px;
  font-size: 11px;
  min-width: 18px;
  height: 18px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 4px;
}

.send-btn {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  border: none;
  background: #20a53a;
  color: #fff;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
  flex-shrink: 0;
  margin-left: auto;
}

.send-btn:hover:not(:disabled) {
  opacity: 0.8;
  background: #20a53a;
}

.send-btn.stop-btn {
  background: #ef4444;
}

.send-btn.stop-btn:hover {
  background: #dc2626;
  opacity: 1;
}

.send-btn svg {
  width: 18px;
  height: 18px;
}

.ai-disclaimer {
  text-align: center;
  font-size: 11px;
  color: #9ca3af;
  padding: 4px 0;
  line-height: 1.2;
}

.custom-popover {
  position: absolute;
  bottom: 100%;
  left: 0;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.15);
  z-index: 100;
  min-width: 280px;
  max-width: 360px;
  margin-bottom: 8px;
  animation: popoverIn 0.2s ease-out;
}

@keyframes popoverIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.tool-popover {
  left: 0;
}

.model-popover {
  left: 80px;
}

.popover-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  border-bottom: 1px solid #f0f1f3;
  font-size: 14px;
  font-weight: 600;
  color: #1f2937;
}

.popover-close {
  width: 24px;
  height: 24px;
  border: none;
  background: transparent;
  color: #9ca3af;
  cursor: pointer;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
}

.popover-close:hover {
  background: #f3f4f6;
  color: #4b5563;
}

.popover-body {
  max-height: 320px;
  overflow-y: auto;
  padding: 8px;
}

.popover-batch-actions {
  display: flex;
  gap: 6px;
  padding: 4px 8px 8px;
}

.batch-btn {
  padding: 4px 12px;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  color: #6b7280;
  font-size: 12px;
  cursor: pointer;
}

.batch-btn:hover {
  background: #f0fdf4;
  border-color: #20a53a;
  color: #20a53a;
}

.popover-loading,
.popover-empty {
  padding: 20px;
  text-align: center;
  color: #9ca3af;
  font-size: 13px;
}

.tool-option,
.model-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}

.tool-option:hover,
.model-option:hover {
  background: #f9fafb;
}

.tool-option.selected,
.model-option.selected {
  background: #f0fdf4;
}

.tool-checkbox {
  width: 18px;
  height: 18px;
  border: 1px solid #d1d5db;
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  flex-shrink: 0;
  background: #fff;
}

.tool-option.selected .tool-checkbox {
  background: #20a53a;
  border-color: #20a53a;
}

.tool-option-name,
.model-option-name {
  font-size: 13px;
  color: #374151;
  flex-shrink: 0;
}

.tool-option-desc {
  font-size: 12px;
  color: #9ca3af;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-tooltip {
  position: absolute;
  bottom: 60px;
  left: 16px;
  background: #1f2937;
  color: #fff;
  border-radius: 8px;
  padding: 10px 14px;
  font-size: 12px;
  z-index: 200;
  display: flex;
  align-items: center;
  gap: 10px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
  animation: popoverIn 0.3s ease-out;
}

.tool-tooltip-content {
  white-space: normal;
  line-height: 1.4;
}

.tool-tooltip-close {
  background: transparent;
  border: none;
  color: #9ca3af;
  cursor: pointer;
  font-size: 16px;
}

/* Upload popover */
.upload-popover {
  left: 0;
  width: 220px;
}

.upload-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  cursor: pointer;
  transition: background 0.15s;
}

.upload-option:hover {
  background: #f9fafb;
}
</style>
