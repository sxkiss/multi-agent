// pages/chat/chat.js
const { api } = require('../../utils/request.js')
const { subscribe } = require('../../utils/sse.js')

// SSE 事件类型 → 页面处理
const MAX_RESUME = 5

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

/**
 * 极简 Markdown → 小程序 rich-text 可用的 HTML 片段。
 *
 * 小程序没有 marked / v-html，这里按行处理最常用的语法：标题、有序/无序
 * 列表、代码块、行内代码、粗体、引用、分割线、链接。
 * rich-text 的 nodes 模式不支持 wxss class，故输出 HTML 字符串并内联 style
 * （这种用法下 style 生效），避免引入第三方渲染库。
 */
// 配色必须跟随小程序的深色主题（页面 #0f1115 / 气泡 #1a1d24，正文 #e8eaed）。
// 早期版本误用了浅色主题的近黑色（#111827）与浅灰底（#f3f4f6），
// 在深色气泡上会糊成一团、几乎看不清，这里统一改为深色适配。
const MD_STYLE = {
  p: 'margin:0 0 12rpx 0;line-height:1.7;color:#e8eaed;',
  h: 'margin:16rpx 0 8rpx 0;font-weight:bold;font-size:30rpx;color:#ffffff;',
  li: 'margin:0 0 6rpx 0;line-height:1.7;padding-left:8rpx;color:#e8eaed;',
  quote: 'margin:8rpx 0;padding:8rpx 16rpx;border-left:6rpx solid #4a7cf7;color:#a8adb8;background:#20242c;',
  code: 'margin:8rpx 0;padding:12rpx 16rpx;background:#12141a;border:1rpx solid #2c303a;border-radius:8rpx;font-size:24rpx;color:#7dd3a8;white-space:pre-wrap;word-break:break-all;',
  codeInline: 'padding:2rpx 8rpx;background:#20242c;border-radius:6rpx;font-size:26rpx;color:#7dd3a8;',
  strong: 'font-weight:bold;color:#ffffff;',
  a: 'color:#6ea8fe;text-decoration:underline;'
}

// 分割线：深色底上用浅灰细线
const MD_HR = '<div style="margin:12rpx 0;height:1rpx;background:#2c303a;"></div>'

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 行内标记：**粗体**、`代码`、[文字](链接) */
function inlineHtml(text) {
  let out = ''
  let last = 0
  const re = /(\*\*|__)(.+?)\1|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)/g
  let m
  while ((m = re.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index))
    if (m[2] !== undefined) {
      out += `<span style="${MD_STYLE.strong}">${escapeHtml(m[2])}</span>`
    } else if (m[3] !== undefined) {
      out += `<span style="${MD_STYLE.codeInline}">${escapeHtml(m[3])}</span>`
    } else {
      out += `<span style="${MD_STYLE.a}">${escapeHtml(m[4])}</span>`
    }
    last = m.index + m[0].length
  }
  out += escapeHtml(text.slice(last))
  return out
}

function markdownToHtml(md) {
  if (!md) return ''
  const lines = String(md).split('\n')
  let html = ''
  let inCode = false
  let codeBuf = []
  let listBuf = []
  let ordered = false

  const flushList = () => {
    if (!listBuf.length) return
    listBuf.forEach((t, i) => {
      const prefix = ordered ? `${i + 1}. ` : '\u2022 '
      html += `<div style="${MD_STYLE.li}">${escapeHtml(prefix)}${inlineHtml(t)}</div>`
    })
    listBuf = []
  }
  const flushCode = () => {
    if (!codeBuf.length) return
    html += `<div style="${MD_STYLE.code}">${escapeHtml(codeBuf.join('\n'))}</div>`
    codeBuf = []
  }

  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, '')
    if (/^\s*```/.test(line)) {
      if (inCode) { flushCode(); inCode = false } else { flushList(); inCode = true }
      continue
    }
    if (inCode) { codeBuf.push(line); continue }
    if (!line.trim()) { flushList(); flushCode(); continue }

    let m
    if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushList()
      html += `<div style="${MD_STYLE.quote}">${inlineHtml(m[1])}</div>`
      continue
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flushList()
      html += MD_HR
      continue
    }
    if ((m = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
      flushList()
      html += `<div style="${MD_STYLE.h}">${inlineHtml(m[2])}</div>`
      continue
    }
    if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      if (ordered) { flushList(); ordered = false }
      listBuf.push(m[1])
      continue
    }
    if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
      if (!ordered) { flushList(); ordered = true }
      listBuf.push(m[2])
      continue
    }
    flushList()
    html += `<div style="${MD_STYLE.p}">${inlineHtml(line)}</div>`
  }
  flushList()
  flushCode()
  return html
}

/** JSON 解析：失败返回 null（不抛异常打断渲染） */
function safeJson(text) {
  try { return JSON.parse(text) } catch (e) { return null }
}

/** 把消息文本同步渲染为 rich-text nodes，挂到消息对象上 */
function decorate(messages) {
  return messages.map((m) => (
    m.role === 'ai'
      ? Object.assign({}, m, { nodes: markdownToHtml(m.content || '') })
      : Object.assign({}, m, { nodes: null })
  ))
}

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
        // 带上失败原因跳登录页：否则用户只看到登录框，不知道微信登录为何失败
        const reason = encodeURIComponent(res.msg || (res.unauthorized ? '微信登录未通过' : '网络异常'))
        wx.redirectTo({ url: `/pages/login/login?reason=${reason}` })
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
        this.setData({ messages: decorate(messages) }, () => this.scrollBottom())
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
    this.setData({ input: '', messages: decorate(messages), sending: true, streaming: true }, () => this.scrollBottom())

    const res = await api.start({
      message: text,
      session_id: this.data.sessionId,
      mode: 'group'
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

    /** 同步刷新气泡（含 markdown 渲染结果 nodes） */
    const flush = () => {
      ai.nodes = markdownToHtml(ai.content)
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
        const src = typeof rawData === 'string' ? rawData : data
        const text = unescapeSSEString(
          typeof src === 'string' ? src : (src && (src.content || src.text)) || ''
        )
        if (text) ai.thinking += text
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
