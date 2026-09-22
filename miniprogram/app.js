// app.js
App({
  globalData: {
    token: '',
    serverUrl: '' // 由 config.js 提供
  },

  onLaunch() {
    const token = wx.getStorageSync('token') || ''
    this.globalData.token = token
  },

  setToken(token) {
    this.globalData.token = token
    wx.setStorageSync('token', token)
  },

  clearToken() {
    this.globalData.token = ''
    wx.removeStorageSync('token')
  }
})
