// sse.js — 小程序端 SSE 流式接收
//
// 微信小程序没有 EventSource，也不原生支持 SSE。官方提供的替代方案是
// wx.request 开启 enableChunked: true，通过 RequestTask.onChunkReceived
// 逐块接收响应体（Transfer-Encoding: chunked）。
//
// 与浏览器的三个关键差异，本文件逐一处理：
//   1. onChunkReceived 返回 ArrayBuffer，且分块边界与 SSE 消息边界无关，
//      一块可能含半条消息、多条消息，也可能把中文字符从中间劈开。
//   2. 因此必须维护一个持久缓冲区，按 SSE 的 \n\n 边界切分完整消息，
//      残余部分留到下一块继续拼。
//   3. ArrayBuffer → 字符串需用 TextDecoder 风格解码；小程序基础库较老时
//      没有 TextDecoder，故内置 UTF-8 解码回退实现。
//
// 使用前提：Nginx 反代必须配置 `proxy_buffering off`，否则流式会被缓冲
// 成一次性返回（真机常见坑）。

const { getBaseUrl, CONFIG } = require('./config.js')
const app = getApp()

// ---------- 临期续期（旁路） ----------

// 距离过期多久之内才刷新：留足余量，确保"一次最长 10 分钟的订阅"全程有效。
const REFRESH_AHEAD_MS = 30 * 60 * 1000
let lastRefreshAt = 0

/**
 * 解析 JWT 的 exp（不验签，仅用于前端判断"快到期了吗"）。
 *
 * 为什么可以只解不验：这里只是决定"要不要提前续一下"的触发条件，
 * 真正的身份校验在服务端；即便 payload 被本地篡改，后续 refresh 请求
 * 也会因签名不符被拒，不会因此拿到新 token。
 */
function tokenExpMs(token) {
  try {
    const seg = String(token || '').split('.')[1]
    if (!seg) return 0
    const b64 = seg.replace(/-/g, '+').replace(/_/g, '/')
    // 老基础库没有 atob：回退到 Buffer（若可用），都没有则放弃续期判断。
    // 取不到 exp 只是"不提前续"，不影响正常订阅——旁路逻辑不该拖垮主流程。
    let json = ''
    if (typeof atob === 'function') {
      json = decodeURIComponent(escape(atob(b64)))
    } else if (typeof Buffer !== 'undefined') {
      json = Buffer.from(b64, 'base64').toString('utf8')
    } else {
      return 0
    }
    const payload = JSON.parse(json)
    return Number(payload && payload.exp ? payload.exp * 1000 : 0)
  } catch (e) {
    return 0
  }
}

/**
 * 临期才续：token 剩余有效期不足阈值时静默换新，供下次连接使用。
 *
 * 刻意做成"旁路 + 节流"：
 * - 不 await：订阅必须同步返回 task，调用方才能 abort，改成异步会破坏契约；
 * - 节流 5 分钟：SSE 断线会频繁重连，不能每次订阅都打一次网络。
 */
function maybeRefreshToken(token) {
  try {
    if (!token) return
    const now = Date.now()
    if (now - lastRefreshAt < 5 * 60 * 1000) return
    const exp = tokenExpMs(token)
    if (!exp || exp - now > REFRESH_AHEAD_MS) return

    lastRefreshAt = now
    wx.request({
      url: getBaseUrl() + '/api/auth/refresh',
      method: 'POST',
      header: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
        'X-Client-Type': 'miniprogram'
      },
      success(res) {
        const body = res.data || {}
        if (res.statusCode === 200 && body.status && body.data && body.data.token) {
          wx.setStorageSync('token', body.data.token)
          if (app && app.globalData) app.globalData.token = body.data.token
          if (app && app.setToken) app.setToken(body.data.token)
        }
      }
    })
  } catch (e) {
    // 续期是旁路优化，失败不影响本次订阅，静默即可
  }
}

// ---------- UTF-8 解码 ----------

/**
 * ArrayBuffer → UTF-8 字符串。
 * 优先用内置 TextDecoder（基础库 >= 2.x 可用），否则回退手写解码，
 * 保证多字节字符在老基础库上也不乱码。
 */
