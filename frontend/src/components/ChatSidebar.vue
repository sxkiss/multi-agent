<template>
  <div class="chat-sidebar" :class="{ 'history-open': historyOpen }">
    <!-- Top actions bar -->
    <div class="top-actions">
      <div class="top-actions-left">
        <button class="top-btn new-chat-btn" title="新建对话" @click="newChat">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="12" y1="5" x2="12" y2="19"></line>
            <line x1="5" y1="12" x2="19" y2="12"></line>
          </svg>
          新建
        </button>
        <button class="top-btn mobile-history-toggle" title="历史对话" @click="historyOpen = !historyOpen">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
          </svg>
          历史
        </button>
      </div>
      <div class="top-actions-right">
        <button
          class="top-btn select-btn"
          :class="{ active: selectMode }"
          title="多选管理"
          @click="toggleSelectMode"
        >
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="9 11 12 14 22 4"></polyline>
            <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>
          </svg>
          {{ selectMode ? '取消' : '选择' }}
        </button>
        <button class="top-btn fullscreen-btn" title="全屏" @click="$emit('toggle-fullscreen')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"></path>
          </svg>
          全屏
        </button>
        <button class="top-btn settings-btn" title="全局设置" @click="$emit('open-settings')">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="3"></circle>
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06-.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06-.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
          </svg>
          设置
        </button>
      </div>
    </div>

    <!-- History list - grouped by workspace folder -->
    <div class="history-list">
      <div class="history-list-header">
        <label v-if="selectMode" class="select-all">
          <input type="checkbox" :checked="isAllSelected" @change="toggleSelectAll" />
          <span>全选</span>
        </label>
        <h4 v-else>对话历史</h4>
        <span class="history-count">{{ conversations.length }}</span>
      </div>

      <!-- 批量操作条 -->
      <div v-if="selectMode" class="batch-bar">
        <span class="batch-count">已选 {{ selectedIds.length }} 项</span>
        <button
          class="batch-del-btn"
          :disabled="selectedIds.length === 0"
          @click="deleteSelected"
        >
          删除选中
        </button>
      </div>

      <div v-if="conversations.length === 0" class="history-empty">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d1d5db" stroke-width="1.5">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
        </svg>
        <p>暂无对话</p>
      </div>

      <!-- 文件夹分组渲染（一级 = 主对话/子代理；二级 = 工作目录） -->
      <div v-else class="folder-groups">
        <div
          v-for="folder in folderGroups"
          :key="folder.path"
          class="folder-group"
        >
          <!-- 一级 folder 头 -->
          <div class="folder-header" @click="toggleFolder(folder.path)">
            <span class="folder-arrow" :class="{ expanded: folderOpenState[folder.path] }">▶</span>
            <svg class="folder-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
              <path d="M10 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2h-8l-2-2z"/>
            </svg>
            <span class="folder-name">{{ folder.name }}</span>
            <span class="folder-count">{{ (folder.children || folder.items).length }}</span>
          </div>

          <!-- 一级 folder 内容：二级 folder 列表 或 直接会话列表 -->
          <div v-show="folderOpenState[folder.path]" class="folder-items">
            <!-- 二级 folder（按工作目录）-->
            <template v-if="folder.children">
              <div v-for="sub in folder.children" :key="sub.path" class="folder-group sub-folder">
                <div class="folder-header sub-folder-header" @click.stop="toggleFolder(sub.path)">
                  <span class="folder-arrow" :class="{ expanded: folderOpenState[sub.path] }">▶</span>
                  <span class="folder-name">{{ sub.name }}</span>
                  <span class="folder-count">{{ sub.items.length }}</span>
                </div>
                <div v-show="folderOpenState[sub.path]" class="folder-items">
                  <div
                    v-for="conv in sub.items"
                    :key="conv.id"
                    class="conversation-item"
                    :class="{ active: currentConversationId === conv.id, selected: isSelected(conv.id), 'in-select': selectMode }"
                    @click="selectConv(conv)"
                  >
                    <label v-if="selectMode" class="conv-check" @click.stop>
                      <input type="checkbox" :checked="isSelected(conv.id)" @change="toggleSelect(conv.id)" />
                    </label>
                    <div class="conv-title">{{ conv.title || '新对话' }}</div>
                    <div class="conv-time">{{ conv.timeStr }}</div>
                    <button
                      v-if="!selectMode"
                      class="delete-btn"
                      title="删除对话"
                      @click.stop="$emit('delete-conversation', conv.id)"
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="3 6 5 6 21 6"></polyline>
                        <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path>
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            </template>
            <!-- 无二级：直接会话列表（兜底分支） -->
            <template v-else>
              <div
                v-for="conv in folder.items"
                :key="conv.id"
                class="conversation-item"
                :class="{ active: currentConversationId === conv.id, selected: isSelected(conv.id), 'in-select': selectMode }"
                @click="selectConv(conv)"
              >
                <label v-if="selectMode" class="conv-check" @click.stop>
                  <input type="checkbox" :checked="isSelected(conv.id)" @change="toggleSelect(conv.id)" />
                </label>
                <div class="conv-title">{{ conv.title || '新对话' }}</div>
                <div class="conv-time">{{ conv.timeStr }}</div>
                <button
                  v-if="!selectMode"
                  class="delete-btn"
                  title="删除对话"
                  @click.stop="$emit('delete-conversation', conv.id)"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <polyline points="3 6 5 6 21 6"></polyline>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path>
                  </svg>
                </button>
              </div>
            </template>
          </div>
        </div>
      </div>
    </div>

    <!-- Sidebar content -->
    <div class="sidebar-body">
    </div>
  </div>
