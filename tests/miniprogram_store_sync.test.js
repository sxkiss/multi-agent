// tests/miniprogram_store_sync.test.js — store.js 服务端联动测试
//
// store.js 的核心改造是「服务端权威 + 本地缓存」：
//   读：本地同步返回，页面立即渲染；sync() 拉服务端校正
//   写：本地乐观更新，异步上报；服务端返回后以服务端为准
//
// 这几条最容易错，且错了会直接丢用户数据：
//   - 老用户升级迁移：本地有数据、服务端为空时必须 import，否则"一夜清零"
//   - 迁移判断不能看 stats.first_seen：GET 接口本身会写它，一取就有值，
//     若拿它当"服务端非空"的信号，空文档会覆盖本地数据
//   - 迁移只做一次：重复 import 无意义且浪费请求
//   - 离线静默：断网时本地数据必须原样保留（服务端不可用≠数据作废）
//
// 实现方式：mock 微信存储 API + 覆盖 request.js 的 api.mpProfile 为
// 有状态的假服务端（会真实处理 checkin/import/update）。
//
// 运行：node --test tests/miniprogram_store_sync.test.js

const test = require('node:test')
const assert = require('node:assert')
const path = require('path')

// ── 模拟微信环境 ──
const mem = new Map()
global.wx = {
  getStorageSync: (k) => (mem.has(k) ? mem.get(k) : ''),
  setStorageSync: (k, v) => { mem.set(k, v) },
  removeStorageSync: (k) => { mem.delete(k) },
  request: () => {}   // 有它 canRemote() 才为真，模拟"可联网"
}

const REQ_PATH = path.join(__dirname, '..', 'miniprogram', 'utils', 'request.js')
const STORE_PATH = path.join(__dirname, '..', 'miniprogram', 'utils', 'store.js')

// 先加载真实 request.js，再替换其 api.mpProfile。
// store.js 里 require('./request.js') 命中同一份模块缓存，解构出的 api 是同一对象。
const reqMod = require(REQ_PATH)
const store = require(STORE_PATH)

// ── 假服务端（有状态）──
const TODAY = store.today()
const YESTERDAY = (() => {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
})()

let server = null
let calls = []

function clone(o) { return JSON.parse(JSON.stringify(o)) }

function makeServerDoc(over) {
  const base = {
    version: 1,
    profile: { nickname: '', avatar: '', updated_at: 0 },
    checkin: { last: '', streak: 0, total: 0, dates: [] },
    settings: { font: 'mid' },
    stats: { first_seen: '', used_days: 0 },
    updated_at: 0
  }
  return Object.assign(base, over || {})
}

/** 安装假服务端：真实处理 action，模拟服务端行为 */
function installApi() {
  reqMod.api.mpProfile = async (action, payload) => {
    calls.push({ action, payload })
    if (!action) {
      // GET：模拟 touch_visit 写入 first_seen（这正是迁移判断的坑）
      if (!server.stats.first_seen) {
        server.stats.first_seen = TODAY
        server.stats.used_days = 1
      }
      return { ok: true, data: { doc: clone(server), today: TODAY } }
    }
    if (action === 'checkin') {
      const ck = server.checkin
      if (ck.last !== TODAY) {
        ck.streak = ck.last === YESTERDAY ? ck.streak + 1 : 1
        ck.total += 1
        ck.last = TODAY
        if (!ck.dates.includes(TODAY)) ck.dates.push(TODAY)
      }
      return { ok: true, data: { doc: clone(server), today: TODAY } }
    }
    if (action === 'import') {
      const d = payload.data || {}
      const ck = server.checkin
      const sc = d.checkin || {}
      if (!ck.last && !ck.total && !ck.dates.length) {
        ck.last = sc.last || ''
        ck.streak = sc.streak || 0
        ck.total = sc.total || 0
        ck.dates = Array.isArray(sc.dates) ? sc.dates.slice() : []
      }
      const sp = d.profile || {}
      if (!server.profile.nickname && sp.nickname) server.profile.nickname = sp.nickname
      if (!server.settings.font || server.settings.font === 'mid') {
        if ((d.settings || {}).font) server.settings.font = d.settings.font
      }
      const ss = d.stats || {}
      if (!server.stats.first_seen && ss.first_seen) server.stats.first_seen = ss.first_seen
      return { ok: true, data: { doc: clone(server) } }
    }
    if (action === 'update') {
      const p = payload.patch || {}
      if (p.settings) Object.assign(server.settings, p.settings)
      if (p.profile) Object.assign(server.profile, p.profile)
      return { ok: true, data: { doc: clone(server) } }
    }
    return { ok: false, msg: '未知 action' }
  }
}