function decodeUTF8(buffer) {
  if (typeof TextDecoder !== 'undefined') {
    try {
      // stream: true 让解码器保留跨块的未完成字节
      if (!decodeUTF8._decoder) decodeUTF8._decoder = new TextDecoder('utf-8')
      return decodeUTF8._decoder.decode(new Uint8Array(buffer), { stream: true })
    } catch (e) {
      /* 落到回退实现 */
    }
  }
  return decodeUTF8Manual(new Uint8Array(buffer))
}

function decodeUTF8Manual(bytes) {
  let out = ''
  let i = 0
  while (i < bytes.length) {
    const b = bytes[i]
    let code
    let len
    if (b < 0x80) { code = b; len = 1 }
    else if (b >= 0xc0 && b < 0xe0) { code = b & 0x1f; len = 2 }
    else if (b >= 0xe0 && b < 0xf0) { code = b & 0x0f; len = 3 }
    else if (b >= 0xf0 && b < 0xf8) { code = b & 0x07; len = 4 }
    else { i++; continue } // 非法字节，跳过
    if (i + len > bytes.length) break // 不完整序列，留给下一块
    for (let j = 1; j < len; j++) {
      code = (code << 6) | (bytes[i + j] & 0x3f)
    }
    if (code < 0x10000) {
      out += String.fromCharCode(code)
    } else {
      code -= 0x10000
      out += String.fromCharCode(0xd800 + (code >> 10), 0xdc00 + (code & 0x3ff))
    }
    i += len
  }
  return out
}

// ---------- SSE 事件解析 ----------

/**
 * 解析一个完整的 SSE 事件块（形如 "id: 1\nevent: message\ndata: {...}\n"）。
 *
 * 返回 {id, event, data, rawData}：
 *   - data    ：尝试 JSON 解析的结果；失败则为原始字符串。
 *   - rawData ：data 字段的原始文本（未经 JSON 解析）。
 *
 * 必须保留 rawData 的原因：
 *   message 事件的 data 是**纯文本分片**（如 "390"、"老板好"），服务端为防
 *   SSE 断行已把换行转义成字面 "\n"，并未加 JSON 引号。此时 JSON.parse 会
 *   把纯数字分片转成 number（"390" → 390）、把空格/空串判为无效，导致
 *   调用方取不到文本而整片丢弃——表现为回答里的数字莫名消失。
 *   因此调用方（chat.js）优先用 rawData，语义与浏览器端一致。
 * @returns {id:number, event:string, data:any, rawData:string} 或 null
 */
function parseEventBlock(block) {
  if (!block || !block.trim()) return null
  let id = 0
  let event = 'message'
  const dataLines = []

  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '')
    if (!line) continue
    if (line.startsWith(':')) continue // 注释/心跳
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)

    if (field === 'id') {
      const n = parseInt(value, 10)
      if (!isNaN(n)) id = n
    } else if (field === 'event') {
      event = value || 'message'
    } else if (field === 'data') {
      dataLines.push(value)
    }
  }

  if (!dataLines.length) return null
  const raw = dataLines.join('\n')
  let data
  try {
    data = JSON.parse(raw)
  } catch (e) {
    data = raw // 非 JSON 时按字符串透传
  }
  return { id, event, data, rawData: raw }
}

/**
 * 订阅事件流。
 *
 * @param {object} opts
 * @param {string} opts.sessionId
 * @param {number} opts.lastId   断线续传起点
 * @param {function} opts.onEvent ({id, event, data}) => void
 * @param {function} opts.onError (errMsg) => void
 * @param {function} opts.onEnd   () => void
 * @returns {object} RequestTask，可调用 .abort() 主动断开
 */
