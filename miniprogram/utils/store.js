// utils/store.js — 用户数据存储（签到 / 统计 / 设置 / 个人资料）
//
// 服务端权威 + 本地缓存：
//   早期版本纯本地存储，清缓存 / 换设备 / 卸载小程序即丢，签到连击这类
//   有积累价值的数据无法接受。现改为服务端为主（/api/mp/profile），
//   本地退化为「离线兜底 + 乐观更新」——断网仍可用，联网后由 sync() 校正。
//
// 约定：
//   - 本地 key 统一前缀 mp_，避免与业务 key（token/session_id）混淆。
//   - 读接口全部同步（页面渲染直接可用）；写接口本地立即生效并异步上报。
//   - 服务端文档结构与本地结构不同（dates 数组 vs days 映射），转换只在
//     本模块内做，页面拿到的始终是本地结构。
//   - 连击天数以服务端为准：客户端日期不可信，且多设备要一致。本地那份
//     只用于即时反馈，sync() 后用服务端结果覆盖。
const { api } = require('./request.js')

const PREFIX = 'mp_'

const KEYS = {
  checkin: PREFIX + 'checkin',   // {last:'2026-09-24', streak:5, total:23, days:{'2026-09-24':true}}
  stats: PREFIX + 'stats',       // {firstUse:'2026-09-01', lastUse:'2026-09-24'}
  profile: PREFIX + 'profile',   // {avatarUrl:'', nickName:''}
  settings: PREFIX + 'settings', // {fontSize:'mid'}
  migrated: PREFIX + 'migrated'  // 本地旧数据是否已并入服务端（只做一次）
}

const FONT_SIZES = { small: 26, mid: 30, large: 34 }

/**
 * 本地写入序号：防止"迟到的服务端响应"覆盖用户刚做的改动。
 *
 * 竞态场景：进入首页触发 sync()（GET 在途），用户随即点签到（本地已改）。
 * 若 GET 响应后到并直接覆盖，本地会被回退成签到前的状态 —— 显示错乱，
 * 用户还会以为没签上。故 sync() 在发起前记下序号，响应回来若发现期间
 * 发生过本地写入，就放弃本次覆盖（下次 sync 会拿到新数据）。
 */
let localWriteSeq = 0

function read(key, fallback) {
  try {
    const raw = wx.getStorageSync(key)
    if (!raw) return fallback
    return typeof raw === 'string' ? JSON.parse(raw) : raw
  } catch (e) {
    return fallback
  }
}

function write(key, value) {
  try {
    wx.setStorageSync(key, JSON.stringify(value))
    // 除 migrated 标记外，任何业务写入都计入序号：sync() 据此判断
    // "响应在途期间本地是否被改过"，避免用陈旧的服务端数据回退用户操作。
    if (key !== KEYS.migrated) localWriteSeq += 1
    return true
  } catch (e) {
    // 存储写满（微信单 key 1MB / 总量 10MB）时静默失败，不打断使用
    return false
  }
}

