<template>
  <div class="settings-drawer-overlay" :class="{ show: visible }" @click="handleOverlayClick">
    <div class="settings-drawer" :class="{ show: visible }" @click.stop>
      <div class="drawer-header">
        <h3>全局参数配置</h3>
        <button class="close-btn" @click="handleClose">&times;</button>
      </div>
      
      <div class="drawer-content">
        <!-- AI 接口与模型 -->
        <div class="form-section-group">
          <h4 class="section-title">AI 接口与模型</h4>

          <div class="form-section">
            <label class="form-label">API Base URL</label>
            <input 
              type="text" 
              class="form-input" 
              v-model="formData.api_base_url"
              placeholder="https://api.openai.com/v1"
            />
          </div>

          <div class="form-section">
            <label class="form-label">API Key</label>
            <input 
              type="password" 
              class="form-input" 
              v-model="formData.api_key"
              placeholder="请输入 API Key"
            />
          </div>

          <div class="form-section">
            <label class="form-label">模型（勾选启用）</label>
            <div v-if="loadingModels" class="loading">
              <span>加载中...</span>
            </div>
            <div v-else-if="fetchError" class="error-message">
              {{ fetchError }}
            </div>
            <div v-else-if="availableModels.length === 0" class="empty-models">
              未获取到模型列表
            </div>
            <div v-else class="models-checkbox-group">
              <label v-for="model in availableModels" :key="model.id" class="model-option">
                <input 
                  type="checkbox" 
                  :value="model.id"
                  v-model="formData.models"
                />
                <span class="model-name">{{ model.id }}</span>
              </label>
            </div>
            <button 
              class="btn-secondary"
              style="margin-top:8px"
              :disabled="loadingModels"
              @click="fetchModels"
            >
              {{ loadingModels ? '获取中...' : '获取模型列表' }}
            </button>
          </div>

          <div class="form-section">
            <label class="form-label">默认模型</label>
            <select class="form-input" v-model="formData.default_model">
              <option value="">（使用第一个勾选的模型）</option>
              <option v-for="m in formData.models" :key="m" :value="m">{{ m }}</option>
            </select>
          </div>
        </div>

        <!-- MCP -->
        <div class="form-section-group">
          <h4 class="section-title">MCP 服务</h4>
          <div class="form-section">
            <label class="switch-label">
              <input type="checkbox" class="switch-input" v-model="formData.enable_mcp" />
              <span class="switch-slider"></span>
              <span class="switch-text">启用 MCP 集成</span>
            </label>
          </div>
          <div class="form-section">
            <label class="form-label">MCP 配置文件路径</label>
            <p class="form-hint">留空使用项目内 mcp/config.json</p>
            <input
              type="text"
              class="form-input"
              v-model="formData.mcp_config_path"
              placeholder="mcp/config.json"
            />
          </div>

          <!-- MCP Server 管理（读写 mcp/config.json + 市场） -->
          <div class="mcp-manage">
            <div class="mcp-manage-bar">
              <button class="btn-secondary mcp-refresh" :disabled="loadingMcp" @click="fetchMcpServers">
                {{ loadingMcp ? '加载中...' : '⟳ 刷新' }}
              </button>
              <button class="btn-secondary mcp-market-btn" :disabled="loadingMarket" @click="fetchMcpMarket">
                {{ loadingMarket ? '拉取中...' : '🛒 MCP 市场' }}
              </button>
            </div>

            <div v-if="loadingMcp" class="loading">加载中...</div>
            <div v-else-if="!mcpServers" class="empty-models">暂无 MCP server</div>
            <div v-else>
              <div v-if="mcpServers.items && mcpServers.items.length" class="mcp-list">
                <div v-for="srv in mcpServers.items" :key="srv.name" class="tool-item">
                  <span class="tool-item-name">{{ srv.name }}</span>
                  <span class="tool-item-desc">{{ srv.type }} · {{ srv.command || srv.url }}</span>
                  <span v-if="srv.has_env" class="mcp-env-badge">env</span>
                  <button class="crew-del" @click="removeMcpServer(srv)">移除</button>
                </div>
              </div>

              <!-- 新增 server -->
              <div class="mcp-add-form">
                <input class="form-input mcp-name-input" v-model="newMcpName" placeholder="server 名称" />
                <textarea
                  class="form-input mcp-config-input"
                  v-model="newMcpConfig"
                  rows="3"
                  placeholder='配置 JSON，例如：{"command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/path"]}'
                ></textarea>
                <label class="overwrite-label">
                  <input type="checkbox" v-model="newMcpOverwrite" /> 同名覆盖
                </label>
                <button
                  class="btn-primary mcp-add-btn"
                  :disabled="addingMcp || !newMcpName.trim() || !newMcpConfig.trim()"
                  @click="addMcpServer"
                >{{ addingMcp ? '添加中...' : '＋ 添加' }}</button>
                <div v-if="mcpMsg" class="skill-install-msg" :class="{ err: mcpMsgErr }">{{ mcpMsg }}</div>
              </div>
            </div>

            <!-- 市场浏览 -->
            <div v-if="mcpMarket" class="mcp-market">
              <div class="mcp-market-head">
                <span>{{ mcpMarket.msg }}</span>
                <button class="crew-del" @click="mcpMarket = null">收起</button>
              </div>
              <div class="mcp-market-list">
                <div v-for="m in mcpMarket.items" :key="m.name" class="mcp-market-item">
                  <div class="mcp-market-meta">
                    <span class="tool-item-name">{{ m.name }}</span>
                    <span v-if="m.env_vars && m.env_vars.length" class="mcp-env-badge" title="需要环境变量">🔑 需 env</span>
                    <span v-if="m.transport" class="mcp-env-badge">{{ m.transport }}</span>
                    <span v-else-if="m._package && m._package.runtime_hint" class="mcp-env-badge">{{ m._package.runtime_hint }}</span>
                  </div>
                  <div class="tool-item-desc">{{ m.description }}</div>
                  <div v-if="m.env_vars && m.env_vars.length" class="mcp-tags">
                    <span v-for="v in m.env_vars" :key="v" class="mcp-tag">{{ v }}</span>
                  </div>
                  <div class="mcp-market-actions">
                    <button class="btn-primary mcp-install-btn" :disabled="installingMcpName === m.name" @click="installMarketMcp(m)">
                      {{ installingMcpName === m.name ? '安装中...' : '⚡ 一键安装' }}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- 工作目录设置 -->
        <div class="form-section-group">
          <h4 class="section-title">工作目录</h4>
          <div class="form-section">
            <label class="form-label">工作目录路径</label>
            <p class="form-hint">Agent 执行命令的默认目录，留空使用项目 workspace/ 目录</p>
            <input
              type="text"
              class="form-input"
              v-model="formData.workspace"
              placeholder="~/workspace 或 /path/to/your/project"
            />
          </div>
        </div>

        <!-- 上下文窗口设置 -->
        <div class="form-section-group">
          <h4 class="section-title">上下文窗口</h4>
          <div class="form-section">
            <label class="form-label">上下文窗口大小 (KB)</label>
            <p class="form-hint">每次请求发送给模型的最大上下文大小，建议 256-2048</p>
            <input
              type="number"
              class="form-input"
              v-model="formData.context_window_kb"
              placeholder="512"
              min="64"
              max="4096"
            />
          </div>
        </div>

        <!-- 搜索设置 -->
        <div class="form-section-group">
          <h4 class="section-title">联网搜索</h4>
          <div class="form-section">
            <label class="form-label">默认搜索引擎</label>
            <p class="form-hint">
              WebSearch 工具默认使用的引擎。Bing 免 Key 且实测可用（无需代理即可直连）；
              SearXNG 免 Key 但需自建实例。生产环境建议用需 Key 的引擎以获得更稳定结果。
            </p>
            <select class="form-input" v-model="searchConfig.provider">
              <option v-for="(p, name) in searchConfig.providers" :key="name" :value="name">
                {{ p.label }}{{ p.need_key ? '（需 Key）' : '（免 Key）' }}
              </option>
            </select>
          </div>
          <div class="form-section">
            <label class="form-label">默认返回条数</label>
            <p class="form-hint">每次搜索返回的结果条数，1-20</p>
            <input
              type="number"
              class="form-input"
              v-model.number="searchConfig.result_size"
              min="1"
              max="20"
            />
          </div>
          <!-- 只显示当前选中引擎的配置项，避免 19 个引擎的表单全铺开 -->
          <div v-if="currentSearchProvider" class="form-section">
            <label class="form-label">{{ currentSearchProvider.label }} 配置</label>
            <p class="form-hint" v-if="currentSearchProvider.need_key">
              API Key 不会回显（只显示是否已配置）。<strong>留空表示不修改</strong>，填入新值才会覆盖。
            </p>
            <p class="form-hint" v-else>
              该引擎无需 API Key。
            </p>
            <div v-if="'api_key' in currentSearchProvider.options" class="key-row">
              <input
                type="password"
                class="form-input"
                v-model="currentSearchProvider.options.api_key"
                :placeholder="currentSearchProvider.options.api_key_set ? '已配置（留空保持原值）' : '未配置，填入 API Key'"
                autocomplete="new-password"
              />
              <span v-if="currentSearchProvider.options.api_key_set" class="key-badge">已配置</span>
            </div>
            <input
              v-if="'url' in currentSearchProvider.options"
              type="text"
              class="form-input"
              v-model="currentSearchProvider.options.url"
              :placeholder="currentSearchProvider.key === 'searxng' ? '实例地址，如 https://searx.example.com' : 'API 地址（可选，指向自建网关）'"
            />
            <input
              v-if="'engine' in currentSearchProvider.options"
              type="text"
              class="form-input"
              v-model="currentSearchProvider.options.engine"
              placeholder="引擎名（可选，逗号分隔）"
            />
            <input
              v-if="'language' in currentSearchProvider.options"
              type="text"
              class="form-input"
              v-model="currentSearchProvider.options.language"
              placeholder="Accept-Language，如 zh-CN,zh"
            />
            <input
              v-if="'model' in currentSearchProvider.options"
              type="text"
              class="form-input"
              v-model="currentSearchProvider.options.model"
              placeholder="模型名，如 grok-3"
            />
            <select v-if="'depth' in currentSearchProvider.options" class="form-input" v-model="currentSearchProvider.options.depth">
              <option value="basic">basic（快）</option>
              <option value="advanced">advanced（深）</option>
              <option value="standard">standard（标准）</option>
            </select>
            <select v-if="'mode' in currentSearchProvider.options" class="form-input" v-model="currentSearchProvider.options.mode">
              <option value="custom">custom（自定义搜索）</option>
              <option value="ai">ai（AI 搜索）</option>
            </select>
          </div>
          <div class="form-section">
            <button class="btn-save" @click="saveSearchConfig" :disabled="searchSaving">
              {{ searchSaving ? '保存中...' : '保存搜索设置' }}
            </button>
            <span v-if="searchMsg" class="form-hint" :class="{ 'err': searchMsgErr }">{{ searchMsg }}</span>
          </div>
        </div>

        <!-- 记忆/知识库设置 -->
        <div class="form-section-group">
          <h4 class="section-title">记忆 / 知识库</h4>
          <div class="form-section">
            <label class="form-label">全局记忆 Agent ID</label>
            <p class="form-hint">写入/检索 mem0 记忆时使用的 agent_id，便于跨 Agent 隔离与共享（默认 ai-agent）</p>
            <input
              type="text"
              class="form-input"
              v-model="formData.global_kb_agent_id"
              placeholder="ai-agent"
            />
          </div>
          <div class="form-section">
            <label class="form-label">mem0 服务地址</label>
            <p class="form-hint">mem0 HTTP API 地址，留空使用本机 http://localhost:8000</p>
            <input
              type="text"
              class="form-input"
              v-model="formData.mem0_api_url"
              placeholder="http://localhost:8000"
            />
          </div>
          <div class="form-section">
            <label class="form-label">RAG 记忆检索模式</label>
            <p class="form-hint">控制对话前是否从知识库检索相关记忆注入上下文</p>
            <select class="form-input" v-model="formData.rag_mode">
              <option value="disabled">关闭 - 不使用记忆检索</option>
              <option value="global_only">启用全局记忆检索（仅本机 mem0）</option>
              <option value="external_only">启用外部知识库检索（ai-assistant.cn）</option>
            </select>
          </div>
        </div>

        <!-- Agent 参数设置 -->
        <div class="form-section-group">
          <h4 class="section-title">Agent 参数</h4>
          <div class="form-section">
            <label class="form-label">思考程度 (Reasoning Effort)</label>
            <p class="form-hint">控制模型的推理深度，max=最大深度思考，high=深度思考</p>
            <select
              class="form-input"
              v-model="formData.agent.reasoning_effort"
            >
              <option value="low">low - 轻量推理</option>
              <option value="medium">medium - 中等推理</option>
              <option value="high">high - 深度推理</option>
              <option value="xhigh">xhigh - 超深度推理</option>
              <option value="max">max - 最大深度推理</option>
            </select>
          </div>
          <div class="form-section">
            <label class="form-label">思考温度 (Temperature)</label>
            <p class="form-hint">控制输出的随机性，0.0=确定性，1.0=高随机性，建议 0.7-0.9</p>
            <input
              type="number"
              class="form-input"
              v-model="formData.agent.temperature"
              placeholder="0.9"
              min="0"
              max="2"
              step="0.1"
            />
          </div>
          <div class="form-section">
            <label class="form-label">Top P</label>
            <p class="form-hint">核采样概率，0.8 表示从概率最高的 80% 词汇中采样</p>
            <input
              type="number"
              class="form-input"
              v-model="formData.agent.top_p"
              placeholder="0.8"
              min="0"
              max="1"
              step="0.1"
            />
          </div>
        </div>

        <!-- Crew Agents Management -->
        <div class="form-section-group">
          <h4 class="section-title">子代理团队（Crew）</h4>
          <p class="form-hint">每个成员自带工具；可按部门组织。对话中经理自动分派。</p>

          <!-- 部门管理 -->
          <details class="crew-form-wrap">
            <summary class="crew-add-btn">＋ 新建部门</summary>
            <div class="crew-form">
              <input class="form-input" v-model="deptForm.name" placeholder="标识 (小写字母数字_-，如 dev)" />
              <input class="form-input" v-model="deptForm.title" placeholder="部门名称 (如 研发部)" />
              <input class="form-input" v-model="deptForm.description" placeholder="职责说明（可选）" />
              <button class="btn-primary" @click="saveDept">保存部门</button>
            </div>
          </details>

          <div class="crew-list" style="margin-top:10px">
            <div v-for="d in departments" :key="d.name" class="crew-item">
              <span class="crew-name">{{ d.title }}</span>
              <span class="crew-badge custom">{{ d.name }}</span>
              <button class="crew-del" @click="deleteDept(d.name)">删除</button>
            </div>
          </div>

          <!-- 成员管理 -->
          <div class="crew-list">
            <div v-if="loadingCrew" class="loading">加载中...</div>
            <template v-else>
              <div v-for="a in crewAgents" :key="a.name" class="crew-item">
                <div class="crew-info">
                  <span class="crew-name">{{ a.name }}</span>
                  <span v-if="a.builtin" class="crew-badge builtin">内置</span>
                  <span v-else class="crew-badge custom">自定义</span>
                  <span class="crew-desc" :title="'工具: ' + (a.tools||[]).join(', ')">{{ a.description }}</span>
                </div>
                <select
                  v-if="!a.builtin"
                  class="crew-dept-select"
                  :value="a.department || ''"
                  @change="assignDept(a.name, $event.target.value)"
                >
                  <option value="">未分配</option>
                  <option v-for="d in departments" :key="d.name" :value="d.name">{{ d.title }}</option>
                </select>
                <button
                  v-if="!a.builtin"
                  class="crew-del"
                  @click="deleteCrewAgent(a.name)"
                >删除</button>
              </div>
            </template>
          </div>

          <details class="crew-form-wrap">
            <summary class="crew-add-btn">＋ 新建成员代理</summary>
            <div class="crew-form">
              <input class="form-input" v-model="crewForm.name" placeholder="名称 (小写字母数字_-, 如 data-analyst)" />
              <input class="form-input" v-model="crewForm.department" placeholder="所属部门标识 (可选, 如 research)" list="dept-list" />
              <datalist id="dept-list">
                <option v-for="d in departments" :key="d.name" :value="d.name">{{ d.title }}</option>
              </datalist>
              <input class="form-input" v-model="crewForm.description" placeholder="职责描述 (Goal)" />
              <textarea class="form-textarea" rows="3" v-model="crewForm.backstory" placeholder="系统提示词 (Backstory)"></textarea>
              <select class="form-input" v-model="crewForm.model">
                <option value="">模型：继承父会话</option>
                <option v-for="m in formData.models" :key="m" :value="m">{{ m }}</option>
              </select>
              <div class="crew-tools">
                <label v-for="t in toolsData" :key="t.id" class="crew-tool-chk">
                  <input type="checkbox" :value="t.id" v-model="crewForm.tools" /> {{ t.id }}
                </label>
              </div>
              <button class="btn-primary" :disabled="savingCrew" @click="saveCrewAgent">
                {{ savingCrew ? '保存中...' : '保存成员' }}
              </button>
              <div v-if="crewMsg" class="skill-install-msg" :class="{ err: crewMsgErr }">{{ crewMsg }}</div>
            </div>
          </details>
        </div>

        <!-- Skill List Management -->
        <div class="form-section-group">
          <h4 class="section-title">技能（Skill）</h4>
          <div class="skill-install-bar">
            <button class="btn-secondary" :disabled="installingSkill" @click="$refs.skillFileInput.click()">
              {{ installingSkill ? '安装中...' : '＋ 安装技能 (ZIP)' }}
            </button>
            <label class="overwrite-label">
              <input type="checkbox" v-model="overwriteSkill" /> 同名覆盖
            </label>
            <input
              ref="skillFileInput"
              type="file"
              accept=".zip,application/zip"
              style="display:none"
              @change="handleSkillUpload"
            />
          </div>
          <div class="skill-url-bar">
            <input
              class="form-input skill-url-input"
              v-model="skillUrl"
              placeholder="https://... .zip 或 GitHub 仓库地址"
              @keyup.enter="installFromUrl"
            />
            <button class="btn-secondary" :disabled="installingUrl || !skillUrl.trim()" @click="installFromUrl">
              {{ installingUrl ? '下载中...' : 'URL 安装' }}
            </button>
          </div>
          <div v-if="skillInstallMsg" class="skill-install-msg" :class="{ err: skillInstallErr }">{{ skillInstallMsg }}</div>
          <div class="skill-install-hint">技能包需包含 SKILL.md。安装方法可在对话中询问 AI。</div>

          <div v-if="loadingSkills" class="loading">加载中...</div>
          <div v-else-if="!skillsData" class="empty-models">暂无技能</div>
          <div v-else class="skill-group">
            <div v-if="skillsData.skills && skillsData.skills.length > 0" class="skill-sub-list">
              <div v-for="skill in skillsData.skills" :key="skill.name" class="tool-item">
                <span class="tool-item-name">{{ skill.name }}</span>
                <span class="tool-item-desc">{{ skill.description }}</span>
                <button
                  v-if="canUninstall(skill.name)"
                  class="crew-del"
                  @click="uninstallSkill(skill)"
                >卸载</button>
                <label class="switch-label">
                  <input
                    type="checkbox"
                    class="switch-input"
                    :checked="skill.enabled"
                    @change="toggleSkill(skill)"
                  />
                  <span class="switch-slider"></span>
                </label>
              </div>
            </div>
          </div>
        </div>

        <!-- Save Button -->
        <div style="display:flex;gap:10px">
          <button class="btn-primary btn-save" style="flex:1" @click="handleSave">
            保存设置
          </button>
          <button class="btn-secondary btn-save" style="flex:0 0 auto" :disabled="reloading" @click="doHotReload" title="不重启服务，立即生效外部修改（工具/配置/团队/MCP）">
            {{ reloading ? '重载中...' : '⟳ 热重载' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
<script>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'

export default {
  name: 'SettingsDrawer',
  props: {
    visible: {
      type: Boolean,
      default: false
    },
    config: {
      type: Object,
      default: () => ({})
    }
  },
  emits: ['close', 'save'],
  setup(props, { emit }) {
    // 独立部署 API 封装（不依赖宝塔面板 window.ai_tools）
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
    const apiGet = (url, cb) => apiCall('GET', url, null, cb)
    const apiPost = (url, body, cb) => apiCall('POST', url, body, cb)

    // M-6: 集中登记所有 setTimeout 句柄，组件卸载时统一清理，避免回调操作已销毁的组件
    const pendingTimers = new Set()
    const safeTimeout = (fn, delay) => {
      const id = setTimeout(() => {
        pendingTimers.delete(id)
        fn()
      }, delay)
      pendingTimers.add(id)
      return id
    }

    const formData = ref({
      api_base_url: '',
      api_key: '',
      models: [],
      default_model: '',
      enable_mcp: true,
      mcp_config_path: '',
      workspace: '',
      context_window_kb: 512,
      global_kb_agent_id: 'ai-agent',
      mem0_api_url: 'http://localhost:8000',
      use_global_rag: false,
      use_external_kb: false,
      rag_mode: 'disabled',
      agent: {
        temperature: 0.9,
        top_p: 0.8,
        reasoning_effort: 'max'
      }
    })

    const availableModels = ref([])
    const loadingModels = ref(false)
    const fetchError = ref('')

    // Tools management
    const toolsData = ref([])
    const loadingTools = ref(false)
    const skillsData = ref(null)
    const loadingSkills = ref(false)

    // Search settings（独立接口，api_key 不回显，只回 api_key_set 布尔）
    const searchConfig = ref({ provider: 'bing', result_size: 10, providers: {} })
    const searchSaving = ref(false)
    const searchMsg = ref('')
    const searchMsgErr = ref(false)

    // 当前选中引擎的配置项（只渲染这一个，避免 19 个引擎的表单全铺开）
    const currentSearchProvider = computed(() => {
      const name = searchConfig.value.provider
      const p = (searchConfig.value.providers || {})[name]
      if (!p) return null
      return { key: name, ...p }
    })

    async function loadSearchConfig() {
      try {
        const r = await fetch('/api/search/config')
        const j = await r.json()
        if (j && j.data) {
          searchConfig.value = {
            provider: j.data.provider || 'bing',
            result_size: j.data.result_size || 10,
            providers: j.data.providers || {}
          }
        }
      } catch (e) {
        // 拉取失败不阻塞整个抽屉，保留默认空结构
        console.warn('加载搜索配置失败', e)
      }
    }

    async function saveSearchConfig() {
      searchSaving.value = true
      searchMsg.value = ''
      try {
        const r = await fetch('/api/search/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ config: JSON.stringify(searchConfig.value) })
        })
        const j = await r.json()
        searchMsgErr.value = !(j && j.status === true)
        searchMsg.value = (j && j.msg) || (searchMsgErr.value ? '保存失败' : '已保存')
        if (!searchMsgErr.value) await loadSearchConfig()
      } catch (e) {
        searchMsgErr.value = true
        searchMsg.value = '保存失败: ' + (e && e.message ? e.message : e)
      } finally {
        searchSaving.value = false
      }
    }

    // MCP servers management
    const mcpServers = ref(null)
    const loadingMcp = ref(false)
    const addingMcp = ref(false)
    const newMcpName = ref('')
    const newMcpConfig = ref('')
    const newMcpOverwrite = ref(false)
    const mcpMsg = ref('')
    const mcpMsgErr = ref(false)
    const mcpMarket = ref(null)
    const loadingMarket = ref(false)

    // Group tools by category
    const groupedTools = computed(() => {
      const groups = {}
      for (const t of toolsData.value) {
        if (!groups[t.category]) groups[t.category] = []
        groups[t.category].push(t)
      }
      return groups
    })

    function categoryAllHidden(category) {
      const tools = groupedTools.value[category] || []
      return tools.length > 0 && tools.every(t => !t.show)
    }

    function toggleToolVisibility(tool) {
      tool._loading = true
      apiPost('/api/tools/show_status', { id: tool.id, show: !tool.show }, (result) => {
        tool._loading = false
        if (result.status) {
          tool.show = !tool.show
          if (window.layer) window.layer.msg('设置成功', { icon: 1 })
        } else {
          if (window.layer) window.layer.msg(result.msg || '设置失败', { icon: 2 })
        }
      })
    }

    function toggleCategoryVisibility(category, hidden) {
      const tools = groupedTools.value[category] || []
      tools.forEach(tool => {
        tool.show = !hidden
      })
      apiPost('/api/tools/show_status', { category, show: !hidden, all: true }, (result) => {
        if (!result.status && window.layer) {
          window.layer.msg(result.msg || '设置失败', { icon: 2 })
        }
      })
    }

    function toggleSkill(skill) {
      skill._loading = true
      apiPost('/api/skills/status', { skill_name: skill.name, enabled: !skill.enabled }, (result) => {
        skill._loading = false
        if (result.status) {
          skill.enabled = !skill.enabled
          if (window.layer) window.layer.msg('设置成功', { icon: 1 })
        } else {
          if (window.layer) window.layer.msg(result.msg || '设置失败', { icon: 2 })
        }
      })
    }

    // 编辑锁：抽屉打开期间，后台配置刷新（如对话完成触发 updateQuota）不再重建表单，
    // 避免覆盖用户正在编辑的内容；关闭抽屉后恢复自动同步
    const syncFormData = (newConfig) => {
      if (!newConfig) return
      formData.value = {
        api_base_url: newConfig.api_base_url || '',
        api_key: newConfig.api_key || '',
        models: (newConfig.models || []).filter(Boolean),
        enable_mcp: newConfig.enable_mcp !== false,
        mcp_config_path: newConfig.mcp_config_path || '',
        default_model: newConfig.default_model || '',
        workspace: newConfig.workspace || '',
        context_window_kb: newConfig.context_window_kb || 512,
        global_kb_agent_id: newConfig.global_kb_agent_id || 'ai-agent',
        mem0_api_url: newConfig.mem0_api_url || 'http://localhost:8000',
        use_global_rag: newConfig.rag_mode === 'global_only' || newConfig.use_global_rag === true || newConfig.use_global_rag === 'true',
        use_external_kb: newConfig.rag_mode === 'external_only' || newConfig.use_external_kb === true || newConfig.use_external_kb === 'true',
        rag_mode: newConfig.rag_mode || (newConfig.use_external_kb ? 'external_only' : newConfig.use_global_rag ? 'global_only' : 'disabled'),
        agent: {
          temperature: newConfig.agent?.temperature || 0.9,
          top_p: newConfig.agent?.top_p || 0.8,
          reasoning_effort: newConfig.agent?.reasoning_effort || 'max'
        }
      }
    }

    watch(() => props.config, (newConfig) => {
      if (props.visible) return
      syncFormData(newConfig)
    }, { immediate: true })

    // 切换下拉框时同步派生字段，保证后端 use_global_rag / use_external_kb 语义不变
    watch(() => formData.value.rag_mode, (mode) => {
      formData.value.use_global_rag = mode === 'global_only'
      formData.value.use_external_kb = mode === 'external_only'
    })

    // Watch visible prop
    watch(() => props.visible, (visible) => {
      if (visible) {
        // 打开时以最新配置初始化表单
        syncFormData(props.config)
        fetchModels()
        fetchTools()
        fetchSkills()
        fetchCrew()
        fetchMcpServers()
      }
    })

    const fetchModels = () => {
      if (!formData.value.api_base_url || !formData.value.api_key) {
        fetchError.value = '请先填写 API Base URL 和 API Key'
        return
      }

      loadingModels.value = true
      fetchError.value = ''

      apiGet('/api/models?base_url=' + encodeURIComponent(formData.value.api_base_url) + '&key=' + encodeURIComponent(formData.value.api_key), (result) => {
        loadingModels.value = false
        if (result.status) {
          availableModels.value = result.data.map(m => ({ id: m }))
          // 剪枝：清除已从服务商下线的陈旧勾选项（否则会被永久保留并随表单一起保存）
          const live = new Set(result.data)
          const before = formData.value.models
          formData.value.models = before.filter(id => live.has(id))
          // 默认模型失效时回退
          const dm = formData.value.default_model
          if (dm && !live.has(dm)) {
            formData.value.default_model = formData.value.models[0] || ''
          }
          fetchError.value = ''
        } else {
          fetchError.value = '未获取到任何模型'
          availableModels.value = []
        }
      })
    }

    const fetchTools = () => {
      loadingTools.value = true
      apiGet('/api/tools', (result) => {
        loadingTools.value = false
        if (result.status && Array.isArray(result.data)) {
          toolsData.value = result.data
        }
      })
    }

    const fetchSkills = () => {
      loadingSkills.value = true
      apiGet('/api/skills', (result) => {
        loadingSkills.value = false
        if (result.status) {
          skillsData.value = result.data
        }
      })
    }

    const fetchMcpServers = async () => {
      loadingMcp.value = true
      try {
        const r = await fetch('/api/mcp/servers')
        const res = await r.json()
        mcpServers.value = res
      } catch {
        mcpServers.value = { items: [] }
      } finally {
        loadingMcp.value = false
      }
    }

    const addMcpServer = async () => {
      addingMcp.value = true
      mcpMsg.value = ''
      mcpMsgErr.value = false
      try {
        const r = await fetch('/api/mcp/servers/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: newMcpName.value.trim(),
            config: newMcpConfig.value,
            overwrite: newMcpOverwrite.value
          })
        })
        const res = await r.json()
        mcpMsgErr.value = !res.status
        mcpMsg.value = res.msg || (res.status ? '添加成功' : '添加失败')
        if (res.status) {
          newMcpName.value = ''
          newMcpConfig.value = ''
          newMcpOverwrite.value = false
           fetchMcpServers()
           safeTimeout(() => {
             if (window.layer) window.layer.msg('已写入配置，点「热重载」让 MCP 客户端重连生效', { icon: 0 })
           }, 600)
        }
      } catch {
        mcpMsgErr.value = true
        mcpMsg.value = '请求失败'
      } finally {
        addingMcp.value = false
      }
    }

    const removeMcpServer = async (srv) => {
      if (!confirm(`确认移除 MCP server「${srv.name}」？`)) return
      try {
        const r = await fetch('/api/mcp/servers/remove', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: srv.name })
        })
        const res = await r.json()
        if (window.layer) window.layer.msg(res.msg || (res.status ? '已移除' : '移除失败'), { icon: res.status ? 1 : 2 })
        if (res.status) fetchMcpServers()
      } catch {}
    }

    const fetchMcpMarket = async () => {
      loadingMarket.value = true
      try {
        const r = await fetch('/api/mcp/market')
        const res = await r.json()
        if (res.status) {
          mcpMarket.value = res
        } else {
          mcpMsgErr.value = true
          mcpMsg.value = res.msg || '市场拉取失败'
        }
      } catch {
        mcpMsgErr.value = true
        mcpMsg.value = '市场拉取请求失败'
      } finally {
        loadingMarket.value = false
      }
    }

    const installingMcpName = ref('')
    const installMarketMcp = async (m) => {
      installingMcpName.value = m.name
      try {
        const r = await fetch('/api/mcp/servers/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mcp_id: m.name })
        })
        const res = await r.json()
        if (window.layer) window.layer.msg(res.msg || (res.status ? '安装成功' : '安装失败'), { icon: res.status ? 1 : 2 })
        if (res.status) {
          fetchMcpServers()
           // 提示用户点热重载让 MCP 客户端重连
           safeTimeout(() => {
             if (window.layer) window.layer.msg('已写入配置，请点「热重载」让 MCP 客户端重连生效', { icon: 0 })
           }, 600)
        }
      } catch {
        if (window.layer) window.layer.msg('安装请求失败', { icon: 2 })
      } finally {
        installingMcpName.value = ''
      }
    }

    // ==================== 热重载 ====================
    const reloading = ref(false)
    const doHotReload = async () => {
      reloading.value = true
      try {
        const r = await fetch('/api/hotreload', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target: 'all' })
        })
        const res = await r.json()
        if (window.layer) window.layer.msg(res.msg || (res.status ? '热重载完成' : '部分失败'), { icon: res.status ? 1 : res.status === false ? 2 : 0 })
        fetchTools(); fetchSkills(); fetchCrew()
      } catch (e) {
        if (window.layer) window.layer.msg('热重载请求失败', { icon: 2 })
      } finally {
           safeTimeout(() => { reloading.value = false }, 800)
      }
    }

    // ==================== 部门 + 子代理团队（Crew） ====================
    const crewAgents = ref([])
    const loadingCrew = ref(false)
    const departments = ref([])
    const deptForm = ref({ name: '', title: '', description: '' })

    const fetchCrew = async () => {
      loadingCrew.value = true
      try {
        const r = await fetch('/api/crew/agents')
        const res = await r.json()
        if (res.status) {
          crewAgents.value = res.data.agents || []
          departments.value = res.data.departments || []
        }
      } catch {} finally { loadingCrew.value = false }
    }

    const saveDept = async () => {
      const f = deptForm.value
      if (!f.name.trim() || !f.title.trim()) return
      try {
        const r = await fetch('/api/crew/dept_save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: f.name.trim(), title: f.title.trim(), description: f.description.trim() })
        })
        const res = await r.json()
        crewMsgErr.value = !res.status
        crewMsg.value = res.msg || (res.status ? '部门已保存' : '保存失败')
        if (res.status) { fetchCrew(); deptForm.value = { name: '', title: '', description: '' } }
      } catch {}
    }

    const deleteDept = (name) => {
      fetch('/api/crew/dept_delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      }).then(r=>r.json()).then(res => { if (res.status) fetchCrew() })
    }

    const assignDept = (memberName, department) => {
      const a = crewAgents.value.find(x => x.name === memberName)
      if (!a) return
      // 复用 save：带上现有字段仅更新部门
      fetch('/api/crew/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: a.name,
          description: a.description,
          backstory: a.backstory || '',
          tools: a.tools,
          model: '',
          department
        })
      }).then(()=>fetchCrew())
    }

    const savingCrew = ref(false)
    const crewMsg = ref('')
    const crewMsgErr = ref(false)
    const crewForm = ref({ name: '', description: '', backstory: '', tools: [], model: '' })

    const saveCrewAgent = async () => {
      const f = crewForm.value
      savingCrew.value = true
      crewMsg.value = ''
      try {
        const r = await fetch('/api/crew/save', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: f.name.trim(),
            description: f.description.trim(),
            backstory: f.backstory.trim(),
            tools: f.tools,
            model: f.model,
          })
        })
        const res = await r.json()
        crewMsgErr.value = !res.status
        crewMsg.value = res.msg || (res.status ? '保存成功' : '保存失败')
        if (res.status) {
          fetchCrew()
          crewForm.value = { name: '', description: '', backstory: '', tools: [], model: '' }
        }
      } catch (e) {
        crewMsgErr.value = true
        crewMsg.value = '请求失败: ' + (e.message || e)
      } finally {
        savingCrew.value = false
      }
    }

    const deleteCrewAgent = (name) => {
      fetch('/api/crew/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
      }).then(r => r.json()).then(res => {
        crewMsgErr.value = !res.status
        crewMsg.value = res.msg || ''
        if (res.status) fetchCrew()
      })
    }

    // ==================== 技能安装 / 卸载 ====================
    const installingSkill = ref(false)
    const overwriteSkill = ref(false)
    const skillInstallMsg = ref('')
    const skillInstallErr = ref(false)
    const skillUrl = ref('')
    const installingUrl = ref(false)

    const applyInstallResult = (result) => {
      skillInstallErr.value = !result.status
      skillInstallMsg.value = result.msg || (result.status ? '安装成功' : '安装失败')
      if (result.status) fetchSkills()
    }

    const canUninstall = (name) => {
      // 内置引导技能不允许卸载
      return name !== 'skill-install-guide'
    }

    const installFromUrl = async () => {
      const url = skillUrl.value.trim()
      if (!url || installingUrl.value) return
      installingUrl.value = true
      skillInstallMsg.value = ''
      try {
        const resp = await fetch('/api/skills/install_url', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url, overwrite: overwriteSkill.value })
        })
        applyInstallResult(await resp.json())
        if (skillInstallErr.value === false) skillUrl.value = ''
      } catch (err) {
        skillInstallErr.value = true
        skillInstallMsg.value = 'URL 安装失败: ' + (err && err.message ? err.message : String(err))
      } finally {
        installingUrl.value = false
      }
    }

    const handleSkillUpload = async (e) => {
      const file = e.target.files && e.target.files[0]
      e.target.value = ''
      if (!file) return
      if (!file.name.toLowerCase().endsWith('.zip')) {
        skillInstallErr.value = true
        skillInstallMsg.value = '请选择 .zip 压缩包'
        return
      }
      if (file.size > 30 * 1024 * 1024) {
        skillInstallErr.value = true
        skillInstallMsg.value = '压缩包超过 30MB 上限'
        return
      }

      installingSkill.value = true
      skillInstallMsg.value = ''
      try {
        const buf = await file.arrayBuffer()
        let binary = ''
        const bytes = new Uint8Array(buf)
        const chunk = 0x8000
        for (let i = 0; i < bytes.length; i += chunk) {
          binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk))
        }
        const zipB64 = btoa(binary)
        const resp = await fetch('/api/skills/install', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ zip_b64: zipB64, overwrite: overwriteSkill.value })
        })
        applyInstallResult(await resp.json())
      } catch (err) {
        skillInstallErr.value = true
        skillInstallMsg.value = '上传失败: ' + (err && err.message ? err.message : String(err))
      } finally {
        installingSkill.value = false
      }
    }

    const uninstallSkill = (skill) => {
      const doDelete = () => {
        fetch('/api/skills/uninstall', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ skill_name: skill.name })
        }).then(r => r.json()).then((result) => {
          if (window.layer) window.layer.msg(result.msg || (result.status ? '已卸载' : '卸载失败'), { icon: result.status ? 1 : 2 })
          if (result.status) fetchSkills()
        }).catch(() => {
          if (window.layer) window.layer.msg('卸载请求失败', { icon: 2 })
        })
      }
      if (window.confirm(`确定卸载技能「${skill.name}」吗？目录将被删除。`)) {
        doDelete()
      }
    }

    const handleOverlayClick = () => {
      emit('close')
    }

    const handleClose = () => {
      emit('close')
    }

    const handleSave = () => {
      apiPost('/api/config', { config: JSON.stringify(formData.value) }, (result) => {
        if (result.status) {
          emit('save', formData.value)
          emit('close')
          alert('配置保存成功')
        } else {
          alert('配置保存失败: ' + (result.msg || '未知错误'))
        }
      })
    }

    const addHeader = () => {
      const key = `header_${Date.now()}`
      formData.value.default_headers[key] = ''
    }

    const removeHeader = (index) => {
      const keys = Object.keys(formData.value.default_headers)
      if (keys[index]) {
        delete formData.value.default_headers[keys[index]]
      }
    }

    const updateHeaderKey = (index, newKey) => {
      const oldKey = Object.keys(formData.value.default_headers)[index]
      if (!oldKey || oldKey === newKey) return
      const copy = { ...formData.value.default_headers }
      delete copy[oldKey]
      copy[newKey] = formData.value.default_headers[oldKey]
      formData.value.default_headers = copy
    }

    const updateHeaderVal = (index, newVal) => {
      const key = Object.keys(formData.value.default_headers)[index]
      if (!key) return
      formData.value.default_headers[key] = newVal
    }

    // M-6: 组件卸载时清理所有待执行的定时器，防止回调操作已销毁的响应式状态
    onUnmounted(() => {
      for (const id of pendingTimers) {
        clearTimeout(id)
      }
      pendingTimers.clear()
    })

    onMounted(() => {
      loadSearchConfig()
    })

    return {
      formData,
      searchConfig,
      currentSearchProvider,
      searchSaving,
      searchMsg,
      searchMsgErr,
      saveSearchConfig,
      availableModels,
      loadingModels,
      fetchError,
      fetchModels,
      fetchTools,
      fetchSkills,
      handleOverlayClick,
      handleClose,
      handleSave,
      addHeader,
      removeHeader,
      updateHeaderKey,
      updateHeaderVal,
      toolsData,
      loadingTools,
      skillsData,
      loadingSkills,
      groupedTools,
      categoryAllHidden,
      toggleToolVisibility,
      toggleCategoryVisibility,
      toggleSkill,
      installingSkill,
      overwriteSkill,
      skillInstallMsg,
      skillInstallErr,
      canUninstall,
      handleSkillUpload,
      uninstallSkill,
      skillUrl,
      installingUrl,
      installFromUrl,
      crewAgents,
      departments,
      deptForm,
      saveDept,
      deleteDept,
      assignDept,
      loadingCrew,
      savingCrew,
      crewMsg,
      crewMsgErr,
      crewForm,
      saveCrewAgent,
      deleteCrewAgent,
    reloading, doHotReload,
    // ── MCP Server 管理 ──
    mcpServers, loadingMcp, addingMcp,
    newMcpName, newMcpConfig, newMcpOverwrite,
    mcpMsg, mcpMsgErr,
    mcpMarket, loadingMarket, installingMcpName,
    fetchMcpServers, addMcpServer, removeMcpServer,
    fetchMcpMarket, installMarketMcp,
  }
}
}
</script>