function reset(serverOver) {
  mem.clear()
  calls = []
  server = makeServerDoc(serverOver)
  installApi()
}

/** 直接往本地存储塞签到数据（模拟老版本升级上来的用户），不触发网络 */
function seedLocalCheckin(data) {
  wx.setStorageSync(store.KEYS.checkin, JSON.stringify(data))
}

function seedLocalProfile(prof) {
  wx.setStorageSync(store.KEYS.profile, JSON.stringify(prof))
}

// ─────────────────────────── 同步：以服务端为准 ───────────────────────────

test('sync: 服务端有数据 → 覆盖本地（换设备恢复）', async () => {
  reset()
  seedLocalCheckin({ last: '2020-01-01', streak: 1, total: 1, days: {} })
  server.checkin = { last: TODAY, streak: 15, total: 42, dates: [TODAY] }

  await store.sync()
  const c = store.getCheckin()
  assert.strictEqual(c.streak, 15, '应以服务端为准')
  assert.strictEqual(c.total, 42)
  assert.ok(c.days[TODAY], '服务端 dates 数组应转成本地 days 映射')
  assert.strictEqual(store.checkedToday(), true)
})

test('sync: 服务端未知字号回退 mid', async () => {
  reset()
  server.settings = { font: 'xxx' }
  await store.sync()
  assert.strictEqual(store.getSettings().fontSize, 'mid', '未知档位不得写入本地')
})

// ─────────────────────────── 首次迁移 ───────────────────────────

test('sync: 服务端空 + 本地有数据 → 触发 import 迁移', async () => {
  reset()
  seedLocalCheckin({ last: YESTERDAY, streak: 3, total: 3, days: { [YESTERDAY]: true } })

  await store.sync()
  const imp = calls.find((c) => c.action === 'import')
  assert.ok(imp, '老用户本地数据必须迁移，否则等于一夜清零')
  assert.strictEqual(imp.payload.data.checkin.total, 3)
  assert.strictEqual(imp.payload.data.checkin.last, YESTERDAY)
  assert.deepStrictEqual(imp.payload.data.checkin.dates, [YESTERDAY])
})

test('sync: 迁移后本地保持原数据（不被空文档覆盖）', async () => {
  reset()
  seedLocalCheckin({ last: YESTERDAY, streak: 5, total: 9, days: { [YESTERDAY]: true } })

  await store.sync()
  const c = store.getCheckin()
  assert.strictEqual(c.streak, 5, '迁移后本地连击不得变成 0')
  assert.strictEqual(c.total, 9)
})

test('sync: 服务端空 + 本地也空 → 不触发 import', async () => {
  reset()
  await store.sync()
  assert.ok(!calls.some((c) => c.action === 'import'), '无本地数据不必迁移')
})

test('sync: 迁移只做一次', async () => {
  reset()
  seedLocalCheckin({ last: YESTERDAY, streak: 1, total: 1, days: {} })

  await store.sync()
  const n1 = calls.filter((c) => c.action === 'import').length
  await store.sync()
  const n2 = calls.filter((c) => c.action === 'import').length
  assert.strictEqual(n1, 1, '第一次应迁移')
  assert.strictEqual(n2, 1, '第二次不应重复迁移')
})

test('sync: 迁移判断不得依赖 stats.first_seen（GET 会写入它）', async () => {
  reset()
  // 关键场景：GET 返回的文档里 first_seen 已有值（touch_visit 写的），
  // 但签到/资料全空 —— 必须仍判定为「服务端空」并触发迁移
  seedLocalCheckin({ last: YESTERDAY, streak: 2, total: 2, days: {} })

  await store.sync()
  assert.ok(calls.some((c) => c.action === 'import'),
    'first_seen 不能作为"服务端非空"的依据，否则老用户数据会被空文档覆盖')
})

// ─────────────────────────── 写操作 ───────────────────────────