/** 本地日期：必须用本地时区算"今天"，用 toISOString 会按 UTC 切日导致跨天错位 */
function today() {
  const d = new Date()
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

function daysBetween(a, b) {
  const da = new Date(a + 'T00:00:00')
  const db = new Date(b + 'T00:00:00')
  return Math.round((db - da) / 86400000)
}

/** 是否具备上报条件：单测环境（mock wx）没有 request，须静默跳过 */
function canRemote() {
  return typeof wx !== 'undefined' && typeof wx.request === 'function'
}

// ─────────────────────── 服务端 ↔ 本地 结构转换 ───────────────────────

function docToLocal(doc) {
  if (!doc || typeof doc !== 'object') return null
  const ck = doc.checkin || {}
  const days = {}
  if (Array.isArray(ck.dates)) ck.dates.forEach((d) => { if (d) days[d] = true })
  const st = doc.stats || {}
  const prof = doc.profile || {}
  const se = doc.settings || {}
  return {
    checkin: {
      last: ck.last || '',
      streak: Number(ck.streak) || 0,
      total: Number(ck.total) || 0,
      days
    },
    stats: { firstUse: st.first_seen || '', lastUse: today() },
    profile: { avatarUrl: prof.avatar || '', nickName: prof.nickname || '' },
    settings: { fontSize: FONT_SIZES[se.font] ? se.font : 'mid' }
  }
}

function localToImport() {
  const ck = getCheckin()
  const st = getStats()
  const prof = getProfile()
  const se = getSettings()
  return {
    checkin: {
      last: ck.last,
      streak: ck.streak,
      total: ck.total,
      dates: Object.keys(ck.days).sort()
    },
    stats: { first_seen: st.firstUse },
    profile: { nickname: prof.nickName, avatar: prof.avatarUrl },
    settings: { font: se.fontSize }
  }
}

function applyLocal(local) {
  if (!local) return
  write(KEYS.checkin, local.checkin)
  write(KEYS.stats, local.stats)
  write(KEYS.profile, local.profile)
  write(KEYS.settings, local.settings)
}

// ─────────────────────────── 签到 ───────────────────────────

function getCheckin() {
  const d = read(KEYS.checkin, null) || { last: '', streak: 0, total: 0, days: {} }
  return {
    last: d.last || '',
    streak: Number(d.streak) || 0,
    total: Number(d.total) || 0,
    days: d.days && typeof d.days === 'object' ? d.days : {}
  }
}

/**
 * 今日是否已签到
 * 注意：签到状态按"记录里的 last 是否等于今天"判定，而不是额外存一个 today 标志，
 * 避免跨天后标志不刷新。
 */
function checkedToday() {
  return getCheckin().last === today()
}

/**
 * 执行签到。本地立即生效（乐观更新，按钮马上变"已签"），随后异步上报服务端。
 * 已签过返回 {ok:false}。
 * @returns {{ok:boolean, data:object, pending?:boolean}}
 */
function checkin() {
  const d = getCheckin()
  const t = today()
  if (d.last === t) return { ok: false, data: d }

  // 连续判定：上次签到是昨天 → +1；否则重置为 1
  const gap = d.last ? daysBetween(d.last, t) : -1
  d.streak = gap === 1 ? d.streak + 1 : 1
  d.total += 1
  d.last = t
  d.days[t] = true
  write(KEYS.checkin, d)

  if (canRemote()) {
    // 服务端是权威：返回后用它校正本地（跨设备/跨时区可能与本地算的不同）
    api.mpProfile('checkin').then((res) => {
      if (res && res.ok && res.data && res.data.doc) {
        const local = docToLocal(res.data.doc)
        if (local) {
          write(KEYS.checkin, local.checkin)
          // 本地又变了：让在途的 sync() 感知到，避免陈旧响应回退此结果
          localWriteSeq += 1
        }
      }
    })
  }
  return { ok: true, data: d }
}

/** 本月日历：[{day:1, checked:bool, isToday:bool}] */
function monthGrid() {
  const d = getCheckin()
  const now = new Date()
  const y = now.getFullYear()
  const m = now.getMonth() + 1
  const p = (n) => String(n).padStart(2, '0')
  const daysInMonth = new Date(y, m, 0).getDate()
  const t = today()
  const out = []
  for (let i = 1; i <= daysInMonth; i += 1) {
    const key = `${y}-${p(m)}-${p(i)}`
    out.push({ day: i, checked: !!d.days[key], isToday: key === t })
  }
  return out
}

// ─────────────────────────── 使用统计 ───────────────────────────

function getStats() {
  const s = read(KEYS.stats, null) || {}
  const t = today()
  if (!s.firstUse) {
    s.firstUse = t
    s.lastUse = t
    write(KEYS.stats, s)
  }
  return { firstUse: s.firstUse, lastUse: s.lastUse || t }
}

/** 使用天数（首次使用至今，含当天） */
function usedDays() {
  const s = getStats()
  return daysBetween(s.firstUse, today()) + 1
}

/** 每次进入首页时刷新 lastUse */
function touchVisit() {
  const s = getStats()
  s.lastUse = today()
  write(KEYS.stats, s)
  return s
}

// ─────────────────────────── 个人资料 ───────────────────────────

function getProfile() {
  const p = read(KEYS.profile, null) || {}
  return { avatarUrl: p.avatarUrl || '', nickName: p.nickName || '' }
}

function saveProfile(patch) {
  const p = Object.assign(getProfile(), patch || {})
  write(KEYS.profile, p)
  if (canRemote()) {
    api.mpProfile('update', { patch: { profile: { nickname: p.nickName, avatar: p.avatarUrl } } })
  }
  return p
}

// ─────────────────────────── 设置 ───────────────────────────

function getSettings() {
  const s = read(KEYS.settings, null) || {}
  return { fontSize: FONT_SIZES[s.fontSize] ? s.fontSize : 'mid' }
}

function saveSettings(patch) {
  const s = Object.assign(getSettings(), patch || {})
  if (!FONT_SIZES[s.fontSize]) s.fontSize = 'mid'
  write(KEYS.settings, s)
  if (canRemote()) {
    api.mpProfile('update', { patch: { settings: { font: s.fontSize } } })
  }
  return s
}

// ─────────────────────────── 同步 ───────────────────────────

/**
 * 与服务端对齐（首页 onShow 调用，失败静默——离线时继续用本地数据）。
 *
 * 首次同步时若服务端为空、本地已有数据，则把本地数据并入服务端
 * （老用户升级路径，见服务端 import_data 的"只填空不覆盖"约定）。
 *
 * @returns {Promise<{ok:boolean, doc?:object}>}
 */
async function sync() {
  if (!canRemote()) return { ok: false }
  // 发起前记录本地写入序号：响应回来时若序号变了，说明期间用户做过操作
  const seqAtStart = localWriteSeq
  const res = await api.mpProfile()
  if (!res || !res.ok || !res.data || !res.data.doc) return { ok: false }

  // 响应在途期间本地被改过（用户签了到 / 改了设置）→ 放弃本次覆盖。
  // 否则弱网下陈旧的 GET 会把刚签的到回退成"未签到"，且无人再校正。
  if (localWriteSeq !== seqAtStart) return { ok: true, skipped: true }

  const serverDoc = res.data.doc
  // 判断"服务端尚无数据"时不能看 stats.first_seen：
  // GET 接口内部会 touch_visit 写入 first_seen，一取就有值，
  // 若把它算作"非空"，老用户本地数据会被空文档覆盖（等于一夜清零）。
  const serverEmpty = !serverDoc.checkin.last && !(Number(serverDoc.checkin.total) > 0) &&
    !serverDoc.profile.nickname && !serverDoc.profile.avatar
  const migrated = !!read(KEYS.migrated, false)

  if (serverEmpty && !migrated) {
    const localHas = getCheckin().total > 0 || getProfile().nickName
    if (localHas) {
      const imp = await api.mpProfile('import', { data: localToImport() })
      if (imp && imp.ok && imp.data && imp.data.doc) {
        applyLocal(docToLocal(imp.data.doc))
        write(KEYS.migrated, true)
        return { ok: true, doc: imp.data.doc }
      }
    }
  }

  applyLocal(docToLocal(serverDoc))
  if (!migrated) write(KEYS.migrated, true)
  return { ok: true, doc: serverDoc }
}

/** 清除本模块写入的全部数据（保留 token / session_id，否则等于退出登录） */
function clearAll() {
  Object.keys(KEYS).forEach((k) => {
    try { wx.removeStorageSync(KEYS[k]) } catch (e) { /* ignore */ }
  })
}

module.exports = {
  KEYS,
  today,
  getCheckin,
  checkedToday,
  checkin,
  monthGrid,
  getStats,
  usedDays,
  touchVisit,
  getProfile,
  saveProfile,
  getSettings,
  saveSettings,
  sync,
  FONT_SIZES,
  clearAll
}
