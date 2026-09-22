// request.js — 统一网络层
//
// 所有请求自动附带 Bearer token；遇到 401 通知调用方跳转登录。
//
// 渠道标识：小程序侧无法自定义 User-Agent/Referer（微信客户端会强制覆盖），
// 因此统一用 X-Client-* 自定义头声明来源，供服务端识别渠道、做访问控制。
const { baseUrl, timeout } = require('./config.js')

const app = getApp()

/** 渠道元信息头：服务端据此识别请求来源（小程序 / 网页 / CLI） */
function clientHeaders() {
  return {
    'X-Client-Type': 'miniprogram',
    'X-Client-Version': '1.0.2',
    'X-Client-Appid': 'wxbb9e77f84a643da8'
  }
}

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
        ...clientHeaders(),
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
    // 带上渠道标识：服务端据此裁剪系统提示，避免回答里泄露网关内部信息
    return request({ url: '/api/chat/start', method: 'POST', data: Object.assign({ client_type: 'miniprogram' }, data) })
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

module.exports = { api, request, authHeader, clientHeaders, baseUrl }