test('checkin: 本地立即生效并异步上报', async () => {
  reset()
  const r = store.checkin()
  assert.strictEqual(r.ok, true, '本地应立刻签到成功（乐观更新）')
  assert.strictEqual(store.checkedToday(), true, '按钮应马上变已签')

  await new Promise((res) => setTimeout(res, 30))
  assert.ok(calls.some((c) => c.action === 'checkin'), '应异步上报服务端')
  assert.strictEqual(server.checkin.total, 1, '服务端应记录签到')
})

test('checkin: 服务端结果校正本地（多设备连击以服务端为准）', async () => {
  reset()
  // 服务端已有 14 天连击（另一台设备签的），本地是全新的
  server.checkin = { last: YESTERDAY, streak: 14, total: 14, dates: [YESTERDAY] }

  const r = store.checkin()
  assert.strictEqual(r.data.streak, 1, '本地乐观更新：不知道服务端历史，先按 1 算')

  await new Promise((res) => setTimeout(res, 30))
  assert.strictEqual(store.getCheckin().streak, 15,
    '服务端返回后应校正为 15（跨设备连击不能各算各的）')
})

test('saveSettings: 上报 + 非法值回退 mid', async () => {
  reset()
  store.saveSettings({ fontSize: 'large' })
  assert.strictEqual(store.getSettings().fontSize, 'large')
  await new Promise((res) => setTimeout(res, 30))
  assert.strictEqual(server.settings.font, 'large', '字号应上报服务端')

  store.saveSettings({ fontSize: 'bogus' })
  assert.strictEqual(store.getSettings().fontSize, 'mid', '非法字号本地回退')
})

test('saveProfile: 字段名转换 nickName→nickname', async () => {
  reset()
  store.saveProfile({ nickName: '李四' })
  await new Promise((res) => setTimeout(res, 30))
  assert.strictEqual(server.profile.nickname, '李四', '须按服务端字段名上报')
})

// ─────────────────────────── 离线兜底 ───────────────────────────

test('sync 失败：本地数据原样保留', async () => {
  reset()
  seedLocalCheckin({ last: TODAY, streak: 7, total: 7, days: { [TODAY]: true } })

  reqMod.api.mpProfile = async () => ({ ok: false, msg: '网络错误' })
  const r = await store.sync()
  assert.strictEqual(r.ok, false)
  assert.strictEqual(store.getCheckin().total, 7, '断网不得破坏本地数据')
  assert.strictEqual(store.checkedToday(), true)
})

test('sync: 在途期间用户改过数据 → 放弃覆盖（防陈旧响应回退）', async () => {
  reset()
  const ck = makeServerDoc().checkin
  server.checkin = { last: YESTERDAY, streak: 14, total: 14, dates: [YESTERDAY] }
  // 本地也同步过，避免走迁移分支（迁移分支有自己的保护，会掩盖此竞态）
  seedLocalCheckin({ last: YESTERDAY, streak: 14, total: 14, days: { [YESTERDAY]: true } })
  seedLocalProfile({ avatarUrl: '', nickName: '老张' })
  assert.ok(ck !== null)

  // GET 延迟返回，模拟弱网；期间用户点了签到
  const slowGet = reqMod.api.mpProfile
  reqMod.api.mpProfile = async (action, payload) => {
    if (!action) {
      const snap = clone(server)                        // 请求到达时的快照
      await new Promise((r) => setTimeout(r, 120))
      return { ok: true, data: { doc: snap, today: TODAY } }
    }
    return slowGet(action, payload)
  }

  const p = store.sync()                                 // GET 在途
  await new Promise((r) => setTimeout(r, 5))
  store.checkin()                                        // 用户签到
  await new Promise((r) => setTimeout(r, 20))            // 签到响应已回
  assert.strictEqual(store.checkedToday(), true)

  await p                                                // 陈旧 GET 到达
  assert.strictEqual(store.getCheckin().total, 15,
    '迟到的陈旧响应不得把用户刚签的到回退')
  assert.strictEqual(store.checkedToday(), true,
    '更不能回退成"未签到"（回退后无人再校正）')
})

test('无 wx.request 时（单测/异常环境）写操作不抛错', async () => {
  reset()
  const savedReq = wx.request
  delete wx.request
  try {
    seedLocalCheckin({ last: '', streak: 0, total: 0, days: {} })
    const r = store.checkin()
    assert.strictEqual(r.ok, true, '无网络能力时仍应本地成功')
    const rr = await store.sync()
    assert.strictEqual(rr.ok, false, '不可联网时 sync 静默失败')
  } finally {
    wx.request = savedReq
  }
})
