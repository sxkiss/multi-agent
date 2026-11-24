/* ============================================================
 * update-notifier.js — 框架更新提示（非侵入式，纯 DOM 注入）
 *
 * 流程：
 *   1. 启动延迟 3s 调 GET /api/update/status（后端会按需生成 version.json 基线）
 *   2. 若有更新且未被「稍后再说」抑制 → 弹提示窗，展示 commit 列表
 *   3. 点「立即更新」→ 二次确认 → POST /api/update/apply → 显示结果
 *   4. 全程不自动安装；失败可点「回滚」→ POST /api/update/rollback
 *
 * 安全：远端 commit 内容一律用 textContent 写入，绝不 innerHTML。
 * ============================================================ */
(function () {
  'use strict';

  var DISMISS_KEY = 'bt-update-dismiss-until';   // 「稍后再说」到期时间戳(ms)
  var DISMISS_MS = 6 * 60 * 60 * 1000;           // 抑制 6 小时

  function getToken() {
    try { return localStorage.getItem('bt_agent_token') || ''; } catch (e) { return ''; }
  }

  async function api(path, opts) {
    opts = opts || {};
    var headers = Object.assign({}, opts.headers || {}, { 'Accept': 'application/json' });
    var t = getToken();
    if (t) headers['Authorization'] = 'Bearer ' + t;
    if (opts.body) headers['Content-Type'] = 'application/json';
    var res = await fetch(path, Object.assign({}, opts, { headers: headers }));
    return await res.json();
  }

  function dismissed() {
    try { return Date.now() < Number(localStorage.getItem(DISMISS_KEY) || 0); }
    catch (e) { return false; }
  }
  function snooze() {
    try { localStorage.setItem(DISMISS_KEY, String(Date.now() + DISMISS_MS)); } catch (e) {}
  }

  function el(tag, style, text) {
    var n = document.createElement(tag);
    if (style) n.style.cssText = style;
    if (text != null) n.textContent = text;
    return n;
  }

  function closeDialog(id) {
    var d = document.getElementById(id);
    if (d && d.parentNode) d.parentNode.removeChild(d);
  }

  // ---------- 主提示窗 ----------
  function showUpdateDialog(status) {
    if (document.getElementById('bt-update-dialog')) return;
    var pending = status.pending || {};
    var dark = document.documentElement.getAttribute('data-theme') === 'dark';
    var surface = dark ? '#1e222b' : '#fff';
    var fg = dark ? '#e8eaed' : '#1f2329';
    var muted = dark ? '#9aa0a6' : '#646a73';
    var border = dark ? '#3a404b' : '#e5e6eb';
    var accent = '#07c160';

    var mask = el('div',
      'position:fixed;inset:0;z-index:99999;background:rgba(0,0,0,.45);' +
      'display:flex;align-items:center;justify-content:center;padding:16px;box-sizing:border-box;');
    mask.id = 'bt-update-dialog';

    var box = el('div',
      'width:min(560px,96vw);max-height:82vh;overflow:auto;background:' + surface + ';' +
      'color:' + fg + ';border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.25);' +
      'font-family:system-ui,-apple-system,sans-serif;font-size:14px;line-height:1.6;');

    var head = el('div', 'display:flex;align-items:center;justify-content:space-between;' +
      'padding:18px 20px 8px;');
    head.appendChild(el('div', 'font-size:17px;font-weight:600;', '发现新版本'));
    var x = el('button', 'border:0;background:transparent;color:' + muted + ';font-size:20px;cursor:pointer;line-height:1;', '×');
    x.title = '关闭';
    x.onclick = function () { snooze(); closeDialog('bt-update-dialog'); };
    head.appendChild(x);
    box.appendChild(head);

    var meta = el('div', 'padding:0 20px 10px;color:' + muted + ';');
    var verLine = '当前 ' + (pending.from || status.version.short_sha || '?') +
      ' → 最新 ' + (pending.to || '?');
    if (pending.ahead_by != null) verLine += '　·　落后 ' + pending.ahead_by + ' 个提交';
    if (status.branch) verLine += '　·　分支 ' + status.branch;
    meta.appendChild(el('div', '', verLine));
    if (status.channel) meta.appendChild(el('div', 'font-size:12px;margin-top:2px;', '更新方式：' + status.channel));
    // 无 token 时更新内容来自 atom feed，明确告知来源更透明
    if (pending.detail_source) {
      var srcLabel = { 'compare-api': 'GitHub API', 'atom-feed': '提交订阅源', 'unavailable': '有限信息' }[pending.detail_source] || pending.detail_source;
      meta.appendChild(el('div', 'font-size:12px;margin-top:2px;', '更新详情来源：' + srcLabel));
    }
    box.appendChild(meta);

    // commit 列表（textContent 写入，防 XSS）
    var list = el('div', 'margin:0 20px 8px;border:1px solid ' + border + ';border-radius:8px;' +
      'max-height:300px;overflow:auto;');
    var commits = pending.commits || [];
    if (!commits.length) {
      list.appendChild(el('div', 'padding:12px;color:' + muted + ';', pending.changelog_md || '暂无更新详情'));
    } else {
      commits.forEach(function (c) {
        var row = el('div', 'padding:8px 12px;border-bottom:1px solid ' + border + ';');
        row.appendChild(el('span', 'display:inline-block;min-width:58px;color:' + muted +
          ';font-family:monospace;font-size:12px;vertical-align:top;', c.sha || ''));
        var body = el('div', 'display:inline-block;width:calc(100% - 70px);vertical-align:top;');
        body.appendChild(el('div', 'word-break:break-word;', c.msg || ''));
        body.appendChild(el('div', 'color:' + muted + ';font-size:12px;margin-top:2px;',
          (c.author || '') + (c.date ? ' · ' + c.date : '')));
        row.appendChild(body);
        list.appendChild(row);
      });
    }
    box.appendChild(list);

    if (pending.files_changed != null) {
      box.appendChild(el('div', 'padding:0 20px 8px;color:' + muted + ';font-size:12px;',
        '涉及 ' + pending.files_changed + ' 个文件'));
    }

    var foot = el('div', 'padding:8px 20px 20px;display:flex;gap:10px;justify-content:flex-end;');
    var later = el('button', 'padding:8px 16px;border:1px solid ' + border + ';background:transparent;' +
      'color:' + fg + ';border-radius:6px;cursor:pointer;', '稍后再说');
    later.onclick = function () { snooze(); closeDialog('bt-update-dialog'); };
    foot.appendChild(later);

    var go = el('button', 'padding:8px 18px;border:0;background:' + accent +
      ';color:#fff;border-radius:6px;cursor:pointer;font-weight:600;', '立即更新');
    go.onclick = function () { confirmAndApply(status, box, foot, accent, muted, fg, border); };
    foot.appendChild(go);
    box.appendChild(foot);

    mask.appendChild(box);
    mask.addEventListener('click', function (e) {
      if (e.target === mask) { snooze(); closeDialog('bt-update-dialog'); }
    });
    document.body.appendChild(mask);
  }

  // ---------- 二次确认 + 执行 ----------
  async function confirmAndApply(status, box, foot, accent, muted, fg, border) {
    foot.innerHTML = '';
    foot.appendChild(el('div', 'flex:1;color:' + muted + ';font-size:12px;',
      '更新前会自动备份，失败可回滚；更新后需重启服务生效。'));
    var cancel = el('button', 'padding:8px 16px;border:1px solid ' + border +
      ';background:transparent;color:' + fg + ';border-radius:6px;cursor:pointer;', '取消');
    cancel.onclick = function () { showUpdateDialog(status); closeDialog('bt-update-dialog'); };
    foot.appendChild(cancel);
    var yes = el('button', 'padding:8px 18px;border:0;background:' + accent +
      ';color:#fff;border-radius:6px;cursor:pointer;font-weight:600;', '确认更新');
    foot.appendChild(yes);

    yes.onclick = async function () {
      yes.disabled = true;
      yes.textContent = '更新中…';
      var tip = el('div', 'margin:0 20px 8px;color:' + muted + ';', '正在下载并应用更新，请稍候…');
      box.insertBefore(tip, foot);
      try {
        var res = await api('/api/update/apply', {
          method: 'POST', body: JSON.stringify({ channel: 'auto' })
        });
        if (res.status && res.data) {
          tip.textContent = '';
          tip.appendChild(el('div', 'color:' + accent + ';font-weight:600;', '✔ ' + (res.msg || '更新完成')));
          tip.appendChild(el('div', 'color:' + muted + ';font-size:12px;margin-top:4px;',
            res.data.from + ' → ' + res.data.to + '　·　方式：' + res.data.method));
          tip.appendChild(el('div', 'color:' + muted + ';font-size:12px;margin-top:4px;',
            '请重启服务（multi-agent.service）使新代码生效。'));
          foot.innerHTML = '';
          var done = el('button', 'padding:8px 18px;border:0;background:' + accent +
            ';color:#fff;border-radius:6px;cursor:pointer;', '我知道了');
          done.onclick = function () { closeDialog('bt-update-dialog'); };
          foot.appendChild(done);
        } else {
          tip.textContent = '✘ 更新失败：' + (res.msg || '未知错误');
          foot.innerHTML = '';
          var rb = el('button', 'padding:8px 16px;border:1px solid ' + border +
            ';background:transparent;color:' + fg + ';border-radius:6px;cursor:pointer;', '回滚');
          rb.onclick = async function () {
            var r = await api('/api/update/rollback', { method: 'POST', body: '{}' });
            tip.textContent = r.status ? '已回滚：' + (r.msg || '') : '回滚失败：' + (r.msg || '');
          };
          foot.appendChild(rb);
          var close = el('button', 'padding:8px 18px;border:0;background:' + muted +
            ';color:#fff;border-radius:6px;cursor:pointer;', '关闭');
          close.onclick = function () { closeDialog('bt-update-dialog'); };
          foot.appendChild(close);
        }
      } catch (e) {
        tip.textContent = '✘ 请求失败：' + e.message;
        yes.disabled = false;
        yes.textContent = '重试';
      }
    };
  }

  async function init() {
    if (dismissed()) return;
    try {
      // check 触发检测（只读，带节流；首次会自动生成 version.json 基线且不提示）
      var checked = await api('/api/update/check');
      if (!checked.status || !checked.data || !checked.data.has_update) return;
      // 有更新再读 status 拿 channel / branch / version 等展示信息
      var res = await api('/api/update/status');
      if (res.status && res.data && res.data.has_update && res.data.pending) {
        showUpdateDialog(res.data);
      }
    } catch (e) {
      console.warn('[update] 检测失败（已静默）', e);
    }
  }

  // 延迟启动，避让首屏渲染
  setTimeout(function () {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', init);
    } else {
      init();
    }
  }, 3000);

  // 暴露给控制台：window.btUpdate.check() 手动触发
  window.btUpdate = { check: init };
})();
