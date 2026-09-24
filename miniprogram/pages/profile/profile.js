// pages/profile/profile.js — 设置页（tab「我的」）
//
// 只读本地数据 + 清理操作；不改后端。
const store = require('../../utils/store.js')
const auth = require('../../utils/auth.js')

const APP_VERSION = '1.1.0'

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
    version: APP_VERSION
  },

  onShow() {
    const s = store.getSettings()
    this.setData({
      fontSize: s.fontSize,
      fontPx: store.FONT_SIZES[s.fontSize] || store.FONT_SIZES.mid,
      uidMasked: this.maskUid(wx.getStorageSync('session_id') || ''),
      storageSize: this.calcStorage()
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
