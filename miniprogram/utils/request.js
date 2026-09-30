// request.js — 统一网络层（支持多节点故障转移）
//
// 所有请求自动附带 Bearer token；遇到 401 通知调用方跳转登录。
//
// 渠道标识：小程序侧无法自定义 User-Agent/Referer（微信客户端会强制覆盖），
// 因此统一用 X-Client-* 自定义头声明来源，供服务端识别渠道、做访问控制。
//
// 多节点故障转移：nodes 列表按优先级遍历，只有连接层失败（statusCode=0）才换节点；
// HTTP 有响应（含 401/500/404）说明链路通，直接返回，不换节点掩盖业务错误。
// 节点信息从 config.js 动态读取，无需修改这里。
const { getBaseUrl, timeout, CONFIG, markNodeFailed } = require('./config.js')

/**
 * 延迟获取 App 实例。
 * 模块顶层直接 getApp() 在页面尚未初始化、或单元测试环境下会抛错，
 * 而 request.js 被多个模块依赖，一处抛错会导致整条依赖链加载失败。
 * 改为调用时才取，取不到则退化为读本地 token（行为不变）。
 */
function appInstance() {
  try {
    return (typeof getApp === 'function' ? getApp() : null) || null
  } catch (e) {
    return null
  }
}

/** 渠道元信息头：服务端据此识别请求来源（小程序 / 网页 / CLI） */
function clientHeaders() {
  return {
    'X-Client-Type': 'miniprogram',
    'X-Client-Version': '1.4.0',
    'X-Client-Appid': 'wxbb9e77f84a643da8'
  }
}

function authHeader() {
  const app = appInstance()
  const token = (app && app.globalData && app.globalData.token) || wx.getStorageSync('token') || ''
  return token ? { Authorization: `Bearer ${token}` } : {}
}

/** 清除本地凭据：globalData 与 storage 一并清，确保 ensureLogin() 走重新登录分支 */
function dropToken() {
  const app = appInstance()
  if (app) {
    if (app.globalData) app.globalData.token = ''
    if (app.clearToken) app.clearToken()
  }
  try { wx.removeStorageSync('token') } catch (e) { /* ignore */ }
}

/**
 * 单次网络请求（不重试）。
 * node 可选：指定节点时跳过故障转移逻辑，用于 failover 内部透传。
 * @returns Promise<{ok:boolean, data:any, msg:string, statusCode:number}>
 */
