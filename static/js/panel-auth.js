/**
 * 静态配置面板的鉴权请求封装（Claude / OpenCode / Codex 面板共用）
 *
 * 背景：网关开启鉴权后（auth.json enabled=true），除 /api/auth/* 与 /static/
 * 外的所有接口都要求 `Authorization: Bearer <token>`。面板页本身位于 /static/
 * 下属于免鉴权前缀、能正常加载，但页面内部发出的 XHR 不受该豁免覆盖，
 * 必须显式带 token，否则一律 401（表现为面板空白、保存无反应）。
 *
 * TOKEN_KEY 必须与 Vue 端 frontend/src/auth.js 保持一致——两端共用同一个
 * localStorage 键，用户在主界面登录后，在同一浏览器里打开面板页即可直接生效。
 * 修改此值时务必同步修改另一处。
 */
(function (global) {
  'use strict'

  var TOKEN_KEY = 'bt_agent_token'

  /** 读取本地令牌；隐私模式等场景下 localStorage 不可用，降级为无令牌 */
  function getToken() {
    try {
      return global.localStorage.getItem(TOKEN_KEY) || ''
    } catch (e) {
      return ''
    }
  }

  /**
   * 合并鉴权头。无令牌时原样返回，由服务端决定放行或返回 401，
   * 不在此处抛错，保证未登录时仍能看到面板骨架而非白屏。
   */
  function authOpts(opts) {
    var token = getToken()
    if (!token) return opts || {}
    opts = opts || {}
    var headers = {}
    for (var k in opts.headers) {
      if (Object.prototype.hasOwnProperty.call(opts.headers, k)) headers[k] = opts.headers[k]
    }
    headers['Authorization'] = 'Bearer ' + token

    var out = {}
    for (var key in opts) {
      if (Object.prototype.hasOwnProperty.call(opts, key) && key !== 'headers') out[key] = opts[key]
    }
    out.headers = headers
    return out
  }

  /** 带鉴权的 fetch，用法与原生 fetch 一致 */
  function authFetch(url, opts) {
    return global.fetch(url, authOpts(opts))
  }

  global.PanelAuth = { authOpts: authOpts, authFetch: authFetch, TOKEN_KEY: TOKEN_KEY }
})(typeof globalThis !== 'undefined' ? globalThis : window)
