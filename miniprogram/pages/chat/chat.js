// pages/chat/chat.js
const { api } = require('../../utils/request.js')
const { subscribe } = require('../../utils/sse.js')
const { parseMarkdown, extractImages } = require('../../utils/markdown.js')
const auth = require('../../utils/auth.js')
const store = require('../../utils/store.js')

// SSE 事件类型 → 页面处理
const MAX_RESUME = 5

/**
 * 在微信里搜索机器人用的关键词。
 * 这里不是"链接"：clawbot 走 ilink 通道，是微信内的机器人，没有小程序
 * appid 也没有可跳转 URL，故只能给出可搜索的名称（见 copyBotName 注释）。
 */
const BOT_SEARCH_KEYWORD = 'clawbot'

/** 快捷指令：把高频用法前置，减少手机端输入成本 */
const QUICK_PROMPTS = [
  '帮我总结这段内容',
  '写一份周报',
  '解释一下这个概念',
  '帮我优化这段代码',
  '翻译成英文',
  '列出行动清单'
]

/**
 * 还原后端 sse_pack 对字符串的转义（把字面 "\n" 还原为真实换行）。
 * 与浏览器端 unescapeSSEString 对齐：不还原的话小程序的 \n\n 会原样显示。
 */
function unescapeSSEString(str) {
  if (typeof str !== 'string') return str
  return str
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\r')
    .replace(/\\t/g, '\t')
}

/** JSON 解析：失败返回 null（不抛异常打断渲染） */
function safeJson(text) {
  try { return JSON.parse(text) } catch (e) { return null }
}

/** 消息文本 → 渲染块（文本/代码/图片），供 wxml 按块渲染 */
function buildBlocks(text) {
  return parseMarkdown(text || '')
}

