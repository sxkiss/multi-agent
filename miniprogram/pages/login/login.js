// pages/login/login.js
const { api } = require('../../utils/request.js')

Page({
  data: {
    password: '',
    loading: false,
    error: '',
    initialized: true,
    checking: true
  },

  async onLoad() {
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
  },

  onInput(e) {
    this.setData({ password: e.detail.value })
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
      wx.navigateBack({
        fail() {
          wx.reLaunch({ url: '/pages/chat/chat' })
        }
      })
    } else {
      this.setData({ error: res.msg || '登录失败' })
    }
  }
})