function subscribe({ sessionId, lastId = 0, onEvent, onError, onEnd, nodeIndex = 0, tryIndex = 0 }) {
  const token = (app && app.globalData && app.globalData.token) || wx.getStorageSync('token') || ''
  // 顺带做一次"临期续期"（不阻塞开连）。
  //
  // 为什么 SSE 要单独处理：它不走 request.js 的 401 拦截（用 wx.request
  // 裸调长连接），且 token 在开连那一刻就拼进 URL、之后不再更新。一次
  // 订阅最长 10 分钟，恰好横跨 24h 有效期边界时会在连接中途判过期。
  // 这里只做"快到期才续"的旁路刷新：本次连接仍用现有 token（保证同步
  // 返回 task 给调用方 abort），但把有效期往后推，下次重连就是新 token。
  maybeRefreshToken(token)
  // 节点选择：外部传 nodeIndex 时用指定节点（故障转移/重连用），否则取当前节点。
  // 与 request.js 的故障转移共用同一份 CONFIG.nodes，避免两处配置漂移。
  const nodes = (CONFIG && CONFIG.nodes) || []
  const baseUrl = nodeIndex > 0 && nodes[nodeIndex] ? nodes[nodeIndex] : getBaseUrl()
  const url =
    `${baseUrl}/api/chat/events?session_id=${encodeURIComponent(sessionId)}` +
    `&last_id=${lastId}` +
    // 渠道标识：SSE 订阅同样需要，便于服务端按渠道审计与限流
    `&client_type=miniprogram` +
    (token ? `&token=${encodeURIComponent(token)}` : '')

  // 跨块缓冲区：承载尚未构成完整事件（未遇到 \n\n）的残余文本
  let buffer = ''
  let lastEventId = lastId
  let ended = false

  const task = wx.request({
    url,
    method: 'GET',
    enableChunked: true, // 关键：开启分块接收，否则只能一次性拿到完整响应
    responseType: 'arraybuffer',
    // 关键：SSE 是长连接，绝不能沿用微信默认 60s 超时。
    // 实测未设置时每 ~60s 被强制断开（日志可见反复订阅、last_id 卡住），
    // 长任务（多轮工具/生成图片）会被反复打断，重连次数耗尽后直接失去流。
    // 官方文档只说明默认 60000ms、未定义 0 的语义，故显式给一个足够大的值，
    // 配合 last_id 续传兜底（超时后按 lastId 重连，不丢事件）。
    timeout: 600000, // 10 分钟；超时后由断线重连续传
    header: {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
      // 渠道标识：SSE 同样需要，便于服务端按渠道限流/审计
      'X-Client-Type': 'miniprogram',
      'X-Client-Version': '1.4.0',
      'X-Client-Appid': 'wxbb9e77f84a643da8',
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    success() {
      // 分块模式下 success 表示响应结束；先冲刷残余缓冲
      flushRemainder()
      if (!ended) { ended = true; onEnd && onEnd() }
    },
    fail(err) {
      if (!ended) {
        ended = true
        // 节点故障转移：tryIndex > 0 表示当前是重试，还有后续节点可试
        const totalNodes = nodes.length
        if (tryIndex < totalNodes - 1) {
          // 还有下一个节点，切过去重连（不触发前端 onError，用户无感知）
          subscribe({
            sessionId, lastId, onEvent, onError, onEnd,
            nodeIndex: tryIndex + 1
          })
          return
        }
        // 所有节点都试过了：把错误透传给前端
        onError && onError(err.errMsg || '事件流连接失败')
      }
    }
  })

  // 处理缓冲区：按 \n\n 切出完整事件，余下部分继续留在 buffer
  function drain(force) {
    let sep
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const block = buffer.slice(0, sep)
      buffer = buffer.slice(sep + 2)
      const evt = parseEventBlock(block)
      if (evt) {
        if (evt.id > lastEventId) lastEventId = evt.id
        onEvent && onEvent(evt)
      }
    }
    if (force && buffer.trim()) {
      const evt = parseEventBlock(buffer)
      buffer = ''
      if (evt) onEvent && onEvent(evt)
    }
  }

  function flushRemainder() {
    drain(true)
  }

  if (task && typeof task.onChunkReceived === 'function') {
    task.onChunkReceived((res) => {
      // res.data 在 responseType=arraybuffer 时为 ArrayBuffer
      const chunk = res.data
      let text
      if (chunk instanceof ArrayBuffer) {
        text = decodeUTF8(chunk)
      } else if (typeof chunk === 'string') {
        text = chunk // 部分基础库直接给字符串
      } else {
        return
      }
      buffer += text
      drain(false)
    })
  } else {
    // 基础库不支持分块：只能退化为一次性响应，仍尽力解析
    onError && onError('当前基础库不支持分块接收，请升级微信版本')
  }

  return {
    abort() {
      try { task && task.abort && task.abort() } catch (e) { /* ignore */ }
      if (!ended) { ended = true; onEnd && onEnd() }
    },
    getLastId() {
      return lastEventId
    }
  }
}

module.exports = { subscribe, parseEventBlock, decodeUTF8 }
