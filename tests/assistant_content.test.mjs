// 前端回复正文预处理回归；所有样本均为合成内容。
// 运行：node --test tests/assistant_content.test.mjs
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

// 前端由 Vite 加载 ES modules；测试无需改动项目 package.json 的模块类型。
const source = await readFile(new URL('../frontend/src/assistant-content.js', import.meta.url), 'utf8')
const { stripInlineThinking } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`)

test('思考段闭合后保留答复正文', () => {
  const answer = '\n\n## 结果\n\n任务已完成。'
  assert.equal(stripInlineThinking('<think>先分析输入。\n</think>' + answer), answer)
})

test('思考段内空行及列表不会吞掉后续 Markdown 正文', () => {
  const answer = '\n\n## 查询结果\n\n- 第一项\n- 第二项\n\n**处理完成。**'
  const response = '<think>检查工具输出。\n\n- 第一步\n- 第二步\n</think>' + answer
  assert.equal(stripInlineThinking(response), answer)
})

test('流式未闭合思考段暂不展示', () => {
  assert.equal(stripInlineThinking('<think>尚未完成\n\n- 处理中'), '')
})

test('流式结束标签被拆分时暂不展示', () => {
  assert.equal(stripInlineThinking('<think>处理中</thi'), '')
})

test('连续前导思考段全部剥离', () => {
  assert.equal(stripInlineThinking('<think>一</think>\n<think>二</think>\n答复'), '\n答复')
})

test('普通 Markdown 原样保留', () => {
  const text = '## 标题\n\n**正文**\n\n[链接](https://example.com/)'
  assert.equal(stripInlineThinking(text), text)
})

test('代码围栏中的 think 示例原样保留', () => {
  const fence = String.fromCharCode(96).repeat(3)
  const code = fence + 'xml\n<think>示例</think>\n' + fence
  assert.equal(stripInlineThinking(code), code)
})

test('行内代码中的 think 示例原样保留', () => {
  const tick = String.fromCharCode(96)
  const text = '示例：' + tick + '<think>内容</think>' + tick
  assert.equal(stripInlineThinking(text), text)
})

test('非字符串内容安全返回空串', () => {
  for (const value of [null, undefined, 42, {}]) {
    assert.equal(stripInlineThinking(value), '')
  }
})
