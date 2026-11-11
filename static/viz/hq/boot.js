/* WEBHOOK 数据桥 · 引导脚本
 * 必须在主题挂载之前同步拿到数据：花名册、部门/车间名、初始工单标题都是 mountX() 里一次性装配的，
 * 挂载后再改会和座位、标签、面板打架。所以这里用同步 XHR 先把两份数据备好：
 *   __WHQ_*  → HQ 智能总部（人 + 部门 + 项目）
 *   __WPK_*  → PARK 智造园区（6 座车间 + 工单标题）
 * 拿不到（未登录 / 异常）时静默退回主题自带示例，页面不至于白屏。
 */
(function () {
  "use strict";
  var W = window;

  var HAIRS = ["#2a221c", "#151515", "#5a3a22", "#8a5a33", "#c98d4a", "#3a7bd5", "#e0569a", "#6b4bd6", "#3fc1a8", "#ffb547", "#7fe3ff"];
  var SHIRTS = ["#ffc35c", "#fbe7c6", "#9ad1c4", "#7fd3ff", "#e9e4da", "#c9b8ff", "#a8e063", "#ff9a62", "#ff8fc0", "#ffd1e3", "#ffd38a", "#bdf2dc", "#ffe08a", "#efe3ff", "#c8e05a", "#3d4658", "#2f3a4a", "#c8553d"];
  var SKINS = ["#f6d5b8", "#eec39a", "#f3cfb0", "#d9a77c", "#f6d8be", "#f2cba6"];

  function hash(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function pick(arr, n) { return arr[(n >>> 0) % arr.length]; }
  function cut(s, n) { s = String(s == null ? "" : s); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

  function GET(url) {
    try {
      var x = new XMLHttpRequest();
      x.open("GET", url, false);
      x.setRequestHeader("Accept", "application/json");
      x.send(null);
      if (x.status === 200) return JSON.parse(x.responseText);
      if (x.status === 401) W.__WHQ_BOOT_ERR = "login";
      else if (!W.__WHQ_BOOT_ERR) W.__WHQ_BOOT_ERR = "http " + x.status;
    } catch (e) {
      if (!W.__WHQ_BOOT_ERR) W.__WHQ_BOOT_ERR = String((e && e.message) || e);
    }
    return null;
  }

  W.__WHQ_LIVE__ = false;
  W.__WHQ_BOOT_ERR = "";

  var data = GET("/api/console/agents");
  var agents = (data && data.agents) || [];
  if (!agents.length) {
    if (!W.__WHQ_BOOT_ERR) W.__WHQ_BOOT_ERR = "empty";
    return;
  }

  /* ---- 分组：同一个角色归一间房 / 一条产线 ---- */
  var groups = {}, order = [];
  agents.forEach(function (a) {
    var g = (a.role || "未分组").trim() || "未分组";
    if (!groups[g]) { groups[g] = []; order.push(g); }
    groups[g].push(a);
  });
  order.sort(function (p, q) { return groups[q].length - groups[p].length; });
  if (order.length > 6) {
    var rest = order.slice(5), merged = groups["其它"] = [];
    rest.forEach(function (g) { merged.push.apply(merged, groups[g]); delete groups[g]; });
    order = order.slice(0, 5).concat(["其它"]);
  }

  var groupOf = {};
  order.forEach(function (g, k) { groups[g].forEach(function (a) { groupOf[a.name] = k; }); });

  /* ================= HQ：8 个部门 ================= */
  var DEPTS = [];
  for (var i = 0; i < 8; i++) {
    var g = order[i];
    if (!g) { DEPTS.push({ zh: "空闲工位", en: "IDLE", fn: "暂无该分组" }); continue; }
    DEPTS.push({
      zh: cut(g, 6),
      en: "ROLE · " + groups[g].length,
      fn: groups[g].map(function (a) { return a.display_name || a.name; }).join(" · ")
    });
  }
  W.__WHQ_DEPTS__ = DEPTS;

  var heads = {};
  agents.forEach(function (a) {
    var d = groupOf[a.name];
    if (heads[d] == null || (a.total || 0) > (heads[d].total || 0)) heads[d] = a;
  });

  W.__WHQ_ROSTER__ = agents.map(function (a) {
    var h = hash(a.name), d = groupOf[a.name] == null ? 0 : groupOf[a.name];
    var st = a.status === "online" ? "在线" : (a.status === "idle" ? "静默" : "无消息");
    return [
      cut(a.display_name || a.name, 8),
      cut(st + " · " + (a.role || "Agent"), 14),
      d, 1,
      heads[d] && heads[d].name === a.name ? 1 : 0,
      h % 5, pick(HAIRS, h >>> 3), pick(SHIRTS, h >>> 7), pick(SKINS, h >>> 11),
      ["收 " + (a.inbound || 0), "发 " + (a.outbound || 0), a.name]
    ];
  });
  W.__WHQ_AGENTS__ = agents;
  W.__WHQ_DEPT_OF__ = groupOf;

  /* ================= PARK：6 座车间 ================= */
  var ev = GET("/api/console/events?limit=200");
  var byHall = {};
  ((ev && ev.events) || []).forEach(function (m) {
    var k = groupOf[m.name];
    if (k == null) return;
    (byHall[k] = byHall[k] || []).push(m);
  });

  var HALLS = [];
  for (var j = 0; j < 6; j++) {
    var gg = order[j];
    if (!gg) { HALLS.push({ zh: "空闲车间", en: "IDLE", ag: "待命 Agent", titles: ["等待新消息"] }); continue; }
    var msgs = (byHall[j] || []).slice(-8).reverse().map(function (m) {
      return cut((m.content || "[" + (m.type || "消息") + "]").replace(/\s+/g, " "), 14);
    });
    HALLS.push({
      zh: cut(gg, 6),
      en: "ROLE · " + groups[gg].length,
      ag: cut(gg, 4) + " Agent",
      titles: msgs.length ? msgs : ["等待新消息"]
    });
  }
  W.__WPK_HALLS__ = HALLS;
  W.__WPK_GROUP_OF__ = groupOf;
  W.__WPK_HALL_OF__ = groupOf;      // 同一个映射，语义别名
  W.__WHQ_LIVE__ = true;

  /* 默认进 HQ；记住用户上次的选择，但只允许这两个主题 */
  try {
    var last = localStorage.getItem("agent-series-theme");
    if (last !== "park" && last !== "hq") localStorage.setItem("agent-series-theme", "hq");
  } catch (e) { /* 忽略 */ }
})();