Page({
  data: {
    messages: [],      // {role, content, thinking, done, blocks, images}
    input: '',
    sessionId: '',
    sending: false,
    streaming: false,
    statusText: '',
    scrollToBottom: '',
    fontPx: 30,        // 正文字号，由「我的」页设置（utils/store.js）

    // 快捷指令
    QUICK_PROMPTS,
    quickVisible: false,

    // 语音输入
    voiceMode: false,
    recording: false,
    recordManager: null,

    // 长按菜单
    menuVisible: false,
    longPressIndex: -1,
    menuIndex: -1,
    menuMessage: { images: [] },

    // ── 微信机器人（从「我的」迁入本页）─────────────────────
    // 本页 tab 已改名为「连接机器人」，对话 UI 隐藏但代码保留（见 chat.wxml
    // 的 chatHidden 开关），机器人状态机是本页的主职责。
    botStatus: 'unknown',     // unknown | new | waiting | scaned | active | expired | error
    botQr: '',                // 原始 data URI（用于判断二维码是否变化）
    botQrSrc: '',             // 实际显示用的 src（本地文件路径，见 resolveQrSrc）
    botError: '',
    botLoading: false,
    // 忙碌态：机器人在微信里收到消息、正在生成回复时为 true。
    // 顶部据此显示动态提示，否则用户发了消息看不到任何反馈。
    botBusy: false,
    botBusyPeer: '',          // 已脱敏的对方标识
    botBusyPreview: '',       // 消息内容预览
    botBusyElapsed: 0,        // 已耗时（秒）
    // 在微信里搜索机器人用的名称（与 copyBotName 用的常量同源）
    botSearchName: BOT_SEARCH_KEYWORD,

    // 对话区显隐开关：false = 隐藏对话 UI（代码不删，随时可恢复）
    chatHidden: true
  },

  onLoad() {
    // 用本地持久化 session_id，保证刷新后仍能续聊
    let sid = wx.getStorageSync('session_id')
    if (!sid) {
      sid = 'mp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
      wx.setStorageSync('session_id', sid)
    }
    this.setData({ sessionId: sid })
    this.applyFontSize()
    this.initRecorder()
    this.checkAuthAndLoad()
  },

  onShow() {
    // 字号可能刚在「我的」页被改过，每次显示时同步一次
    this.applyFontSize()
    this.loadBotStatus()
  },

  onHide() {
    this.stopBotPolling()
  },

  // ─────────────────────── 微信机器人 ───────────────────────

  /** 查询自己机器人的连接状态，并按状态决定是否轮询。 */
  async loadBotStatus() {
    const r = await auth.ensureLogin()
    if (!r.ok) return
    const res = await api.botStatus()
    if (!res.ok || !res.data) return
    this.applyBotStatus(res.data)
    // 等待扫码：轮询等确认结果；已连接：也要轮询，否则微信里发消息
    // 时"正在处理"状态永远不会刷新到界面（这是顶部无状态显示的直接原因）
    if (this.data.botStatus === 'waiting' || this.data.botStatus === 'scaned' ||
        this.data.botStatus === 'active') {
      this.startBotPolling()
    }
  },

  /** 把后端状态写进 data（轮询与首次加载共用，避免两处字段写漏） */
  applyBotStatus(d) {
    const patch = {
      botStatus: d.status || 'new',
      botQr: d.qr_payload || '',
      botError: d.error || '',
      botBusy: !!d.busy,
      botBusyPeer: d.busy_peer || '',
      botBusyPreview: d.busy_preview || '',
      botBusyElapsed: Number(d.busy_elapsed) || 0
    }
    // 二维码只在变化时重解析：resolveQrSrc 要落盘写文件，
    // 每 3 秒刷一次会白白产生大量 IO
    if ((d.qr_payload || '') !== this.data.botQr) {
      patch.botQrSrc = this.resolveQrSrc(d.qr_payload || '')
    }
    this.setData(patch)
  },

  /** 取二维码：请求携带用户 token，服务端据此创建"属于你的"机器人槽位。 */
  async connectBot() {
    if (this.data.botLoading) return
    this.setData({ botLoading: true })
    const r = await auth.ensureLogin()
    if (!r.ok) {
      this.setData({ botLoading: false })
      wx.showToast({ title: r.reason || '请先登录', icon: 'none' })
      return
    }
    const res = await api.botQrcode()
    this.setData({ botLoading: false })
    if (!res.ok || !res.data || !res.data.qr_payload) {
      wx.showToast({ title: res.msg || '获取失败，请重试', icon: 'none' })
      return
    }
    this.setData({
      botStatus: 'waiting',
      botQr: res.data.qr_payload,
      botQrSrc: this.resolveQrSrc(res.data.qr_payload),
      botError: ''
    })
    this.startBotPolling()
  },

  /**
   * 轮询机器人状态：3 秒一次。
   *
   * 覆盖三种情形：
   *   1. waiting/scaned —— 等扫码确认，登录成功即停
   *   2. active         —— 持续轮询，实时反映"正在处理微信消息"（忙碌态）
   *   3. 其余           —— 不轮询（未连接/已过期，无需刷新）
   * 注意 active 不能停：一停忙碌态就再也不会更新到界面。
   */
  startBotPolling() {
    this.stopBotPolling()
    this._botTimer = setInterval(async () => {
      const res = await api.botStatus()
      if (!res.ok || !res.data) return
      const d = res.data
      const wasActive = this.data.botStatus === 'active'
      this.applyBotStatus(d)
      // 从等待扫码变为已连接：提示一次，但继续轮询（还要看忙碌态）
      if (d.status === 'active' && !wasActive) {
        wx.showToast({ title: '连接成功', icon: 'success' })
      } else if (d.status === 'expired' || d.status === 'error' ||
                 d.status === 'new' || d.status === 'unknown') {
        this.stopBotPolling()
      }
    }, 3000)
  },

  stopBotPolling() {
    if (this._botTimer) {
      clearInterval(this._botTimer)
      this._botTimer = null
    }
  },

  /**
   * 二维码 data URI → 显示用的 src（本地文件路径）。
   *
   * 为什么必须落盘：image 的原生长按识别（show-menu-by-longpress）对
   * data URI 支持不稳定——社区多例反馈直接给 base64 时长按不弹出
   * "识别图中二维码"。写成真实文件后，微信客户端才能按图片处理，
   * 长按即可得原生菜单：识别图中二维码 / 保存图片 / 转发朋友 / 收藏。
   *
   * 空值直接返回空（页面 wx:if 会隐藏），写失败则退回原 data URI，
   * 保证至少能看见图（只是长按识别可能不生效）。
   */
  resolveQrSrc(dataUri) {
    if (!dataUri) return ''
    try {
      const fs = wx.getFileSystemManager()
      const filePath = `${wx.env.USER_DATA_PATH}/bot_qr.png`
      fs.writeFileSync(filePath, dataUri.split(',')[1], 'base64')
      return filePath
    } catch (e) {
      return dataUri
    }
  },

  /** 断开机器人：清除登录态，之后可重新扫码（换号场景）。 */
  disconnectBot() {
    wx.showModal({
      title: '断开机器人',
      content: '断开后微信里的对话将停止，重新扫码即可恢复。小程序数据不受影响。',
      confirmText: '断开',
      confirmColor: '#f87171',
      success: async (r) => {
        if (!r.confirm) return
        await auth.ensureLogin()
        const res = await api.botDisconnect()
        if (!res.ok) {
          wx.showToast({ title: res.msg || '操作失败', icon: 'none' })
          return
        }
        this.setData({ botStatus: 'new', botQr: '', botError: '' })
        wx.showToast({ title: '已断开', icon: 'none' })
      }
    })
  },

  /** 临时开关：把隐藏的对话区调出来（chatHidden=false 即可，代码一直在）。 */
  toggleChat() {
    this.setData({ chatHidden: !this.data.chatHidden })
  },

  /**
   * 复制机器人名称，供用户去微信里搜索。
   *
   * 为什么是"复制"而不是"跳转链接"：clawbot 是微信内的机器人（ilink 通道），
   * 没有小程序 appid、也没有公众号文章 URL，微信未提供任何可用的跳转 scheme；
   * 而小程序也无法调起微信客户端的原生搜索页。实测可行的只剩复制关键词——
   * 用户复制后进微信搜索框粘贴，是当前能力边界内最短的路径。
   */
  copyBotName() {
    wx.setClipboardData({
      data: BOT_SEARCH_KEYWORD,
      success: () => wx.showToast({ title: '已复制，去微信粘贴搜索', icon: 'none' }),
      fail: () => wx.showToast({ title: '复制失败，请长按手动选取', icon: 'none' }),
    })
  },

  /** 读取本地字号设置并应用（与「我的」页共用 utils/store.js） */
  applyFontSize() {
    const s = store.getSettings()
    const px = store.FONT_SIZES[s.fontSize] || store.FONT_SIZES.mid
    if (px !== this.data.fontPx) this.setData({ fontPx: px })
  },

  onUnload() {
    // 页面卸载：断 SSE + 停轮询（与 onHide 不同，这里是真正的离开）
    this.abortStream()
    this.stopBotPolling()
  },

  /**
    * 分享：优先分享当前这条回答。
    * 通过点击气泡右侧的"分享"进入；无选中时分享小程序本身。
    */
  onShareAppMessage() {
    const idx = this.data.menuIndex
    const msg = idx >= 0 ? this.data.messages[idx] : null
    if (msg && msg.role === 'ai' && msg.content) {
      const summary = msg.content.replace(/\s+/g, ' ').slice(0, 60)
      return {
        title: summary || '来自多智能体协作助手',
        path: '/pages/index/index'
      }
    }
    return { title: '多智能体协作助手', path: '/pages/index/index' }
  },

  /** 允许"分享到朋友圈"仅安卓有效，这里保留转发好友能力 */
  onShareTimeline() {
    return { title: '多智能体协作助手' }
  },

  // ─────────────────────────── 鉴权与历史 ───────────────────────────

  async checkAuthAndLoad() {
    const res = await api.authStatus()
    if (res.ok && res.data && res.data.enabled) {
      const token = wx.getStorageSync('token') || ''
      if (!token) {
        // 优先走微信一键登录，失败再退回密码登录页
        const ok = await this.wxLogin()
        if (!ok) return
      }
    }
    this.loadHistory()
  },

  /**
   * 微信登录：核心逻辑由 utils/auth.js 共享（首页也要用，避免两份实现走样）。
   * 本页只负责失败时的页面引导：带上原因跳密码登录页。
   * @returns {boolean} 是否登录成功
   */
  async wxLogin() {
    const r = await auth.ensureLogin()
    if (r.ok) {
      // 关键：用服务端绑定的会话 ID，实现"换设备也能续聊"
      if (r.sessionId) this.setData({ sessionId: r.sessionId })
      return true
    }
    // 带上失败原因跳登录页：否则用户只看到登录框，不知道微信登录为何失败
    const reason = encodeURIComponent(r.reason || '微信登录未通过')
    wx.redirectTo({ url: `/pages/login/login?reason=${reason}` })
    return false
  },

  async loadHistory() {
    const res = await api.messages(this.data.sessionId)
    if (res.ok && res.data) {
      // 历史事件结构依后端而定，尽量兼容数组/对象两种形态
      const list = Array.isArray(res.data) ? res.data : (res.data.messages || [])
      const messages = []
      for (const m of list) {
        if (!m) continue
        // tool 消息是工具执行结果，不是对话内容，直接丢弃。
        // 若不过滤，工具返回的 JSON/HTML 会当成 AI 回答渲染出来。
        if (m.role === 'tool') continue
        const kind = m.role === 'user' ? 'user' : 'ai'
        let content = String(m.content || '')
        // content 可能是 [{type:'text',text:'..'}] 结构（服务端多模态格式）
        if (Array.isArray(m.content)) {
          content = m.content
            .filter((p) => p && p.type === 'text')
            .map((p) => p.text || '')
            .join('')
        }
        if (!content.trim() && kind === 'ai') continue

        // 合并连续的同角色消息：服务端按 turn 存储，可能出现相邻的 ai 消息，
        // 不合并会渲染成一串气泡
        const prev = messages[messages.length - 1]
        if (prev && prev.role === kind) {
          prev.content += '\n' + content
          continue
        }
        messages.push({
          role: kind,
          content,
          thinking: '',
          done: true,
          blocks: buildBlocks(content),
          images: extractImages(content)
        })
      }
      // 重算合并后消息的渲染块
      messages.forEach((m) => {
        m.blocks = buildBlocks(m.content)
        m.images = extractImages(m.content)
      })
      if (messages.length) {
        this.setData({ messages }, () => this.scrollBottom())
      }
      return
    }

    // 登录态过期（token 超过 24h）：静默重登，用户无感知。
    // 若不处理，用户会看到空白历史且不知原因。
    if (res.unauthorized) {
      const ok = await this.wxLogin()
      if (ok) this.loadHistory()
      return
    }
    if (res.msg) this.setData({ statusText: res.msg })
  },

  // ─────────────────────────── 输入与发送 ───────────────────────────

  onInput(e) {
    this.setData({ input: e.detail.value })
  },

  async send() {
    const text = String(this.data.input || '').trim()
    if (!text || this.data.sending) return

    const messages = this.data.messages.concat([
      { role: 'user', content: text, thinking: '', done: true, blocks: buildBlocks(text), images: [] },
      { role: 'ai', content: '', thinking: '', done: false, blocks: [], images: [] }
    ])
    this.setData({ input: '', messages, sending: true, streaming: true, quickVisible: false }, () => this.scrollBottom())

    const res = await api.start({
      message: text,
      session_id: this.data.sessionId,
      // 单 Agent 模式：Agent 直接干活、使用全套工具，不经「经理」中转一层。
      // 服务端会为小程序渠道补默认人设与输出约束（见 _MINIPROGRAM_SINGLE_PERSONA）。
      mode: 'single'
    })

    if (!res.ok) {
      if (res.unauthorized) {
        // token 过期：清掉后重新走微信登录
        const app2 = getApp()
        app2.clearToken()
        const ok = await this.wxLogin()
        if (ok) { this.send(); return }
        return
      }
      this.finishStream()
      this.setData({ statusText: res.msg || '发送失败' })
      wx.showToast({ title: res.msg || '发送失败', icon: 'none' })
      return
    }

    this.startStream()
  },

  startStream() {
    let resume = 0

    const connect = () => {
      this.stream = subscribe({
        sessionId: this.data.sessionId,
        lastId: this.lastId || 0,
        onEvent: (evt) => this.handleEvent(evt),
        onError: (msg) => {
          // 断线自动续传，最多 MAX_RESUME 次
          if (resume < MAX_RESUME && this.data.streaming) {
            resume += 1
            setTimeout(connect, 800)
          } else {
            this.setData({ statusText: msg })
            this.finishStream()
          }
        },
        onEnd: () => {
          this.finishStream()
        }
      })
    }

    connect()
  },

  handleEvent(evt) {
    const { event, data, rawData } = evt
    if (evt.id) this.lastId = evt.id

    const messages = this.data.messages.slice()
    const ai = messages[messages.length - 1]
    if (!ai || ai.role !== 'ai') return

    /** 同步刷新气泡（重新切块渲染） */
    const flush = () => {
      ai.blocks = buildBlocks(ai.content)
      ai.images = extractImages(ai.content)
      this.setData({ messages }, () => this.scrollBottom())
    }

    switch (event) {
      case 'message': {
        // message 的 data 是**纯文本分片**（服务端未加 JSON 引号）。
        // 必须用 rawData 而非 data：JSON.parse("390") 会得到 number，
        // 走 data.content||data.text 取不到文本导致整片（数字/true/null）丢失。
        // 同时还原服务端为防断行做的 \n 转义，否则气泡里会显示字面 \n\n。
        const src = typeof rawData === 'string' ? rawData : data
        const text = unescapeSSEString(
          typeof src === 'string' ? src : (src && (src.content || src.text)) || ''
        )
        if (text) {
          ai.content += text
          flush()
        }
        break
      }
      case 'message_think': {
        // 思维链只用来标记"正在思考"，绝不保存原文：模型思考会复述系统提示词
        // 并暴露内部指令（实测出现「渠道约束：小程序」等条款原文）。
        // 因此这里只置位标志，不累积文本；渲染层也只显示"思考中…"。
        ai.thinking = '1'
        break
      }
      case 'tool_call': {
        // 服务端字段是 tool（浏览器端也读 tool）；兼容 function.name 旧形态
        const tc = typeof data === 'string' ? safeJson(data) : data
        const name = tc && (tc.tool || (tc.function && tc.function.name))
        if (name) {
          ai.content += `\n\n> 调用工具：${name}\n`
          flush()
        }
        break
      }
      case 'tool_result': {
        ai.content += `\n`
        flush()
        break
      }
      case 'error': {
        const src = typeof rawData === 'string' ? rawData : data
        const parsed = typeof src === 'string' ? safeJson(src) : src
        const msg = (parsed && parsed.msg) || (typeof src === 'string' ? unescapeSSEString(src) : '') || '出错了'
        ai.content += `\n\n⚠️ ${msg}`
        ai.done = true
        flush()
        break
      }
      case 'message_end': {
        ai.done = true
        flush()
        break
      }
      default:
        break
    }
  },

  finishStream() {
    const messages = this.data.messages.slice()
    const ai = messages[messages.length - 1]
    if (ai && ai.role === 'ai') ai.done = true
    this.setData({ messages, sending: false, streaming: false })
    this.abortStream()
  },

  abortStream() {
    if (this.stream && this.stream.abort) {
      try { this.stream.abort() } catch (e) { /* ignore */ }
    }
    this.stream = null
  },

  stop() {
    const sid = this.data.sessionId
    api.stop(sid)
    this.finishStream()
    this.setData({ statusText: '已停止' })
  },

  scrollBottom() {
    this.setData({ scrollToBottom: 'msg-' + (this.data.messages.length - 1) })
  },


  // ─────────────────────────── 快捷指令 ───────────────────────────

  toggleQuick() {
    this.setData({ quickVisible: !this.data.quickVisible })
  },

  useQuickPrompt(e) {
    const text = e.currentTarget.dataset.text || ''
    // 填入输入框而非直接发送：给用户补充细节的机会
    this.setData({ input: text, quickVisible: false })
  },

  // ─────────────────────────── 语音输入 ───────────────────────────

  initRecorder() {
    // 同声传译插件（微信官方）：把语音转文字，替代手打
    try {
      // eslint-disable-next-line no-undef
      const plugin = requirePlugin('WechatSI')
      const manager = plugin.getRecordRecognitionManager()
      if (!manager) return

      manager.onStop = (res) => {
        this.setData({ recording: false })
        const text = (res && res.result) || ''
        if (!text) {
          wx.showToast({ title: '没听清，再说一次', icon: 'none' })
          return
        }
        // 追加到已有内容后面，避免覆盖用户已输入的文字
        const cur = String(this.data.input || '')
        this.setData({ input: cur ? `${cur}${text}` : text })
      }
      manager.onError = (err) => {
        this.setData({ recording: false })
        const msg = (err && err.retcode === -30011) ? '录音时间太短' : '语音识别失败'
        wx.showToast({ title: msg, icon: 'none' })
      }
      this.recorder = manager
    } catch (e) {
      // 插件未配置时静默降级：不显示语音按钮
      this.recorder = null
    }
  },

  toggleVoiceMode() {
    if (!this.data.voiceMode && !this.recorder) {
      wx.showToast({ title: '语音输入需先配置同声传译插件', icon: 'none' })
      return
    }
    this.setData({ voiceMode: !this.data.voiceMode })
  },

  startRecord() {
    if (!this.recorder) return
    this.setData({ recording: true })
    try {
      this.recorder.start({ duration: 60000, lang: 'zh_CN' })
    } catch (e) {
      this.setData({ recording: false })
      wx.showToast({ title: '录音启动失败', icon: 'none' })
    }
  },

  stopRecord() {
    if (!this.recorder || !this.data.recording) return
    try { this.recorder.stop() } catch (e) { /* ignore */ }
  },

  cancelRecord() {
    if (!this.recorder) return
    this.setData({ recording: false })
    try { this.recorder.stop() } catch (e) { /* ignore */ }
  },

  // ─────────────────────────── 长按消息菜单 ───────────────────────────

  onBubbleTouchStart(e) {
    const index = Number(e.currentTarget.dataset.index)
    this._touchStart = Date.now()
    this._touchIndex = index
    this._longPressTimer = setTimeout(() => {
      this._longPressTimer = null
      wx.vibrateShort({ type: 'light' })
      const msg = this.data.messages[index] || {}
      this.setData({
        menuVisible: true,
        longPressIndex: index,
        menuIndex: index,
        menuMessage: { images: msg.images || [] }
      })
    }, 550)
  },

  onBubbleTouchMove() {
    // 滑动即视为非长按（用户可能在滚动列表）
    this.clearLongPressTimer()
  },

  onBubbleTouchEnd() {
    this.clearLongPressTimer()
    if (this.data.longPressIndex >= 0) {
      this.setData({ longPressIndex: -1 })
    }
  },

  clearLongPressTimer() {
    if (this._longPressTimer) {
      clearTimeout(this._longPressTimer)
      this._longPressTimer = null
    }
  },

  hideMenu() {
    this.setData({ menuVisible: false, longPressIndex: -1 })
  },

  onMenuAction(e) {
    const act = e.currentTarget.dataset.act
    const idx = this.data.menuIndex
    const msg = this.data.messages[idx]
    if (!msg && act !== 'share') {
      this.hideMenu()
      return
    }

    switch (act) {
      case 'copy':
        wx.setClipboardData({
          data: msg.content || '',
          success: () => wx.showToast({ title: '已复制', icon: 'none' })
        })
        this.hideMenu()
        break
      case 'copyCode': {
        // 只复制最后一段代码：多数场景用户想要的是刚生成的那段
        const codes = (msg.blocks || []).filter((b) => b.type === 'code')
        if (!codes.length) {
          wx.showToast({ title: '这条没有代码', icon: 'none' })
          this.hideMenu()
          return
        }
        wx.setClipboardData({
          data: codes[codes.length - 1].code,
          success: () => wx.showToast({ title: '代码已复制', icon: 'none' })
        })
        this.hideMenu()
        break
      }
      case 'share':
        // 触发系统转发：onShareAppMessage 会读取 menuIndex
        wx.showShareMenu({ withShareTicket: false })
        this.hideMenu()
        wx.showToast({ title: '点右上角「···」转发', icon: 'none' })
        break
      case 'regen':
        this.hideMenu()
        this.regenerate(idx)
        break
      case 'saveImage':
        this.hideMenu()
        this.saveImages(msg.images || [])
        break
      case 'delete':
        this.hideMenu()
        this.deleteMessage(idx)
        break
      default:
        this.hideMenu()
    }
  },

  /**
   * 重新生成：删掉这条 AI 回答及其后的消息，用上一条用户消息重发。
   *
   * 纯本地操作 + 复用对话接口：服务端历史里旧回答仍保留，但会话上下文
   * 由服务端记忆接管，重新生成的结果会以新消息追加，不影响后续对话。
   */
  async regenerate(idx) {
    if (this.data.sending) {
      wx.showToast({ title: '正在生成，请先停止', icon: 'none' })
      return
    }
    const messages = this.data.messages
    // 向前找最近的用户消息
    let userIdx = -1
    for (let i = idx; i >= 0; i -= 1) {
      if (messages[i] && messages[i].role === 'user') { userIdx = i; break }
    }
    if (userIdx < 0) {
      wx.showToast({ title: '找不到对应的提问', icon: 'none' })
      return
    }
    const prompt = messages[userIdx].content || ''
    if (!prompt) return

    // 截断到该用户消息之前（不含它）：send() 会把这条提问重新追加，
    // 若保留原消息会出现两个一模一样的提问气泡
    const kept = messages.slice(0, userIdx)
    this.setData({ messages: kept }, () => {
      this.setData({ input: prompt })
      this.send()
    })
  },

  /**
   * 删除这条：仅本地移除（不改服务端历史）。
   * 用 splice 后重新 setData 整数组，避免 wx:key=index 引起的错位。
   */
  deleteMessage(idx) {
    const messages = this.data.messages.slice()
    if (idx < 0 || idx >= messages.length) return
    messages.splice(idx, 1)
    this.setData({ messages }, () => this.scrollBottom())
    wx.showToast({ title: '已删除', icon: 'none' })
  },

  // ─────────────────────────── 代码与图片 ───────────────────────────

  copyCode(e) {
    const index = Number(e.currentTarget.dataset.index)
    const bi = Number(e.currentTarget.dataset.bi)
    const msg = this.data.messages[index]
    const blk = msg && msg.blocks && msg.blocks[bi]
    if (!blk || !blk.code) return
    wx.setClipboardData({
      data: blk.code,
      success: () => wx.showToast({ title: '已复制', icon: 'none' })
    })
  },

  previewImage(e) {
    const url = e.currentTarget.dataset.url
    const index = Number(e.currentTarget.dataset.index)
    const msg = this.data.messages[index] || {}
    const urls = (msg.images && msg.images.length) ? msg.images : [url]
    wx.previewImage({ current: url, urls })
  },

  onImageLongPress(e) {
    const url = e.currentTarget.dataset.url
    if (!url) return
    wx.showActionSheet({
      itemList: ['保存到相册', '预览图片'],
      success: (res) => {
        if (res.tapIndex === 0) this.saveImages([url])
        if (res.tapIndex === 1) wx.previewImage({ current: url, urls: [url] })
      },
      fail: () => { /* 用户取消 */ }
    })
  },

  /**
   * 保存图片到相册。
   * 需要 scope.writePhotosAlbum 授权：拒绝过则引导去设置页重新打开，
   * 否则会静默失败、用户不知道为什么存不下。
   */
  saveImages(urls) {
    if (!urls || !urls.length) {
      wx.showToast({ title: '没有可保存的图片', icon: 'none' })
      return
    }
    const url = urls[0]
    wx.showLoading({ title: '保存中…' })
    wx.downloadFile({
      url,
      success: (res) => {
        if (res.statusCode !== 200) {
          wx.hideLoading()
          wx.showToast({ title: '下载失败', icon: 'none' })
          return
        }
        wx.saveImageToPhotosAlbum({
          filePath: res.tempFilePath,
          success: () => {
            wx.hideLoading()
            wx.showToast({ title: '已保存到相册', icon: 'none' })
          },
          fail: (err) => {
            wx.hideLoading()
            const msg = String((err && err.errMsg) || '')
            if (msg.indexOf('auth deny') >= 0 || msg.indexOf('authorize') >= 0) {
              wx.showModal({
                title: '需要相册权限',
                content: '请在设置中允许保存到相册',
                confirmText: '去设置',
                success: (r) => { if (r.confirm) wx.openSetting() }
              })
            } else {
              wx.showToast({ title: '保存失败', icon: 'none' })
            }
          }
        })
      },
      fail: () => {
        wx.hideLoading()
        wx.showToast({ title: '下载失败', icon: 'none' })
      }
    })
  }
})
