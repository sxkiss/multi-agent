// request.js — 统一网络层
//
// 所有请求自动附带 Bearer token；遇到 401 通知调用方跳转登录。
const { baseUrl, timeout } = require('./config.js')

const app = getApp()

function authHeader() {
  const token = (app && app.globalData && app.globalData.token) || wx.getStorageSync('token') || ''
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/**
 * 普通请求。
 * @returns Promise<{ok:boolean, data:any, msg:string, statusCode:number}>
 */
function request({ url, method = 'GET', data = {}, header = {} }) {
  return new Promise((resolve) => {
    wx.request({
      url: baseUrl + url,
      method,
      data,
      timeout,
      header: {
        'Content-Type': 'application/json',
        ...authHeader(),
        ...header
      },
      success(res) {
        const statusCode = res.statusCode
        if (statusCode === 401) {
          resolve({ ok: false, unauthorized: true, msg: '未授权', statusCode, data: null })
          return
        }
        const body = res.data || {}
        if (statusCode >= 200 && statusCode < 300) {
          // 后端统一 {status, data, msg} 结构
          if (typeof body === 'object' && 'status' in body) {
            resolve({
              ok: !!body.status,
              data: body.data,
              msg: body.msg || '',
              statusCode
            })
          } else {
            resolve({ ok: true, data: body, msg: '', statusCode })
          }
        } else {
          resolve({ ok: false, data: null, msg: body.msg || `请求失败(${statusCode})`, statusCode })
        }
      },
      fail(err) {
        resolve({ ok: false, data: null, msg: err.errMsg || '网络错误', statusCode: 0 })
      }
    })
  })
}

const api = {
  // ---- 鉴权 ----
  login(password) {
    return request({ url: '/api/auth/login', method: 'POST', data: { password } })
  },
  // 微信登录：wx.login 拿 code → 服务端换 openid 并签发 token
  wxLogin(code) {
    return request({ url: '/api/auth/wxlogin', method: 'POST', data: { code } })
  },
  authStatus() {
    return request({ url: '/api/auth/status' })
  },

  // ---- 会话 ----
  history(params) {
    return request({ url: '/api/chat/history', data: params })
  },
  start(data) {
    return request({ url: '/api/chat/start', method: 'POST', data })
  },
  stop(sessionId) {
    return request({ url: '/api/chat/stop', method: 'POST', data: { session_id: sessionId } })
  },
  status(sessionId) {
    return request({ url: '/api/chat/status', data: { session_id: sessionId } })
  },

  // ---- 元信息 ----
  agents() {
    return request({ url: '/api/agents' })
  },
  config() {
    return request({ url: '/api/config' })
  }
}

module.exports = { api, request, authHeader, baseUrl }