<style scoped>
.models-checkbox-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 12px;
}

.model-option {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}

.model-name {
  font-size: 13px;
  color: #374151;
}

.loading, .error-message, .empty-models {
  padding: 12px;
  border-radius: 8px;
  font-size: 13px;
}

.loading {
  background: #f3f4f6;
  color: #6b7280;
}

.error-message {
  background: #fef2f2;
  color: #dc2626;
}

.empty-models {
  background: #f9fafb;
  color: #9ca3af;
}

.header-row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.header-key, .header-value {
  flex: 1;
}

.btn-remove {
  width: 32px;
  height: 32px;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #9ca3af;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.btn-remove:hover {
  background: #fee2e2;
  border-color: #ef4444;
  color: #ef4444;
}

.btn-primary {
  padding: 8px 16px;
  border-radius: 8px;
  border: none;
  background: #20a53a;
  color: #fff;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-primary:hover {
  background: #1a8a2e;
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-secondary {
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #6b7280;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
}

.btn-secondary:hover {
  background: #f9fafb;
  border-color: #d1d5db;
}

.btn-save {
  width: 100%;
  padding: 12px;
  font-size: 14px;
  margin-top: 16px;
}

/* Tool & Skill management */
.tool-group,
.skill-group {
  margin-bottom: 16px;
}

.tool-category-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px solid #f0f1f3;
  margin-bottom: 8px;
}

