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

  // 全局错误兜底：无此处理时脚本异常会导致白屏且无任何提示，
  // 用户只能杀掉小程序重进，极难排查。
  onError(err) {
    console.error('[全局错误]', err)
    try {
      wx.showToast({ title: '出现异常，请重试', icon: 'none' })
    } catch (e) { /* 某些时机无法弹 toast，忽略 */ }
  },

  onUnhandledRejection(res) {
    console.error('[未处理的 Promise 异常]', res && res.reason)
  },

  onPageNotFound(res) {
    // 避免路径错误时白屏，回到首页。
    // 必须用 reLaunch：加了 tabBar 之后，tabBar 页面不能用 redirectTo 跳转
    // （会报 fail can not redirectTo a tabbar page），首页属于 tabBar 页。
    wx.reLaunch({ url: '/pages/index/index' })
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
