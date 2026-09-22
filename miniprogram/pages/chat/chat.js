// pages/chat/chat.js
const { api } = require('../../utils/request.js')
const { subscribe } = require('../../utils/sse.js')

// SSE 事件类型 → 页面处理
const MAX_RESUME = 5

Page({
  data: {
    messages: [],      // {role:'user'|'ai', content:'', thinking:'', done:bool}
    input: '',
    sessionId: '',
    sending: false,
    streaming: false,
    statusText: '',
    scrollToBottom: ''
  },

  onLoad() {
    // 用本地持久化 session_id，保证刷新后仍能续聊
    let sid = wx.getStorageSync('session_id')
    if (!sid) {
      sid = 'mp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
      wx.setStorageSync('session_id', sid)
    }
    this.setData({ sessionId: sid })
    this.checkAuthAndLoad()
  },

  onUnload() {
    this.abortStream()
  },

  onHide() {
    // 页面隐藏时不断开 SSE：后端任务是后台式的，回来可续传
  },

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
   * 微信登录：wx.login 取 code → 服务端换 openid + 绑定会话。
   * 成功后把服务端返回的 session_id 持久化，保证同一用户始终回到同一会话。
   * @returns {boolean} 是否登录成功
   */
  async wxLogin() {
    try {
      const loginRes = await new Promise((resolve, reject) => {
        wx.login({ success: resolve, fail: reject })
      })
      if (!loginRes.code) {
        wx.redirectTo({ url: '/pages/login/login' })
        return false
      }

      const res = await api.wxLogin(loginRes.code)
      if (!res.ok || !res.data || !res.data.token) {
        // 服务端未配置小程序凭据、或 code 无效时，退回密码登录
        wx.redirectTo({ url: '/pages/login/login' })
        return false
      }

      const app = getApp()
      app.setToken(res.data.token)
      // 关键：用服务端绑定的会话 ID，实现"换设备也能续聊"
      if (res.data.session_id) {
        wx.setStorageSync('session_id', res.data.session_id)
        this.setData({ sessionId: res.data.session_id })
      }
      return true
    } catch (e) {
      wx.redirectTo({ url: '/pages/login/login' })
      return false
    }
  },

  async loadHistory() {
    const res = await api.history({ session_id: this.data.sessionId })
    if (res.ok && res.data) {
      // 历史事件结构依后端而定，尽量兼容数组/对象两种形态
      const list = Array.isArray(res.data) ? res.data : (res.data.messages || [])
      const messages = list
        .filter((m) => m && (m.role || m.content))
        .map((m) => ({
          role: m.role === 'user' ? 'user' : 'ai',
          content: String(m.content || ''),
          thinking: '',
          done: true
        }))
      if (messages.length) {
        this.setData({ messages }, () => this.scrollBottom())
      }
    }
  },

  onInput(e) {
    this.setData({ input: e.detail.value })
  },

  async send() {
    const text = String(this.data.input || '').trim()
    if (!text || this.data.sending) return

    const messages = this.data.messages.concat([
      { role: 'user', content: text, thinking: '', done: true },
      { role: 'ai', content: '', thinking: '', done: false }
    ])
    this.setData({ input: '', messages, sending: true, streaming: true }, () => this.scrollBottom())

    const res = await api.start({
      message: text,
      session_id: this.data.sessionId,
      mode: 'group'
    })

    if (!res.ok) {
      if (res.unauthorized) {
        wx.redirectTo({ url: '/pages/login/login' })
        return
      }
      this.finishStream()
      this.setData({ statusText: res.msg || '发送失败' })
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
    const { event, data } = evt
    if (evt.id) this.lastId = evt.id

    const messages = this.data.messages.slice()
    const ai = messages[messages.length - 1]
    if (!ai || ai.role !== 'ai') return

    switch (event) {
      case 'message': {
        // 后端 message 事件的 data 是 JSON 字符串或纯文本
        const text = typeof data === 'string' ? data : (data && (data.content || data.text)) || ''
        if (text) {
          ai.content += text
          this.setData({ messages }, () => this.scrollBottom())
        }
        break
      }
      case 'message_think': {
        const text = typeof data === 'string' ? data : (data && (data.content || data.text)) || ''
        if (text) ai.thinking += text
        break
      }
      case 'tool_call': {
        const name = data && data.function && data.function.name
        if (name) {
          ai.content += `\n\n> 调用工具：${name}\n`
          this.setData({ messages }, () => this.scrollBottom())
        }
        break
      }
      case 'tool_result': {
        ai.content += `\n`
        this.setData({ messages })
        break
      }
      case 'error': {
        const msg = (data && data.msg) || '出错了'
        ai.content += `\n\n⚠️ ${msg}`
        ai.done = true
        this.setData({ messages }, () => this.scrollBottom())
        break
      }
      case 'message_end': {
        ai.done = true
        this.setData({ messages }, () => this.scrollBottom())
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

  newSession() {
    this.abortStream()
    // 保持在同一用户的命名空间内：若为微信绑定会话（wx_ 前缀），
    // 新会话以其为基追加时间戳，避免脱离用户绑定导致换设备丢失。
    const cur = this.data.sessionId || ''
    const base = cur.startsWith('wx_') ? cur.slice(0, 27) : 'mp_' + Date.now().toString(36)
    const sid = base + '_' + Date.now().toString(36)
    wx.setStorageSync('session_id', sid)
    this.setData({ sessionId: sid, messages: [], statusText: '' })
  }
})