function request({ url, method = 'GET', data = {}, header = {}, node }) {
  const baseUrl = node || getBaseUrl()
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

/**
 * 用旧 token 换新 token（滑动续期）。
 *
 * 为什么前端要主动续：后端 JWT 的 exp 签出即固定（默认 24h），到期后所有
 * 接口一律 401。若等用户发现"用不了了"再处理，体验上是掉登录；这里在 401
 * 时静默换新并重放原请求，用户完全无感。
 *
 * 并发控制：页面常并行发多个请求，若不合并会同时触发 N 次 refresh，
 * 且先返回的会把后返回的 token 覆盖掉（旧 token 作废，后续请求全 401）。
 * 故用单例 Promise 让并发请求共用一次刷新。
 */
let refreshPromise = null

function refreshToken() {
  if (refreshPromise) return refreshPromise

  refreshPromise = request({ url: '/api/auth/refresh', method: 'POST' })
    .then((res) => {
      if (res.ok && res.data && res.data.token) {
        const app = appInstance()
        if (app && app.setToken) {
          app.setToken(res.data.token)
        } else {
          wx.setStorageSync('token', res.data.token)
        }
        // 续期可能换回 session_id：与首次登录保持一致地落库，避免"聊天记录丢失"
        if (res.data.session_id) {
          wx.setStorageSync('session_id', res.data.session_id)
          if (app && app.globalData) app.globalData.sessionId = res.data.session_id
        }
        return true
      }
      // 续期失败：旧 token 已无法换发新 token（典型场景是服务端签名密钥
      // 轮换，老 token 连"我是谁"都验不出来）。此时必须把废凭据清掉——
      // 否则 ensureLogin() 只看"token 是否存在"就判定已登录，永远不去
      // 重新走微信授权，用户就卡在每秒 401 的重试死循环里出不来。
      dropToken()
      return false
    })
    .catch(() => false)
    .finally(() => {
      // 无论成败都清空：成功后后续请求直接用新 token；失败则允许下次重试，
      // 否则一次失败会把后续所有请求永久挡住。
      refreshPromise = null
    })

  return refreshPromise
}

/**
 * 带 401 自动续期的请求：先发一次，遇到 401 就刷新 token 再重放。
 *
 * 只对"曾经登录过"的情况续期——本地没有 token 时说明本来就没登录，
 * 刷新也没有意义，直接把 401 抛给调用方去引导登录。
 */
function requestWithRefresh({ url, method = 'GET', data = {}, header = {}, node }) {
  return request({ url, method, data, header, node }).then((res) => {
    if (res.statusCode !== 401 || url === '/api/auth/refresh') return res

    const app = appInstance()
    const hasToken = !!(app && app.globalData && app.globalData.token) ||
      !!wx.getStorageSync('token')
    if (!hasToken) return res

    return refreshToken().then((ok) => {
      if (!ok) return res
      // 关键：重放时必须重新读 token——authHeader() 在 request() 内部取，
      // 续期后已写入 storage/globalData，这里重发即可带上新凭据。
      return request({ url, method, data, header, node })
    })
  })
}

/**
 * 节点故障转移：把一次请求依次打到 nodes 列表里的每个节点。
 *
 * @returns Promise<{ok, data, msg, statusCode, unauthorized, tried}>
 *   - tried: 尝试过的节点列表，便于排查"哪个节点坏了"
 */
function requestWithFailover({ url, method = 'GET', data = {}, header = {} }) {
  const nodes = (CONFIG && CONFIG.nodes) || []

  // 单节点（或 local 调试）时无需故障转移：保持原行为，避免多一次包装
  if (nodes.length <= 1 || CONFIG.useLocal) {
    return requestWithRefresh({ url, method, data, header })
  }

  return new Promise((resolve) => {
    let idx = 0
    const tried = []

    function attempt() {
      if (idx >= nodes.length) {
        // 所有节点都不通：返回最后一个的错误，附带尝试过的节点便于排查
        resolve({
          ok: false,
          data: null,
          msg: '网络连接失败，请检查网络后重试',
          statusCode: 0,
          tried
        })
        return
      }
      const node = nodes[idx]
      idx += 1
      tried.push(node)

      requestWithRefresh({ url, method, data, header, node }).then((res) => {
        // HTTP 有响应 = 链路通，业务结果直接返回（401/500/404 等不换节点）
        if (res.statusCode !== 0) {
          resolve(res)
          return
        }
        // 连接层失败：换下一个节点
        markNodeFailed(node)
        attempt()
      })
    }

    attempt()
  })
}

const api = {
  // ---- 鉴权 ----
  login(password) {
    return requestWithFailover({ url: '/api/auth/login', method: 'POST', data: { password } })
  },
  // 微信登录：wx.login 拿 code → 服务端换 openid 并签发 token
  wxLogin(code) {
    return requestWithFailover({ url: '/api/auth/wxlogin', method: 'POST', data: { code } })
  },
  authStatus() {
    return requestWithFailover({ url: '/api/auth/status' })
  },

  // ---- 会话 ----
  // 注意：/api/chat/history 返回的是「会话列表」（[{session_id,title,...}]），
  // 不是某个会话的消息；某会话的消息要用 /api/chat/messages。
  // 早期小程序误用 history 取消息，导致每次刷新历史都是空白。
  messages(sessionId) {
    return requestWithFailover({ url: '/api/chat/messages', data: { session_id: sessionId } })
  },
  history(params) {
    return requestWithFailover({ url: '/api/chat/history', data: params })
  },
  start(data) {
    // 带上渠道标识：服务端据此裁剪系统提示，避免回答里泄露网关内部信息
    return requestWithFailover({ url: '/api/chat/start', method: 'POST', data: Object.assign({ client_type: 'miniprogram' }, data) })
  },
  stop(sessionId) {
    return requestWithFailover({ url: '/api/chat/stop', method: 'POST', data: { session_id: sessionId } })
  },
  status(sessionId) {
    return requestWithFailover({ url: '/api/chat/status', data: { session_id: sessionId } })
  },

  // ---- 用户数据（服务端权威存储，见 utils/store.js）----
  // 服务端 /api/mp/profile：GET 拉全量；POST 按 action 执行签到/更新/导入。
  // 身份由 Bearer token 决定，不接受客户端传 user_key。
  mpProfile(action, payload) {
    if (action) {
      return requestWithFailover({
        url: '/api/mp/profile',
        method: 'POST',
        data: Object.assign({ action }, payload || {})
      })
    }
    return requestWithFailover({ url: '/api/mp/profile' })
  },

  // ---- 微信机器人（每用户独立实例，扫码连接）----
  // 取码请求携带用户 token，服务端据此创建"属于你的"机器人槽位；
  // 连接后微信里的对话与小程序共用同一条会话（session_id = user_key）。
  botQrcode() {
    return requestWithFailover({ url: '/api/mp/bot/qrcode', method: 'POST' })
  },
  botStatus() {
    return requestWithFailover({ url: '/api/mp/bot/status' })
  },
  botDisconnect() {
    return requestWithFailover({ url: '/api/mp/bot/disconnect', method: 'POST' })
  },

  // ---- 元信息 ----
  agents() {
    return requestWithFailover({ url: '/api/agents' })
  },
  config() {
    return requestWithFailover({ url: '/api/config' })
  }
}

module.exports = { api, request, requestWithFailover, authHeader, clientHeaders, getBaseUrl, CONFIG }
