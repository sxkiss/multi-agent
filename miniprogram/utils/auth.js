// utils/auth.js — 登录逻辑共享
//
// 首页与聊天页都需要"静默登录"（无 token 时自动走微信一键登录）。
// 若各页各写一份，登录失败处理/会话绑定容易改漏一处，故统一在此。
//
// 本模块只负责"拿到 token 与绑定会话"，不负责页面跳转 —— 跳哪个页面
// 是页面级决策（首页静默失败即可，聊天页要引导去密码登录页）。
const { api } = require('./request.js')

/**
 * 确保处于登录态：已有 token 直接通过，否则尝试微信静默登录。
 * 成功后会把服务端绑定的 session_id 写入本地（同一微信用户恒定会话）。
 *
 * @returns {Promise<{ok:boolean, sessionId?:string, reason?:string}>}
 */
async function ensureLogin() {
  const app = getApp()
  const token = wx.getStorageSync('token') || (app && app.globalData && app.globalData.token) || ''
  if (token) return { ok: true }

  try {
    const loginRes = await new Promise((resolve, reject) => {
      wx.login({ success: resolve, fail: reject })
    })
    if (!loginRes || !loginRes.code) {
      return { ok: false, reason: '无法获取微信登录凭证' }
    }

    const res = await api.wxLogin(loginRes.code)
    if (!res.ok || !res.data || !res.data.token) {
      const reason = res.msg || (res.unauthorized ? '微信登录未通过' : '网络异常')
      return { ok: false, reason }
    }

    if (app && app.setToken) app.setToken(res.data.token)
    const sessionId = res.data.session_id || ''
    if (sessionId) wx.setStorageSync('session_id', sessionId)
    return { ok: true, sessionId }
  } catch (e) {
    return { ok: false, reason: '网络异常' }
  }
}

/** 退出登录：清 token 与会话绑定 */
function logout() {
  const app = getApp()
  if (app && app.clearToken) app.clearToken()
  try { wx.removeStorageSync('session_id') } catch (e) { /* ignore */ }
}

module.exports = { ensureLogin, logout }
