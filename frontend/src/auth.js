/**
 * 网关鉴权：令牌存取与请求头注入
 *
 * 背景：网关开启鉴权后，除 /api/auth/* 与静态资源外的所有接口都要求
 * `Authorization: Bearer <token>`。本模块提供统一入口，避免在多个
 * Vue 组件里各写一份 token 读取逻辑。
 *
 * 存储：localStorage。选择它而非 cookie，是因为微信小程序等外部客户端
 * 不发 cookie，统一走 header 才能让多端行为一致。
 */

const TOKEN_KEY = 'bt_agent_token'

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token) {
  try {
    localStorage.setItem(TOKEN_KEY, token)
  } catch {
    /* 隐私模式下 localStorage 可能不可用，降级为内存态 */
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
}

/**
 * 生成带鉴权头的 fetch 选项。
 * 未持有令牌时返回原样 opts，由服务端决定放行或返回 401。
 */
export function authOpts(opts = {}) {
  const token = getToken()
  if (!token) return opts
  return {
    ...opts,
    headers: { ...(opts.headers || {}), Authorization: `Bearer ${token}` },
  }
}

/**
 * 统一带鉴权的 fetch。用法与原生 fetch 一致。
 */
export function authFetch(url, opts = {}) {
  return fetch(url, authOpts(opts))
}

/**
 * 登录。首次调用会用传入密码初始化管理员凭据。
 * @returns {Promise<{ok: boolean, msg: string}>}
 */
export async function login(password) {
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    })
    const res = await r.json().catch(() => ({}))
    if (res && res.status && res.data && res.data.token) {
      setToken(res.data.token)
      return { ok: true, msg: '' }
    }
    return { ok: false, msg: (res && res.msg) || '登录失败' }
  } catch (e) {
    return { ok: false, msg: String((e && e.message) || e) }
  }
}

/** 查询服务端鉴权状态：是否启用、是否已初始化密码 */
export async function authStatus() {
  try {
    const r = await fetch('/api/auth/status')
    const res = await r.json().catch(() => ({}))
    return {
      enabled: !!(res && res.data && res.data.enabled),
      initialized: !!(res && res.data && res.data.initialized),
      hasToken: !!getToken(),
    }
  } catch {
    return { enabled: false, initialized: false, hasToken: !!getToken() }
  }
}

export function logout() {
  clearToken()
}
