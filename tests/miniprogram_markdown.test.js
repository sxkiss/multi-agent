// tests/miniprogram_markdown.test.js — 小程序 markdown 解析器测试
//
// chat.wxml 依赖 parseMarkdown 把它拆成 文本/代码/图片 三类块：
//   - 代码块必须独立成块，UI 才能挂"复制"按钮（rich-text 无法绑点击事件）
//   - 图片必须独立成块，才支持点击预览 / 长按保存
//   - 流式中间态（代码块未闭合）必须标记 open，否则半成品会被当正常代码
// 这些约束一旦破坏，交互能力会静默失效，故单独覆盖。
//
// 运行：node --test tests/miniprogram_markdown.test.js

const test = require('node:test')
const assert = require('node:assert')
const path = require('path')

const { parseMarkdown, extractImages } = require(
  path.join(__dirname, '..', 'miniprogram', 'utils', 'markdown.js')
)

test('代码块独立成块', () => {
  const blocks = parseMarkdown('正文\n\n```python\nprint(1)\n```\n')
  const codes = blocks.filter((b) => b.type === 'code')
  assert.strictEqual(codes.length, 1, '代码块应独立成块')
  assert.strictEqual(codes[0].code, 'print(1)')
  assert.strictEqual(codes[0].lang, 'python')
  assert.strictEqual(codes[0].open, false, '闭合的代码块 open 应为 false')
})

test('未闭合代码块标记 open（流式中间态）', () => {
  const blocks = parseMarkdown('```js\nconst a = 1')
  const codes = blocks.filter((b) => b.type === 'code')
  assert.strictEqual(codes.length, 1)
  assert.strictEqual(codes[0].open, true, '未闭合代码块必须标记 open')
  assert.strictEqual(codes[0].code, 'const a = 1')
})

test('代码内容原样保留，不被 HTML 转义污染', () => {
  const src = 'a < b && c > d'
  const blocks = parseMarkdown('```\n' + src + '\n```')
  const code = blocks.find((b) => b.type === 'code')
  assert.strictEqual(code.code, src, '代码内容不得被 HTML 转义污染')
})

test('图片独立成块', () => {
  const blocks = parseMarkdown('先看图\n\n![alt](https://x.com/a.png)\n\n后文')
  const imgs = blocks.filter((b) => b.type === 'image')
  assert.strictEqual(imgs.length, 1, '图片应独立成块')
  assert.strictEqual(imgs[0].url, 'https://x.com/a.png')
  assert.strictEqual(imgs[0].alt, 'alt')
})

test('extractImages 收集全部图片', () => {
  const md = '![a](https://x/1.png)\n正文\n![b](https://x/2.png)'
  assert.deepStrictEqual(extractImages(md), ['https://x/1.png', 'https://x/2.png'])
})

test('extractImages 无图时返回空数组', () => {
  assert.deepStrictEqual(extractImages('没有图片'), [])
})

test('行内图片不当作图片块', () => {
  const blocks = parseMarkdown('文字 ![x](https://x/1.png) 结尾')
  assert.strictEqual(blocks.filter((b) => b.type === 'image').length, 0)
  const joined = blocks.map((b) => b.html || '').join('')
  assert.ok(joined.includes('https://x/1.png'), '行内图片仍应以文本形式保留')
})

test('空输入返回空数组', () => {
  assert.deepStrictEqual(parseMarkdown(''), [])
  assert.deepStrictEqual(parseMarkdown(null), [])
})

test('文本做 HTML 转义（防注入）', () => {
  const html = parseMarkdown('<script>alert(1)</script>').map((b) => b.html || '').join('')
  assert.ok(!html.includes('<script>'), '文本必须做 HTML 转义')
  assert.ok(html.includes('&lt;script&gt;'))
})

test('标题与列表渲染', () => {
  const html = parseMarkdown('# 标题\n\n- 项目一\n- 项目二').map((b) => b.html || '').join('')
  assert.ok(html.includes('标题'))
  assert.ok(html.includes('项目一'))
  assert.ok(html.includes('项目二'))
})

test('有序列表带序号', () => {
  const html = parseMarkdown('1. 第一\n2. 第二').map((b) => b.html || '').join('')
  assert.ok(html.includes('1.'))
  assert.ok(html.includes('2.'))
})

test('引用与分割线', () => {
  const html = parseMarkdown('> 引用一句\n\n---\n').map((b) => b.html || '').join('')
  assert.ok(html.includes('引用一句'))
  assert.ok(html.includes('border-left'), '引用应有左边框样式')
})

test('粗体与行内代码', () => {
  const html = parseMarkdown('这是**粗体**和`代码`').map((b) => b.html || '').join('')
  assert.ok(html.includes('font-weight:bold'))
  assert.ok(html.includes('粗体'))
  assert.ok(html.includes('代码'))
})

test('流式增量：逐步追加不丢内容', () => {
  // 模拟逐字到达：每个前缀都应能解析且不抛异常
  const full = '# 标题\n\n正文**加粗**\n\n```js\nlet a=1\n```\n\n![图](https://x/a.png)'
  for (let i = 1; i <= full.length; i += 1) {
    const blocks = parseMarkdown(full.slice(0, i))
    assert.ok(Array.isArray(blocks), `前缀长度 ${i} 应返回数组`)
  }
  const final = parseMarkdown(full)
  assert.ok(final.some((b) => b.type === 'code'), '完整文本应含代码块')
  assert.ok(final.some((b) => b.type === 'image'), '完整文本应含图片块')
})
