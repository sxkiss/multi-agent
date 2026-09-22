// config.js — 部署配置
//
// 注意：域名必须在小程序后台「开发管理 → 服务器域名」中登记为 request 合法域名，
// 且必须使用 HTTPS 与已备案域名，否则真机请求会被微信拦截（开发工具可临时
// 勾选"不校验合法域名"绕过，但正式版不行）。
//
// 本地/内网调试时把 useLocal 改为 true 并填写局域网地址，
// 同时关闭开发工具的域名校验。

const CONFIG = {
  // 生产：走已备案 HTTPS 域名（经 Nginx 反代到网关 9876）
  production: 'https://weixin.sxkiss.com',

  // 调试：局域网直连网关（http，仅开发工具"不校验合法域名"时可用）
  local: 'http://192.168.68.185:9876',

  // true = 使用上面的 local 地址；false = 使用 production
  useLocal: false,

  // 请求超时（毫秒）。流式响应不在此限，由 SSE 自行控制。
  timeout: 15000
}

module.exports = {
  baseUrl: CONFIG.useLocal ? CONFIG.local : CONFIG.production,
  timeout: CONFIG.timeout,
  CONFIG
}