.tool-category-name {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}

.tool-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 0;
}

.tool-item-name {
  font-size: 13px;
  color: #374151;
  flex: 1;
}

.tool-item-desc {
  font-size: 12px;
  color: #9ca3af;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.skill-sub-list {
  padding-left: 16px;
  border-left: 2px solid #f0f1f3;
}

/* 子代理 Crew */
.crew-list { margin-bottom: 10px; }
.crew-item {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 0; border-bottom: 1px dashed #f0f1f3;
}
.crew-name { font-size: 13px; font-weight: 600; color: #1f2937; }
.crew-badge {
  font-size: 10px; padding: 1px 6px; border-radius: 4px;
}
.crew-badge.builtin { background: #f3f4f6; color: #9ca3af; }
.crew-badge.custom { background: #ecfdf5; color: #059669; }
.crew-desc {
  flex: 1; font-size: 11px; color: #9ca3af;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.crew-del {
  border: 1px solid #fecaca; background: #fff; color: #ef4444;
  font-size: 11px; padding: 2px 8px; border-radius: 4px; cursor: pointer;
}
.crew-del:hover { background: #fef2f2; }
.crew-add-btn {
  font-size: 12px; color: #20a53a; cursor: pointer; user-select: none;
}
.crew-form { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
.crew-tools {
  display: flex; flex-wrap: wrap; gap: 4px 10px;
  max-height: 140px; overflow-y: auto;
  border: 1px solid #e5e7eb; border-radius: 6px; padding: 6px;
}
.crew-tool-chk {
  font-size: 11px; color: #374151;
  display: inline-flex; align-items: center; gap: 3px; cursor: pointer;
}

/* 技能安装 */
.skill-install-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

/* ===== MCP Server 管理 ===== */
.mcp-manage {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed #e5e7eb;
}

.mcp-manage-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
}

.mcp-refresh,
.mcp-market-btn {
  font-size: 12px;
  padding: 4px 10px;
}

.mcp-list {
  padding-left: 12px;
  border-left: 2px solid #f0f1f3;
  margin-bottom: 10px;
}

.mcp-env-badge {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #f3f4f6;
  color: #9ca3af;
  flex-shrink: 0;
}

.mcp-add-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}

.mcp-name-input { font-size: 12px; }

.mcp-config-input {
  font-size: 12px;
  font-family: 'SFMono-Regular', Menlo, Consolas, monospace;
  resize: vertical;
}

.mcp-add-btn { align-self: flex-start; font-size: 12px; padding: 4px 14px; }

.mcp-market {
  margin-top: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px;
  background: #fafafa;
}

.mcp-market-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 8px;
}

.mcp-market-list {
  max-height: 320px;
  overflow-y: auto;
}

.mcp-market-item {
  padding: 8px;
  border-bottom: 1px solid #eee;
}

.mcp-market-meta {
  display: flex;
  align-items: center;
  gap: 6px;
}

.mcp-market-item .tool-item-name {
  flex: none;
  font-weight: 600;
}

.mcp-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin: 4px 0;
}

.mcp-tag {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #ecfdf5;
  color: #059669;
}

.mcp-market-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 4px;
}