</template>

<script>
export default {
  name: 'ChatSidebar',
  props: {
    conversations: { type: Array, default: () => [] },
    currentConversationId: { type: String, default: '' },
    isSending: { type: Boolean, default: false },
    chatMode: { type: String, default: 'group' },
    apiConfigured: { type: Boolean, default: false },
    getTimeAgo: { type: Function, default: () => () => '' }
  },
  emits: [
    'create-new',
    'delete-conversation',
    'delete-conversations',
    'load-conversation',
    'open-settings',
    'toggle-fullscreen'
  ],
  data() {
    return {
      selectMode: false,
      selectedIds: [],
      historyOpen: false,
      folderOpenState: {}
    }
  },
  computed: {
    isAllSelected() {
      return this.conversations.length > 0 && this.selectedIds.length === this.conversations.length
    },
    folderGroups() {
      // 统一分类规则：一级 = 主对话 / 子代理；二级 = 工作目录
      //  - group 模式：source=single && is_group → 集团对话(主)；source=crew → 集团子代理；其余 single → 单 Agent
      //  - single 模式：source=single && !is_group → 单 Agent 主对话；其余 → 子代理
      //  - opencode/claude/codex 模式：source=<mode> → 主对话（CLI 无子代理概念）
      // 每个一级分类下，按 workspace（工作目录）再细分二级，未指定目录归入「未分类目录」
      const folders = []
      const mode = this.chatMode || 'group'

      // 把一组会话按工作目录分组，返回二级 folder 列表
      const byWorkspace = (items, label) => {
        const groups = {}
        const order = []
        for (const conv of items) {
          const ws = (conv.workspace || '').trim()
          const key = ws ? ws : '未分类目录'
          if (!groups[key]) {
            groups[key] = []
            order.push(key)
          }
          groups[key].push(conv)
        }
        // 二级 folder 的 path 用一级 path 前缀，避免展开态冲突
        return order.map(k => ({
          path: `${label}::${k}`,
          name: k,
          isSub: true,
          parent: label,
          items: groups[k],
        }))
      }

      if (mode === 'group') {
        // 主对话（集团对话） + 单 Agent（主对话）
        const mainChats = this.conversations.filter(
          c => c.source === 'single' && c.is_group
        )
        const singleChats = this.conversations.filter(
          c => c.source === 'single' && !c.is_group
        )
        // 子代理（集团子代理）
        const subChats = this.conversations.filter(c => c.source === 'crew')

        if (mainChats.length > 0) {
          folders.push({
            path: '__group_main__',
            name: '集团对话（主对话）',
            items: [],
            children: byWorkspace(mainChats, '__group_main__'),
          })
        }
        if (singleChats.length > 0) {
          folders.push({
            path: '__single_main__',
            name: '单 Agent（主对话）',
            items: [],
            children: byWorkspace(singleChats, '__single_main__'),
          })
        }
        if (subChats.length > 0) {
          folders.push({
            path: '__group_sub__',
            name: '集团子代理',
            items: [],
            children: byWorkspace(subChats, '__group_sub__'),
          })
        }
      } else if (mode === 'single') {
        const mainChats = this.conversations.filter(
          c => c.source === 'single' && !c.is_group
        )
        const subChats = this.conversations.filter(
          c => c.source !== 'single' || c.is_group
        )
        if (mainChats.length > 0) {
          folders.push({
            path: '__single_main__',
            name: '单 Agent 主对话',
            items: [],
            children: byWorkspace(mainChats, '__single_main__'),
          })
        }
        if (subChats.length > 0) {
          folders.push({
            path: '__single_sub__',
            name: '子代理',
            items: [],
            children: byWorkspace(subChats, '__single_sub__'),
          })
        }
      } else if (['opencode', 'claude', 'codex'].includes(mode)) {
        // CLI 模式：source=<mode> 全部作为主对话；其它来源（如历史混合）作子代理
        const mainChats = this.conversations.filter(c => c.source === mode)
        const subChats = this.conversations.filter(c => c.source !== mode && c.source)
        if (mainChats.length > 0) {
          folders.push({
            path: `__${mode}_main__`,
            name: `${mode} 主对话`,
            items: [],
            children: byWorkspace(mainChats, `__${mode}_main__`),
          })
        }
        if (subChats.length > 0) {
          folders.push({
            path: `__${mode}_sub__`,
            name: '子代理',
            items: [],
            children: byWorkspace(subChats, `__${mode}_sub__`),
          })
        }
      } else {
        // 兜底：按原逻辑
        const mainChats = this.conversations.filter(
          c => c.source === 'single' && c.is_group
        )
        const singleChats = this.conversations.filter(
          c => c.source === 'single' && !c.is_group
        )
        const subChats = this.conversations.filter(c => c.source === 'crew')
        if (mainChats.length > 0) folders.push({ path: '__group_main__', name: '集团对话（主对话）', items: mainChats })
        if (singleChats.length > 0) folders.push({ path: '__single_main__', name: '单 Agent（主对话）', items: singleChats })
        if (subChats.length > 0) folders.push({ path: '__group_sub__', name: '集团子代理', items: subChats })
      }
      return folders
    }
  },
  watch: {
    conversations() {
      // 列表变化（如删除后）时清理已不存在的选中项
      const ids = new Set(this.conversations.map(c => c.id))
      this.selectedIds = this.selectedIds.filter(id => ids.has(id))
    }
  },
  methods: {
    toggleFolder(path) {
      this.folderOpenState = { ...this.folderOpenState, [path]: !this.folderOpenState[path] }
    },
    _folderName(path) {
      // 新格式 "模式 · 路径" 直接展示；旧格式纯路径取最后一级
      if (!path) return '默认'
      if (path.includes(' · ')) return path
      const parts = path.replace(/\\+/g, '/').split('/')
      return parts[parts.length - 1] || path
    },
    newChat() {
      this.historyOpen = false
      this.$emit('create-new')
    },
    selectConv(conv) {
      if (this.selectMode) {
        this.toggleSelect(conv.id)
      } else {
        this.historyOpen = false
        this.$emit('load-conversation', conv.id)
      }
    },
    toggleSelectMode() {
      this.selectMode = !this.selectMode
      if (!this.selectMode) this.selectedIds = []
    },
    isSelected(id) {
      return this.selectedIds.includes(id)
    },
    toggleSelect(id) {
      const i = this.selectedIds.indexOf(id)
      if (i >= 0) this.selectedIds.splice(i, 1)
      else this.selectedIds.push(id)
    },
    toggleSelectAll(e) {
      if (e && e.target && e.target.checked) {
        this.selectedIds = this.conversations.map(c => c.id)
      } else {
        this.selectedIds = []
      }
    },
    deleteSelected() {
      if (this.selectedIds.length === 0) return
      const ids = this.selectedIds.slice()
      this.$emit('delete-conversations', ids)
    }
  }
}
</script>

