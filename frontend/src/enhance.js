/* ============================================================
 * enhance.js — 前端功能增强（2026-10-15）
 * 非侵入式：不改动 Vue 组件逻辑，通过 DOM 增强提供：
 *   1. 深浅色主题切换（localStorage 持久化）
 *   2. 对话历史搜索（在侧边栏注入搜索框，过滤历史项）
 *   3. 键盘快捷键（Ctrl/Cmd+K 聚焦输入，Esc 关闭弹层，Ctrl/Cmd+/ 显示快捷键）
 *   4. 新消息到达自动滚底（辅助）
 * ============================================================ */

(function () {
  'use strict';

  const THEME_KEY = 'bt-theme';

  // ---------- 1. 主题 ----------
  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) {}
    const btn = document.getElementById('bt-theme-toggle');
    if (btn) btn.textContent = theme === 'dark' ? '☀️' : '🌙';
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', theme === 'dark' ? '#0f1115' : '#07c160');
  }

  function initTheme() {
    let saved = 'light';
    try { saved = localStorage.getItem(THEME_KEY) || 'light'; } catch (e) {}
    applyTheme(saved);
    if (!document.getElementById('bt-theme-toggle')) {
      const btn = document.createElement('button');
      btn.id = 'bt-theme-toggle';
      btn.title = '切换深浅色主题';
      btn.textContent = saved === 'dark' ? '☀️' : '🌙';
      btn.addEventListener('click', () => {
        const cur = document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
        applyTheme(cur === 'dark' ? 'light' : 'dark');
      });
      document.body.appendChild(btn);
    }
  }

  // ---------- 2. 对话历史搜索 ----------
  function injectConversationSearch() {
    const sidebar = document.querySelector('.chat-sidebar');
    if (!sidebar || document.getElementById('bt-conv-search')) return;
    const list = sidebar.querySelector('.history-list') || sidebar;
    const input = document.createElement('input');
    input.id = 'bt-conv-search';
    input.type = 'text';
    input.placeholder = '🔍 搜索对话…';
    input.setAttribute('autocomplete', 'off');
    // 插到历史列表头部之后
    const header = list.querySelector('.history-list-header');
    if (header && header.parentNode) header.parentNode.insertBefore(input, header.nextSibling);
    else list.insertBefore(input, list.firstChild);

    input.addEventListener('input', () => {
      const q = input.value.trim().toLowerCase();
      // 兼容多种历史项类名
      const items = list.querySelectorAll('.history-item, .conversation-item, .folder-group, .folder-group-item');
      items.forEach((el) => {
        const text = (el.textContent || '').toLowerCase();
        el.style.display = (!q || text.includes(q)) ? '' : 'none';
      });
    });
    // 阻止快捷键冒泡（在搜索框内按 Esc 清空）
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { input.value = ''; input.dispatchEvent(new Event('input')); input.blur(); }
      e.stopPropagation();
    });
  }

  // ---------- 3. 快捷键 ----------
  function showHint(text) {
    let hint = document.getElementById('bt-shortcut-hint');
    if (!hint) {
      hint = document.createElement('div');
      hint.id = 'bt-shortcut-hint';
      document.body.appendChild(hint);
    }
    hint.textContent = text;
    hint.classList.add('show');
    clearTimeout(hint._t);
    hint._t = setTimeout(() => hint.classList.remove('show'), 1800);
  }

  function initShortcuts() {
    document.addEventListener('keydown', (e) => {
      const mod = e.ctrlKey || e.metaKey;
      // Ctrl/Cmd+K：聚焦输入框
      if (mod && e.key.toLowerCase() === 'k') {
        const ta = document.querySelector('.input-textarea, textarea');
        if (ta) { e.preventDefault(); ta.focus(); showHint('已聚焦输入框'); }
      }
      // Ctrl/Cmd+/：显示快捷键
      if (mod && e.key === '/') {
        e.preventDefault();
        showHint('Ctrl+K 输入 · Ctrl+Enter 发送 · Esc 关闭弹层 · Ctrl+/ 帮助');
      }
      // Esc：关闭弹层
      if (e.key === 'Escape') {
        document.querySelectorAll('.custom-popover, .history-panel.show').forEach((el) => {
          el.style.display = 'none';
          el.classList.remove('show');
        });
      }
    });
  }

  // ---------- 4. 新消息自动滚底 ----------
  function initAutoScroll() {
    const messages = document.querySelector('.chat-messages');
    if (!messages) return;
    let stick = true;
    messages.addEventListener('scroll', () => {
      const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
      stick = nearBottom;
    });
    const obs = new MutationObserver(() => {
      if (stick) messages.scrollTop = messages.scrollHeight;
    });
    obs.observe(messages, { childList: true, subtree: true, characterData: true });
  }

  // ---------- 启动 ----------
  function boot() {
    initTheme();
    initShortcuts();
    // 侧边栏 / 消息区可能是异步渲染，轮询注入
    let tries = 0;
    const timer = setInterval(() => {
      injectConversationSearch();
      initAutoScroll();
      if ((document.getElementById('bt-conv-search')) || ++tries > 20) clearInterval(timer);
    }, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

  // 暴露给控制台调试
  window.btTheme = { apply: applyTheme };
})();