.mcp-market-link,
.mcp-copy {
  display: none;
}

.mcp-install-btn {
  font-size: 11px;
  padding: 2px 10px;
}

.skill-url-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.skill-url-input {
  flex: 1;
  font-size: 12px;
}

.overwrite-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: #6b7280;
  cursor: pointer;
}

.skill-install-msg {
  font-size: 12px;
  color: #16a34a;
  margin-bottom: 6px;
}

.skill-install-msg.err {
  color: #dc2626;
}

/* 搜索设置：Key 输入行与"已配置"标记 */
.key-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.key-row .form-input {
  flex: 1;
}

.key-badge {
  flex-shrink: 0;
  font-size: 11px;
  color: #16a34a;
  border: 1px solid #16a34a;
  border-radius: 999px;
  padding: 2px 8px;
}

.form-hint.err {
  color: #dc2626;
}

.skill-install-hint {
  font-size: 11px;
  color: #9ca3af;
  line-height: 1.5;
  margin-bottom: 10px;
}

.skill-uninstall-btn {
  padding: 3px 10px;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
  background: #fff;
  color: #9ca3af;
  font-size: 12px;
  cursor: pointer;
}

.skill-uninstall-btn:hover {
  background: #fee2e2;
  border-color: #ef4444;
  color: #ef4444;
}

.switch-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  position: relative;
}

.switch-input {
  position: absolute;
  opacity: 0;
  width: 0;
  height: 0;
}

.switch-slider {
  width: 36px;
  height: 20px;
  background: #e5e7eb;
  border-radius: 10px;
  position: relative;
  transition: all 0.3s;
  flex-shrink: 0;
}

.switch-slider::before {
  content: '';
  position: absolute;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  top: 2px;
  left: 2px;
  transition: all 0.3s;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

.switch-input:checked + .switch-slider {
  background: #20a53a;
}

.switch-input:checked + .switch-slider::before {
  transform: translateX(16px);
}

.switch-text {
  font-size: 12px;
  color: #6b7280;
}
</style>
