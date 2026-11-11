/* WEBHOOK 数据桥 · 运行时
 * 不改主题内核，只用它们挂在宿主元素上的调试接口驱动：
 *   HQ   ：__task(名字, 7, 项数, 加急, 部门链) 建真实任务；__st() 读状态
 *   PARK ：__ws 直接读写车间配置（工单标题），__ctl(k,"on") 启停车间
 * 数据源：/api/console/events 拉历史，/api/console/stream 走 SSE 增量。
 */
(function () {
  "use strict";
  var W = window;
  var seen = new Set();
  var pending = new Map();
  var subs = [];

  if (W.__WHQ_BOOT_ERR === "login") { mask(); return; }

  function mask() {
    var d = document.createElement("div");
    d.style.cssText = "position:fixed;inset:0;z-index:9999;display:grid;place-items:center;background:#07080d;" +
      "font:14px/1.7 -apple-system,'Noto Sans SC',sans-serif;color:#eef0f7;text-align:center;padding:24px";
    d.innerHTML = '<div><b style="font-size:17px">需要先登录工作台</b><br>' +
      '<span style="color:#7d8298">这个页面读的是 /api/console 的鉴权数据</span><br><br>' +
      '<a href="/viz/" style="color:#45e6f5">→ 去登录 /viz/</a></div>';
    document.body.appendChild(d);
  }

  function hash(s) {
    var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function short(s, n) {
    s = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  /* 主题是懒挂载的：切换时才 mount，所以要盯着宿主元素出现 */
  function watch(id, init) {
    var t0 = Date.now(), done = false;
    (function tick() {
      var h = document.getElementById(id);
      if (!done && h && typeof h.__st === "function") { done = true; try { init(h); } catch (e) { console.warn(e); } }
      if (Date.now() - t0 > 3600e3) return;
      setTimeout(tick, 400);
    })();
  }

  /* ---------------- 一次性消息总线 ---------------- */
  function onMsg(fn) { subs.push(fn); }
  function emit(items) { subs.forEach(function (f) { try { f(items); } catch (e) { /* 忽略 */ } }); }

  function feed(items) {
    var fresh = [];
    (items || []).forEach(function (m) {
      if (!m || !m.id || seen.has(m.id)) return;
      seen.add(m.id);
      fresh.push(m);
    });
    if (fresh.length) { HOME.msgs += fresh.length; emit(fresh); }
  }

  /* =====================================================================
     HQ 智能总部
     ===================================================================== */
  var HOME = { msgs: 0, tasks: 0, warm: 0 };

  function initHQ(h) {
    var deptOf = W.__WHQ_DEPT_OF__ || {};
    var queue = [], pumping = false;

    /* bt 实时流修复（2026-11-11）：SSE 推来的事件 name 是运行时工位
       （「会话·job」/「子代理·实例」），而 __WHQ_DEPT_OF__ 是 boot.js 在挂载前
       由 /api/console/agents 一次性装配的，覆盖不到连接之后才动态出现的工位。
       原 chainOf 对未知 name 返回 null → enqueue 直接丢弃 → 真事件全被扔掉、
       tasks 恒 0、工单区永远空（真实线上浏览器排障实证：msgs 涨、tasks=0）。
       修法：未知 name 按稳定 hash 落到已有部门槽(DEPTS 共 8)并缓存，既不丢真事件，
       又让不同工位稳定分到不同部门、不合并成一个工位；已登记的实例工位照走原逻辑。*/
    var NDEPT = 8, seenDept = {};
    function seatHash(s){var h=2166136261,t=String(s);for(var i=0;i<t.length;i++){h^=t.charCodeAt(i);h=Math.imul(h,16777619);}return h>>>0;}
    function deptFor(n){
      if (n == null) return null;
      if (deptOf[n] != null) return deptOf[n];
      if (seenDept[n] != null) return seenDept[n];
      var k = seatHash(n) % NDEPT;
      seenDept[n] = k;     // 稳定缓存；独立对象，不污染 boot.js 的 __WHQ_DEPT_OF__
      return k;
    }
    function chainOf(src, dst) {
      var a = deptFor(src), b = deptFor(dst);
      if (a == null && b == null) return null;
      if (a == null) return [b];
      if (b == null || a === b) return [a];
      return [a, b];
    }
    function enqueue(name, src, dst, urgent) {
      var chain = chainOf(src, dst);
      if (!chain) return;
      queue.push({ name: short(name, 18) || "新任务", chain: chain, urgent: !!urgent });
      pump();
    }
    function pump() {
      if (pumping || !queue.length) return;
      pumping = true;
      var job = queue.shift();
      try { h.__task(job.name, 7, 1 + (hash(job.name) % 3), job.urgent, job.chain); HOME.tasks++; }
      catch (e) { /* 主题内部异常不影响页面 */ }
      setTimeout(function () { pumping = false; pump(); }, 2200);
    }

    var SR = h.shadowRoot;
    if (SR) {
      var n = (W.__WHQ_AGENTS__ || []).length;
      var sub = SR.getElementById("mSub");
      if (sub) sub.textContent = "中继实时数据 · " + n + " 个信箱";
      var sub2 = SR.querySelector(".sub2");
      if (sub2) sub2.textContent = "中继实时数据 · " + n + " 个信箱 · Webhook";
      var eb = SR.querySelector(".eb");
      if (eb) eb.innerHTML = 'WEBHOOK RELAY · <b>LIVE</b> · ' + n + " INBOXES · 24H";
    }

    onMsg(function (items) {
      items.forEach(function (m) {
        if (m.direction === "inbound") {
          if (m.request_id) pending.set(m.request_id, m);
          else enqueue(m.name + "：" + (m.content || "[附件]"), m.name, null, false);
          return;
        }
        if (!m.request_id || !pending.has(m.request_id)) return;
        var inb = pending.get(m.request_id);
        pending.delete(m.request_id);
        enqueue(inb.content || ("[" + (inb.type || "消息") + "]"), inb.name, m.name, false);
      });
    });

    // 历史热身：让场景一开始就有活干
    fetch("/api/console/events?limit=120", { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : { events: [] }; })
      .then(function (d) {
        var buf = {}, pairs = [];
        (d.events || []).forEach(function (m) {
          if (m.direction === "inbound" && m.request_id) buf[m.request_id] = m;
          else if (m.direction === "outbound" && m.request_id && buf[m.request_id]) {
            pairs.push([buf[m.request_id], m]);
            delete buf[m.request_id];
          }
        });
        HOME.warm = pairs.length;
        pairs.slice(-6).forEach(function (p) {
          enqueue(p[0].content || "[消息]", p[0].name, p[1].name, false);
        });
      })
      .catch(function () { /* 忽略 */ });
  }

  /* =====================================================================
     PARK 智造园区：6 座车间 = 6 个信箱分组
     - 有消息的分组自动开工，15 分钟没消息的分组停机
     - 新消息标题直接推进该车间的工单池，产线上跑的就是真实内容
     ===================================================================== */
  var IDLE_MS = 15 * 60 * 1000;

  function initPark(h) {
    var groupOf = W.__WPK_HALL_OF__ || {};
    var WS = h.__ws || [];
    var last = WS.map(function () { return 0; });

    (W.__WHQ_AGENTS__ || []).forEach(function (a) {
      var k = groupOf[a.name];
      if (k != null && a.last_ts) last[k] = Math.max(last[k], a.last_ts * 1000);
    });

    var SR = h.shadowRoot;
    if (SR) {
      var sub = SR.getElementById("mSub");
      if (sub) sub.textContent = "中继实时数据 · " + WS.length + " 条产线";
      var sub2 = SR.querySelector(".sub2");
      if (sub2) sub2.textContent = "中继实时数据 · " + WS.length + " 条产线 · Webhook";
      var eb = SR.querySelector(".eb");
      if (eb) eb.innerHTML = 'AGENT OPS · <b>LIVE</b> · ' + WS.length + " LINES · WEBHOOK";
    }

    function hallK(name) { var k = groupOf[name]; return k == null ? -1 : k; }

    function pushTitle(name, text) {
      var k = hallK(name);
      if (k < 0 || !WS[k]) return;
      var t = short(text, 14);
      if (!t || t === "…") return;
      var arr = WS[k].titles || (WS[k].titles = []);
      if (arr[0] === t) return;
      arr.unshift(t);
      if (arr.length > 8) arr.length = 8;
    }

    onMsg(function (items) {
      items.forEach(function (m) {
        var k = hallK(m.name);
        if (k >= 0) last[k] = Date.now();
        pushTitle(m.name, m.content || ("[" + (m.type || "消息") + "]"));
      });
    });

    // 每 4 秒对齐一次启停：__ctl 返回每个车间的 "on/spd/wt/maint"
    function syncLines() {
      var st;
      try { st = h.__ctl(-1, ""); } catch (e) { return; }
      if (!st || !st.length) return;
      var now = Date.now();
      st.forEach(function (s, k) {
        var on = String(s).charAt(0) === "1";
        var want = now - (last[k] || 0) < IDLE_MS;
        if (on !== want) { try { h.__ctl(k, "on"); } catch (e) { /* 忽略 */ } }
      });
    }
    syncLines();
    setInterval(syncLines, 4000);
  }

  /* ---------------- 历史热身 + SSE ---------------- */
  function live() {
    if (!W.EventSource) return;
    var es = new EventSource("/api/console/stream?since=" + (Date.now() / 1000 - 5));
    es.addEventListener("messages", function (ev) {
      try {
        var items = JSON.parse(ev.data);
        if (Array.isArray(items)) feed(items);
      } catch (e) { /* 坏包忽略 */ }
    });
  }

  watch("host-hq", initHQ);
  watch("host-park", initPark);
  live();

  W.__WHQ = {
    get stats() { return { msgs: HOME.msgs, tasks: HOME.tasks, warm: HOME.warm }; },
    enqueue: function (n, s, d) { subs.length && feed([{ id: "manual" + Math.random(), name: s, direction: "outbound", content: n }]); }
  };
})();