<style scoped>
.chat-sidebar {
  width: 240px;
  background: #fff;
  border-right: 1px solid #e5e7eb;
  display: flex;
  flex-direction: column;
  position: relative;
  flex-shrink: 0;
  height: 100vh;
}

.top-actions {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px;
  border-bottom: 1px solid #e5e7eb;
  gap: 8px;
  flex-shrink: 0;
}

.top-actions-left,
.top-actions-right {
  display: flex;
  gap: 8px;
}

.top-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #6b7280;
  cursor: pointer;
  transition: all 0.2s;
  font-size: 13px;
  white-space: nowrap;
}

.top-btn:hover {
  background: #f0fdf4;
  border-color: #20a53a;
  color: #20a53a;
  transform: translateY(-1px);
  box-shadow: 0 2px 6px rgba(32, 165, 58, 0.2);
}

.top-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
  background: #f9fafb;
  border-color: #e5e7eb;
  color: #9ca3af;
  transform: none;
  box-shadow: none;
}

.history-list {
  flex: 1;
  overflow-y: auto;
  border-bottom: 1px solid #e5e7eb;
  min-height: 0;
}

.history-list-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px 8px;
  border-bottom: 1px solid #f0f1f3;
  background: linear-gradient(to bottom, #fafbfc, #fff);
  position: sticky;
  top: 0;
  z-index: 1;
}

