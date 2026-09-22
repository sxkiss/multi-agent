// pages/login/login.js
const { api } = require('../../utils/request.js')

Page({
  data: {
    password: '',
    loading: false,
    error: '',
    initialized: true,
    checking: true,
    wxLoginAvailable: false,
    wxLoading: false,
    wxError: ''
  },

  async onLoad(opts) {
    // 从聊天页跳转过来时带上微信登录失败原因，避免用户对着登录框一头雾水
    if (opts && opts.reason) {
      this.setData({ wxError: decodeURIComponent(opts.reason) })
    }
    const res = await api.authStatus()
    if (res.unauthorized) {
      // 鉴权开启时 status 接口本身是白名单，理论上不会 401
      this.setData({ initialized: true, checking: false })
      return
    }
    if (res.ok && res.data) {
      this.setData({
        initialized: !!res.data.initialized,
        checking: false
      })
    } else {
      this.setData({ checking: false })
    }

    // 探测服务端是否支持微信登录：尝试 wx.login 拿 code，能拿到就说明
    // 小程序环境可用；服务端未配置凭据时该按钮不显示（避免点了必然失败）。
    wx.login({
      success: (r) => this.setData({ wxLoginAvailable: !!r.code }),
      fail: () => this.setData({ wxLoginAvailable: false })
    })
  },

  onInput(e) {
    this.setData({ password: e.detail.value })
  },

  /** 微信一键登录：code → 服务端换 openid → 签发 token + 绑定会话 */
  async submitWxLogin() {
    if (this.data.wxLoading || this.data.loading) return
    this.setData({ wxLoading: true, error: '', wxError: '' })

    try {
      const loginRes = await new Promise((resolve, reject) => {
        wx.login({ success: resolve, fail: reject })
      })
      if (!loginRes.code) throw new Error('wx.login 未返回 code')

      const res = await api.wxLogin(loginRes.code)
      if (!res.ok || !res.data || !res.data.token) {
        // 区分：401 通常是 code 失效/凭据未配置；其余为网络或服务异常
        this.setData({
          wxLoading: false,
          wxError: res.unauthorized ? '微信登录失败，请重试或用密码登录' : (res.msg || '微信登录失败')
        })
        return
      }

      const app = getApp()
      app.setToken(res.data.token)
      if (res.data.session_id) wx.setStorageSync('session_id', res.data.session_id)
      wx.showToast({ title: '登录成功', icon: 'success' })
      this.goChat()
    } catch (e) {
      this.setData({ wxLoading: false, wxError: '微信登录失败，请改用密码登录' })
    }
  },

  /** 登录成功后回聊天页：优先返回，失败则重开（兼容直接进入登录页的场景） */
  goChat() {
    wx.navigateBack({
      fail() {
        wx.reLaunch({ url: '/pages/chat/chat' })
      }
    })
  },

  async submit() {
    const pwd = String(this.data.password || '')
    if (!pwd) {
      this.setData({ error: '请输入密码' })
      return
    }
    if (!this.data.initialized && pwd.length < 6) {
      this.setData({ error: '密码至少 6 位' })
      return
    }

    this.setData({ loading: true, error: '' })
    const res = await api.login(pwd)
    this.setData({ loading: false })

    if (res.ok && res.data && res.data.token) {
      const app = getApp()
      app.setToken(res.data.token)
      wx.showToast({ title: '登录成功', icon: 'success' })
      this.goChat()
    } else {
      this.setData({ error: res.msg || '登录失败' })
    }
  }
})
