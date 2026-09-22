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

const { baseUrl } = require('./config.js')
const app = getApp()

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
 * @returns {id:number, event:string, data:any} 或 null
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
    data = raw // 非 JSON 时按字符串透传（如 message 事件是纯文本 JSON 串）
  }
  return { id, event, data }
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
function subscribe({ sessionId, lastId = 0, onEvent, onError, onEnd }) {
  const token = (app && app.globalData && app.globalData.token) || wx.getStorageSync('token') || ''
  const url =
    `${baseUrl}/api/chat/events?session_id=${encodeURIComponent(sessionId)}` +
    `&last_id=${lastId}` +
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
    header: {
      Accept: 'text/event-stream',
      'Cache-Control': 'no-cache',
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
