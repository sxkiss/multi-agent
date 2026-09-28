// tests/miniprogram_store.test.js — 签到 / 统计本地存储逻辑测试
//
// store.js 依赖微信全局 wx，测试时用内存 Map 模拟其存储 API。
// 重点覆盖签到连续天数的判定（最容易错的地方）与跨天边界：
//   - 昨天签过 → 连续 +1
//   - 前天签过 → 中断，重置为 1
//   - 同一天重复签 → 拒绝且不重复计数
//   - 日期必须按本地时区计算（用 toISOString 会在 UTC 下切日错位）
//
// 运行：node --test tests/miniprogram_store.test.js

const test = require('node:test')
const assert = require('node:assert')
const path = require('path')

// ── 模拟微信存储 API ──
const mem = new Map()
global.wx = {
  getStorageSync: (k) => (mem.has(k) ? mem.get(k) : ''),
  setStorageSync: (k, v) => { mem.set(k, v) },
  removeStorageSync: (k) => { mem.delete(k) }
}

const store = require(path.join(__dirname, '..', 'miniprogram', 'utils', 'store.js'))

function d(offsetDays) {
  const t = new Date()
  t.setDate(t.getDate() + offsetDays)
  const p = (n) => String(n).padStart(2, '0')
  return `${t.getFullYear()}-${p(t.getMonth() + 1)}-${p(t.getDate())}`
}

function reset() {
  mem.clear()
}

test('首次签到：连续 1 天，累计 1 天', () => {
  reset()
  const r = store.checkin()
  assert.strictEqual(r.ok, true)
  assert.strictEqual(r.data.streak, 1)
  assert.strictEqual(r.data.total, 1)
  assert.strictEqual(store.checkedToday(), true)
})

test('同一天重复签到：拒绝且不重复计数', () => {
  reset()
  store.checkin()
  const again = store.checkin()
  assert.strictEqual(again.ok, false, '同一天应拒绝重复签到')
  assert.strictEqual(again.data.total, 1, '累计天数不得重复累加')
})

test('昨天签过：连续 +1', () => {
  reset()
  // 伪造：昨天签过，连续 3 天
  mem.set(store.KEYS.checkin, JSON.stringify({
    last: d(-1), streak: 3, total: 3, days: { [d(-1)]: true }
  }))
  const r = store.checkin()
  assert.strictEqual(r.data.streak, 4, '昨天签过应延续连续天数')
  assert.strictEqual(r.data.total, 4)
})

test('前天签过（断签）：连续重置为 1，累计继续累加', () => {
  reset()
  mem.set(store.KEYS.checkin, JSON.stringify({
    last: d(-2), streak: 9, total: 20, days: { [d(-2)]: true }
  }))
  const r = store.checkin()
  assert.strictEqual(r.data.streak, 1, '断签后连续天数必须重置')
  assert.strictEqual(r.data.total, 21, '累计天数不受断签影响')
})

test('本地时区日期：不能因 UTC 切日错位', () => {
  reset()
  const t = store.today()
  const now = new Date()
  const p = (n) => String(n).padStart(2, '0')
  const expected = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`
  assert.strictEqual(t, expected, 'today() 必须按本地时区计算')
  assert.ok(!t.includes('T'), '日期格式应为 YYYY-MM-DD')
})

test('日历：本月天数正确且标记今天', () => {
  reset()
  store.checkin()
  const grid = store.monthGrid()
  const now = new Date()
  const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()
  assert.strictEqual(grid.length, daysInMonth, '日历格子数应等于本月天数')
  const todayCell = grid.filter((c) => c.isToday)
  assert.strictEqual(todayCell.length, 1, '应恰好标记一个今天')
  assert.strictEqual(todayCell[0].checked, true, '刚签到后今天应显示已签')
})

test('使用天数：首日即为 1', () => {
  reset()
  assert.strictEqual(store.usedDays(), 1)
})

test('损坏数据不崩溃：读取非法 JSON 返回默认值', () => {
  reset()
  mem.set(store.KEYS.checkin, '{坏掉的 JSON')
  const c = store.getCheckin()
  assert.strictEqual(c.streak, 0)
  assert.deepStrictEqual(c.days, {})
  // 仍可正常签到
  assert.strictEqual(store.checkin().ok, true)
})

test('字号设置：默认 mid，可保存与读取', () => {
  reset()
  assert.strictEqual(store.getSettings().fontSize, 'mid')
  store.saveSettings({ fontSize: 'large' })
  assert.strictEqual(store.getSettings().fontSize, 'large')
  assert.strictEqual(store.FONT_SIZES.large, 34)
})

test('clearAll：清掉本模块数据但不碰 token/session_id', () => {
  reset()
  store.checkin()
  store.saveProfile({ nickName: '测试' })
  wx.setStorageSync('token', 'abc')
  wx.setStorageSync('session_id', 'wx_123')
  store.clearAll()
  assert.strictEqual(store.getCheckin().total, 0, '签到数据应被清除')
  assert.strictEqual(store.getProfile().nickName, '', '资料应被清除')
  assert.strictEqual(wx.getStorageSync('token'), 'abc', 'token 不能被清（否则等于退出登录）')
  assert.strictEqual(wx.getStorageSync('session_id'), 'wx_123', '会话绑定不能被清')
})