.select-all {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: #1f2937;
  cursor: pointer;
}

.select-all input {
  width: 15px;
  height: 15px;
  cursor: pointer;
}

.batch-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: #f0fdf4;
  border-bottom: 1px solid #bbf7d0;
}

.batch-count {
  font-size: 12px;
  color: #166534;
}

.batch-del-btn {
  padding: 5px 14px;
  border: none;
  border-radius: 6px;
  background: #ef4444;
  color: #fff;
  font-size: 12px;
  cursor: pointer;
}

.batch-del-btn:disabled {
  background: #f3f4f6;
  color: #9ca3af;
  cursor: not-allowed;
}

.batch-del-btn:not(:disabled):hover {
  background: #dc2626;
}

.history-list-header h4 {
  font-size: 13px;
  font-weight: 600;
  color: #1f2937;
  margin: 0;
}

.history-count {
  font-size: 11px;
  color: #9ca3af;
  background: #f3f4f6;
  padding: 2px 8px;
  border-radius: 10px;
}

.history-empty {
  text-align: center;
  padding: 40px 20px;
  color: #9ca3af;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
  background: #fafbfc;
  border-radius: 12px;
  margin: 8px;
}

.history-empty svg {
  opacity: 0.5;
}

/* 文件夹分组 */
.folder-groups {
  padding: 4px 0;
}

.folder-group {
  margin-bottom: 2px;
}

.folder-header {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 12px;
  cursor: pointer;
  user-select: none;
  font-size: 11px;
  font-weight: 600;
  color: #6b7280;
  transition: background 0.15s;
}

.folder-header:hover {
  background: #f3f4f6;
}

.folder-arrow {
  font-size: 9px;
  transition: transform 0.2s;
  width: 10px;
  flex-shrink: 0;
}

.folder-arrow.expanded {
  transform: rotate(90deg);
}

.folder-icon {
  color: #f59e0b;
  flex-shrink: 0;
}

.folder-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-count {
  font-size: 10px;
  color: #9ca3af;
  background: #f3f4f6;
  padding: 1px 6px;
  border-radius: 8px;
  flex-shrink: 0;
}

.folder-items {
  padding-left: 8px;
}
.sub-folder {
  margin-top: 2px;
}
.sub-folder-header {
  padding: 4px 8px 4px 16px;
  font-size: 12px;
  color: #6b7280;
  background: transparent;
}
.sub-folder-header:hover {
  background: #f9fafb;
}

/* 会话条目 */
.conversation-item {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;
  margin: 1px 4px;
  position: relative;
}

.conversation-item.selected {
  background: #ecfdf5;
  border: 1px solid #34d399;
}

.conv-check {
  position: absolute;
  left: 4px;
  top: 50%;
  transform: translateY(-50%);
  display: inline-flex;
  align-items: center;
}

.conv-check input {
  width: 14px;
  height: 14px;
  cursor: pointer;
}

.conversation-item.selected .conv-title,
.conversation-item.selected .conv-time {
  padding-left: 20px;
}

.conversation-item.in-select .conv-title,
.conversation-item.in-select .conv-time {
  padding-left: 20px;
}

.conversation-item:hover {
  background: #f9fafb;
}

.conversation-item.active {
  background: #f0fdf4;
  border: 1px solid #20a53a;
}

.conv-title {
  font-size: 12px;
  color: #1f2937;
  margin-bottom: 2px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-right: 18px;
}

.conv-time {
  font-size: 10px;
  color: #9ca3af;
}

.delete-btn {
  position: absolute;
  right: 6px;
  top: 50%;
  transform: translateY(-50%);
  width: 20px;
  height: 20px;
  border-radius: 5px;
  border: none;
  background: transparent;
  color: #9ca3af;
  cursor: pointer;
  display: none;
  align-items: center;
  justify-content: center;
}

.conversation-item:hover .delete-btn {
  display: flex;
}

.delete-btn:hover {
  background: #fee2e2;
  color: #ef4444;
}

.sidebar-body {
  flex-shrink: 0;
  padding: 12px;
  border-top: 1px solid #e5e7eb;
}

/* 移动端历史切换按钮 */
.mobile-history-toggle {
  display: none;
}

@media (max-width: 768px) {
  .mobile-history-toggle {
    display: inline-flex;
  }
  .chat-sidebar.history-open .history-list {
    display: block;
    max-height: 50vh;
    overflow-y: auto;
    margin-top: 6px;
    border-top: 1px solid #e5e7eb;
    padding-top: 6px;
  }
}
</style>
