// config.js — 多节点部署配置 + 故障转移
//
// 注意：所有生产域名必须在小程序后台「开发管理 → 服务器域名」中登记为 request
// 合法域名（必须 HTTPS + 已备案），否则真机请求会被微信拦截；开发工具可勾选
// "不校验合法域名"绕过，但正式版不行。
//
// 本地/内网调试时把 useLocal 改为 true 并填写局域网地址，同时关闭开发工具的
// 域名校验。

const CONFIG = {
  // 生产节点列表：按优先级排列，第一个不通会自动切到下一个。
  // 两个域名都反代到同一台网关 9876，token/会话完全通用。
  nodes: [
    'https://weixin.sxkiss.com',
    'https://weixin.sxkiss.cn'
  ],

  // 调试：局域网直连网关（http，仅开发工具"不校验合法域名"时可用）
  local: 'http://192.168.68.185:9876',

  // true = 使用上面的 local 地址；false = 使用 nodes 里第一个（故障转移由 runtime 处理）
  useLocal: false,

  // 请求超时（毫秒）。流式响应不在此限，由 SSE 自行控制。
  timeout: 15000,

  // 节点失效缓存 key，避免反复重试已知的坏节点
  failedNodeKey: 'mp_failed_node',
  // 最近一次成功的节点缓存 key
  workingNodeKey: 'mp_working_node'
}

/**
 * 获取当前使用的 base URL。
 *
 * 优先级：local > 上次成功的节点 > nodes[0]
 * 读取/写入 storage 时用 try/catch 保护——单元测试环境没有 wx，但本模块
 * 被多个测试依赖，一处抛错就会让整条依赖链加载失败。
 */
function getBaseUrl() {
  if (CONFIG.useLocal) return CONFIG.local
  try {
    const cached = wx.getStorageSync(CONFIG.workingNodeKey)
    if (cached && CONFIG.nodes.indexOf(cached) !== -1) return cached
  } catch (_) { /* 无 wx 环境直接跳过 */ }
  return CONFIG.nodes[0]
}

/**
 * 记录节点失效：下次 getBaseUrl 会退回首选节点重试。
 * 同样做防护，避免单元测试环境崩溃。
 */
function markNodeFailed(url) {
  try {
    const now = Date.now()
    // 写一个带时间戳的标记，避免污染 workingNodeKey（防止误清）
    wx.setStorageSync(CONFIG.failedNodeKey, now)
  } catch (_) {}
}

const currentBase = getBaseUrl()

module.exports = {
  // 保留 baseUrl 兼容历史调用方（只读，不用于故障转移）
  baseUrl: currentBase,
  timeout: CONFIG.timeout,
  CONFIG,
  getBaseUrl,
  markNodeFailed
}
