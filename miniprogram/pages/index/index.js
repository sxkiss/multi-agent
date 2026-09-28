// pages/index/index.js — 首页（默认启动页）
//
// 定位：聊天是能力入口而非门面，首页承担「个人中心 + 签到 + 统计 + 入口」。
// 数据全部本地存储（utils/store.js），累计消息数复用已有的 /api/chat/messages
// 接口计算 —— 它属于对话能力本身，不新增后端接口。
const { api } = require('../../utils/request.js')
const store = require('../../utils/store.js')
const auth = require('../../utils/auth.js')

// 应用版本：与 app.json / 上传版本保持一致
const APP_VERSION = '1.3.0'

Page({
  data: {
    profile: { avatarUrl: '', nickName: '' },
    profileInitial: 'U',
    uidMasked: '',
    checkin: { streak: 0, total: 0 },
    checkedToday: false,
    calendar: [],
    usedDays: 1,
    msgCount: -1,        // -1 = 尚未取到（显示占位符，不假装是 0）
    fontPx: 30,
    version: APP_VERSION
  },

  onLoad() {
    this.applyFontSize()
  },

  onShow() {
    this.refresh()
    this.loadMsgCount()
  },

  /**
   * 刷新本地数据（签到跨天后状态会变，故每次 onShow 都重算）。
   * 先渲染本地缓存，再用服务端数据校正 —— 换设备或清缓存后能自动恢复。
   */
  refresh() {
    const profile = store.getProfile()
    const checkin = store.getCheckin()
    store.touchVisit()

    this.setData({
      profile,
      profileInitial: (profile.nickName || 'U').slice(0, 1).toUpperCase(),
      uidMasked: this.maskUid(wx.getStorageSync('session_id') || ''),
      checkin,
      checkedToday: store.checkedToday(),
      calendar: store.monthGrid(),
      usedDays: store.usedDays()
    })

    // 服务端校正：成功则重渲染，失败静默（离线继续用本地）
    store.sync().then((r) => {
      if (!r.ok) return
      const p = store.getProfile()
      const ck = store.getCheckin()
      this.setData({
        profile: p,
        profileInitial: (p.nickName || 'U').slice(0, 1).toUpperCase(),
        checkin: ck,
        checkedToday: store.checkedToday(),
        calendar: store.monthGrid(),
        usedDays: store.usedDays()
      })
      this.applyFontSize()
    })
  },

  applyFontSize() {
    const s = store.getSettings()
    const px = store.FONT_SIZES[s.fontSize] || store.FONT_SIZES.mid
    this.setData({ fontPx: px })
  },

  /** 账号标识打码：wx_73e9cb753c08b1a893c15a25 → wx_73e9…5a25 */
  maskUid(sid) {
    if (!sid) return '未登录'
    if (sid.length <= 12) return sid
    return `${sid.slice(0, 7)}…${sid.slice(-4)}`
  },

  /**
   * 累计消息数：读当前会话的历史条数（复用已有接口，不新增后端接口）。
   * 未登录时先静默登录；仍失败则保持 -1 显示" — "，不用 0 冒充。
   */
  async loadMsgCount() {
    let sid = wx.getStorageSync('session_id') || ''
    if (!sid) {
      const r = await auth.ensureLogin()
      if (!r.ok) return           // 静默失败：首页不阻塞，统计显示占位符
      sid = r.sessionId || wx.getStorageSync('session_id') || ''
      this.setData({ uidMasked: this.maskUid(sid) })
    }
    if (!sid) return

    const res = await api.messages(sid)
    if (res.ok && Array.isArray(res.data)) {
      this.setData({ msgCount: res.data.length })
    }
  },

  // ─────────────────────────── 资料 ───────────────────────────

  /**
   * 微信头像选择。
   * 临时文件在小程序重启后会被清理，必须转存到用户目录才能长期显示。
   */
  onChooseAvatar(e) {
    const tmp = e.detail && e.detail.avatarUrl
    if (!tmp) return
    const fs = wx.getFileSystemManager()
    const target = `${wx.env.USER_DATA_PATH}/avatar_${Date.now()}.png`
    try {
      fs.copyFileSync(tmp, target)
    } catch (err) {
      // 转存失败就直接用临时路径（本次可见，重启可能丢）
      this.setData({ profile: store.saveProfile({ avatarUrl: tmp }) })
      return
    }
    const profile = store.saveProfile({ avatarUrl: target })
    this.setData({ profile })
    wx.showToast({ title: '已更新头像', icon: 'none' })
  },

  onNicknameBlur(e) {
    const name = String((e.detail && e.detail.value) || '').trim()
    if (!name) return
    const profile = store.saveProfile({ nickName: name })
    this.setData({ profile, profileInitial: name.slice(0, 1).toUpperCase() })
  },

  // ─────────────────────────── 签到 ───────────────────────────

  doCheckin() {
    const r = store.checkin()
    if (!r.ok) {
      wx.showToast({ title: '今天已经签过啦', icon: 'none' })
      return
    }
    const streak = r.data.streak
    // 连击文案：给点即时反馈，但不做积分/奖励（避免审核风险）
    const tip = streak >= 7 ? `连续 ${streak} 天，厉害！` : `已连续签到 ${streak} 天`
    wx.showToast({ title: tip, icon: 'none' })
    this.refresh()
  },

  // ─────────────────────────── 导航 ───────────────────────────

  goChat() {
    wx.switchTab({ url: '/pages/chat/chat' })
  },

  /** 带快捷指令入口：切到聊天页并提示用户点 ⚡ 按钮 */
  goChatQuick() {
    wx.switchTab({
      url: '/pages/chat/chat',
      success: () => {
        // 页面已存在时 switchTab 不触发 onLoad，用 eventChannel 无法传参，
        // 这里保持简单：切过去即可，快捷面板在聊天页点 ⚡ 打开
        wx.showToast({ title: '点 ⚡ 打开快捷指令', icon: 'none' })
      }
    })
  },

  goProfile() {
    wx.switchTab({ url: '/pages/profile/profile' })
  },

  onShareAppMessage() {
    return { title: '多智能体协作助手', path: '/pages/index/index' }
  }
})
