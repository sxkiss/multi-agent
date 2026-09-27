// pages/profile/profile.js — 设置页（tab「我的」）
const { api } = require('../../utils/request.js')
const store = require('../../utils/store.js')
const auth = require('../../utils/auth.js')

const APP_VERSION = '1.4.0'

Page({
  data: {
    fontOptions: [
      { key: 'small', label: '小' },
      { key: 'mid', label: '中' },
      { key: 'large', label: '大' }
    ],
    fontSize: 'mid',
    fontPx: 30,
    storageSize: '',
    uidMasked: '',
    version: APP_VERSION,
    // 微信机器人（每用户独立实例）：用户扫码连接自己的机器人，
    // 连接后微信里的对话与小程序共用同一条会话、同一份数据。
    botStatus: 'unknown',     // unknown | new | waiting | scaned | active | expired | error
    botQr: '',                // 二维码 data URI（waiting/scaned 时有值）
    botError: '',
    botLoading: false
  },

  onShow() {
    const s = store.getSettings()
    this.setData({
      fontSize: s.fontSize,
      fontPx: store.FONT_SIZES[s.fontSize] || store.FONT_SIZES.mid,
      uidMasked: this.maskUid(wx.getStorageSync('session_id') || ''),
      storageSize: this.calcStorage()
    })
    this.loadBotStatus()
  },

  onHide() {
    this.stopBotPolling()
  },

  onUnload() {
    this.stopBotPolling()
  },

  // ─────────────────────── 微信机器人 ───────────────────────

  /** 查询自己机器人的连接状态；通过则启动轮询等待扫码结果。 */
  async loadBotStatus() {
    const r = await auth.ensureLogin()
    if (!r.ok) return
    const res = await api.botStatus()
    if (!res.ok || !res.data) return
    const d = res.data
    this.setData({
      botStatus: d.status || 'new',
      botQr: d.qr_payload || '',
      botError: d.error || ''
    })
    // 正在等待扫码：启动轮询，扫描确认后自动更新界面
    if (d.status === 'waiting' || d.status === 'scaned') this.startBotPolling()
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
      botError: ''
    })
    this.startBotPolling()
  },

  /** 轮询扫码结果：3 秒一次，登录成功 / 过期 / 出错即停。 */
  startBotPolling() {
    this.stopBotPolling()
    this._botTimer = setInterval(async () => {
      const res = await api.botStatus()
      if (!res.ok || !res.data) return
      const d = res.data
      if (d.status !== this.data.botStatus || (d.qr_payload || '') !== this.data.botQr) {
        this.setData({
          botStatus: d.status,
          botQr: d.qr_payload || '',
          botError: d.error || ''
        })
      }
      if (d.status === 'active') {
        this.stopBotPolling()
        wx.showToast({ title: '连接成功', icon: 'success' })
      } else if (d.status === 'expired' || d.status === 'error') {
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

  /** 长按保存二维码：微信支持长按识别，也可保存后从相册识别。 */
  saveBotQr() {
    const qr = this.data.botQr
    if (!qr) return
    // data URI → 临时文件
    const fs = wx.getFileSystemManager()
    const filePath = `${wx.env.USER_DATA_PATH}/bot_qr.png`
    try {
      fs.writeFileSync(filePath, qr.split(',')[1], 'base64')
    } catch (e) {
      wx.showToast({ title: '保存失败', icon: 'none' })
      return
    }
    wx.saveImageToPhotosAlbum({
      filePath,
      success: () => wx.showToast({ title: '已保存到相册', icon: 'none' }),
      fail: () => wx.showToast({ title: '长按图片即可识别', icon: 'none' })
    })
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

  maskUid(sid) {
    if (!sid) return '未登录'
    if (sid.length <= 12) return sid
    return `${sid.slice(0, 7)}…${sid.slice(-4)}`
  },

  /** 估算本小程序占用的存储（微信未提供精确接口，按 key 累加字符数近似） */
  calcStorage() {
    try {
      const info = wx.getStorageInfoSync()
      const total = (info.keys || []).reduce((sum, k) => {
        try {
          const v = wx.getStorageSync(k)
          return sum + String(v === undefined ? '' : JSON.stringify(v)).length
        } catch (e) {
          return sum
        }
      }, 0)
      if (total < 1024) return `${total} B`
      if (total < 1024 * 1024) return `${(total / 1024).toFixed(1)} KB`
      return `${(total / 1024 / 1024).toFixed(2)} MB`
    } catch (e) {
      return ''
    }
  },

  setFontSize(e) {
    const key = e.currentTarget.dataset.key
    if (!store.FONT_SIZES[key]) return
    store.saveSettings({ fontSize: key })
    this.setData({ fontSize: key, fontPx: store.FONT_SIZES[key] })
    // 立刻把字号应用到聊天页（tab 页常驻，setData 可直接生效）
    this.applyFontToChat(store.FONT_SIZES[key])
  },

  /** 字号是跨页设置：聊天页 onShow 时也会自行读取，这里提前同步减少闪烁 */
  applyFontToChat(px) {
    const pages = getCurrentPages()
    const chatPage = pages.find((p) => p.route === 'pages/chat/chat')
    if (chatPage && chatPage.setData) {
      try { chatPage.setData({ fontPx: px }) } catch (e) { /* ignore */ }
    }
  },

  /**
   * 清除本机缓存。
   * 签到 / 资料 / 设置已同步到服务端，清除后会从服务端自动恢复，
   * 因此这里只清本地；服务端数据清不掉，也不需要用户为此担心。
   */
  clearCache() {
    wx.showModal({
      title: '清除缓存',
      content: '将删除本机缓存的签到记录、头像昵称等。数据已保存在服务端，清除后会自动恢复。',
      confirmText: '清除',
      confirmColor: '#f87171',
      success: (r) => {
        if (!r.confirm) return
        store.clearAll()
        const s = store.getSettings()
        this.setData({ fontSize: s.fontSize, fontPx: store.FONT_SIZES[s.fontSize] })
        wx.showToast({ title: '已清除，正在恢复', icon: 'none' })
        // 立刻从服务端拉回：正常情况下清完刷新发现数据还在
        store.sync().then(() => {
          const a = store.getSettings()
          this.setData({ fontSize: a.fontSize, fontPx: store.FONT_SIZES[a.fontSize] })
        })
      }
    })
  },

  logout() {
    wx.showModal({
      title: '退出登录',
      content: '退出后需要重新登录才能继续对话。',
      confirmText: '退出',
      confirmColor: '#f87171',
      success: (r) => {
        if (!r.confirm) return
        auth.logout()
        wx.reLaunch({ url: '/pages/index/index' })
      }
    })
  }
})
