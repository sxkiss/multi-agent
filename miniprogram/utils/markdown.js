// utils/markdown.js — Markdown → 小程序可渲染的"块"数组
//
// 为什么不直接输出一整段 HTML 交给 rich-text：
//   rich-text 无法绑定点击事件，代码块就没法挂"复制"按钮，图片也没法
//   点击预览 / 长按保存。因此按块拆分：文本块交给 rich-text，代码块与
//   图片块用原生组件渲染，从而支持交互。
//
// 流式场景：模型逐字输出时代码块往往尚未闭合（缺收尾 ```），此时把该块
// 标记为 open，UI 据此显示"生成中"而不是把它当普通代码。

// 配色跟随小程序深色主题（页面 #0f1115 / 气泡 #1a1d24，正文 #e8eaed）。
// 早期版本误用浅色主题的近黑色与浅灰底，在深色气泡上糊成一团，这里统一深色适配。
const MD_STYLE = {
  p: 'margin:0 0 12rpx 0;line-height:1.7;color:#e8eaed;',
  h: 'margin:16rpx 0 8rpx 0;font-weight:bold;font-size:30rpx;color:#ffffff;',
  li: 'margin:0 0 6rpx 0;line-height:1.7;padding-left:8rpx;color:#e8eaed;',
  quote: 'margin:8rpx 0;padding:8rpx 16rpx;border-left:6rpx solid #4a7cf7;color:#a8adb8;background:#20242c;',
  codeInline: 'padding:2rpx 8rpx;background:#20242c;border-radius:6rpx;font-size:26rpx;color:#7dd3a8;',
  strong: 'font-weight:bold;color:#ffffff;',
  a: 'color:#6ea8fe;text-decoration:underline;'
}

const MD_HR = '<div style="margin:12rpx 0;height:1rpx;background:#2c303a;"></div>'

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/** 行内标记：**粗体**、`代码`、[文字](链接)、![alt](图片) */
function inlineHtml(text) {
  let out = ''
  let last = 0
  // 图片分支必须排在链接之前：否则 ![x](url) 会被链接规则吃掉，
  // alt 文字变成了链接、URL 直接丢失（信息不可见）。
  const re = /!\[([^\]]*)\]\(([^)\s]+)\)|(\*\*|__)(.+?)\3|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)/g
  let m
  while ((m = re.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index))
    if (m[2] !== undefined) {
      // 行内图片：保留可见的图片地址（渲染层只对独立成行的图片做真图片块）
      out += `<span style="${MD_STYLE.a}">${escapeHtml(m[2])}</span>`
    } else if (m[4] !== undefined) {
      out += `<span style="${MD_STYLE.strong}">${escapeHtml(m[4])}</span>`
    } else if (m[5] !== undefined) {
      out += `<span style="${MD_STYLE.codeInline}">${escapeHtml(m[5])}</span>`
    } else {
      out += `<span style="${MD_STYLE.a}">${escapeHtml(m[6])}</span>`
    }
    last = m.index + m[0].length
  }
  out += escapeHtml(text.slice(last))
  return out
}

// 独立成行的图片：![alt](url)
const RE_IMAGE_LINE = /^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$/

/**
 * 解析 markdown 为块数组。
 *
 * @param {string} md
 * @returns {Array<{type:string}>} 元素形如：
 *   {type:'text', html:string}
 *   {type:'code', code:string, lang:string, open:boolean}
 *   {type:'image', url:string, alt:string}
 */
function parseMarkdown(md) {
  const blocks = []
  if (!md) return blocks

  const lines = String(md).split('\n')

  let inCode = false
  let codeBuf = []
  let codeLang = ''
  let listBuf = []
  let ordered = false
  let html = '' // 当前文本块累积的 HTML

  const flushList = () => {
    if (!listBuf.length) return
    listBuf.forEach((t, i) => {
      const prefix = ordered ? `${i + 1}. ` : '\u2022 '
      html += `<div style="${MD_STYLE.li}">${escapeHtml(prefix)}${inlineHtml(t)}</div>`
    })
    listBuf = []
  }

  const flushText = () => {
    flushList()
    if (html) {
      blocks.push({ type: 'text', html })
      html = ''
    }
  }

  const flushCode = (open) => {
    const code = codeBuf.join('\n')
    // open 块即使内容为空也要输出，让 UI 能显示"代码生成中"
    if (code.length || open) {
      blocks.push({ type: 'code', code, lang: codeLang, open: !!open })
    }
    codeBuf = []
    codeLang = ''
  }

  for (const rawLine of lines) {
    const line = rawLine.replace(/\r$/, '')

    // 围栏代码块
    const fence = line.match(/^\s*```(.*)$/)
    if (fence) {
      if (inCode) {
        flushCode(false)
        inCode = false
      } else {
        flushText()
        inCode = true
        codeLang = (fence[1] || '').trim()
      }
      continue
    }
    if (inCode) { codeBuf.push(line); continue }

    // 图片独立成行
    const img = line.match(RE_IMAGE_LINE)
    if (img) {
      flushText()
      blocks.push({ type: 'image', url: img[2], alt: img[1] || '' })
      continue
    }

    if (!line.trim()) { flushList(); continue }

    let m
    if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushList()
      html += `<div style="${MD_STYLE.quote}">${inlineHtml(m[1])}</div>`
      continue
    }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
      flushList()
      html += MD_HR
      continue
    }
    if ((m = line.match(/^\s*(#{1,6})\s+(.*)$/))) {
      flushList()
      html += `<div style="${MD_STYLE.h}">${inlineHtml(m[2])}</div>`
      continue
    }
    if ((m = line.match(/^\s*[-*+]\s+(.*)$/))) {
      if (ordered) { flushList(); ordered = false }
      listBuf.push(m[1])
      continue
    }
    if ((m = line.match(/^\s*(\d+)[.)]\s+(.*)$/))) {
      if (!ordered) { flushList(); ordered = true }
      listBuf.push(m[2])
      continue
    }
    flushList()
    html += `<div style="${MD_STYLE.p}">${inlineHtml(line)}</div>`
  }

  flushList()
  // 收尾：代码块可能尚未闭合（流式中间态）
  if (inCode) flushCode(true)
  flushText()

  return blocks
}

/** 提取文本中独立成行的图片地址，用于"预览全部图片" */
function extractImages(md) {
  const urls = []
  for (const line of String(md || '').split('\n')) {
    const m = line.match(RE_IMAGE_LINE)
    if (m) urls.push(m[2])
  }
  return urls
}

module.exports = { parseMarkdown, extractImages, escapeHtml }
