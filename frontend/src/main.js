import { createApp } from 'vue'
import { marked } from 'marked'
import App from './App.vue'
import './styles.css'
import { authOpts } from './auth.js'

// ==================== 面板全局对象（原版内联于 index.html，现随源码编译） ====================
window.ai_tools = {
  send: function (opts, cb, opts2) {
    opts2 = opts2 || {}
    const url = opts.url
    const data = opts.data || null
    const isPost = !!data
    const xhr = new XMLHttpRequest()
    xhr.open(isPost ? 'POST' : 'GET', url, true)
    xhr.onreadystatechange = function () {
      if (xhr.readyState === 4) {
        let r
        try {
          r = JSON.parse(xhr.responseText)
        } catch (e) {
          r = { status: false, msg: '响应解析失败' }
        }
        if (cb) cb(r)
      }
    }
    if (isPost) {
      xhr.setRequestHeader('Content-Type', 'application/json')
      xhr.send(JSON.stringify(data))
    } else {
      xhr.send()
    }
  }
}
window.layer = {
  open: function (opts) {
    const id = 'layer_' + Date.now()
    return {
      id: id,
      close: function () {
        if (opts && typeof opts.end === 'function') opts.end()
      }
    }
  },
  msg: function (msg, opts) { console.log('[layer] ' + msg) },
  close: function (id) {},
  closeAll: function () {},
  alert: function (msg, opts) { console.log('[layer] alert: ' + msg) },
  // 独立部署兜底：用原生 confirm 实现确认框（面板宿主会覆盖此实现）
  confirm: function (opts, cb) {
    const msg = typeof opts === 'string' ? opts : (opts && opts.msg) || '确定执行该操作吗?'
    const ok = window.confirm(msg)
    if (cb) cb(ok)
  }
}
// 同时给 ai_tools 提供 confirm，保持调用方（window.ai_tools?.confirm）可用
window.ai_tools.confirm = window.layer.confirm

// ---- 独立部署专属：服务器文件选择器（面板宿主提供 aiApp.select_path，此处兜底） ----
window.aiApp = window.aiApp || {}
if (typeof window.aiApp.select_path !== 'function') {
  window.aiApp.select_path = function (id, type, cb, startPath) {
    openFileBrowser(startPath || '', cb)
  }
}
if (typeof window.aiApp.simple_confirm !== 'function') {
  window.aiApp.simple_confirm = window.layer.confirm
}

// ==================== 文件浏览器（调用 /api/files/browse） ====================
function openFileBrowser(startPath, cb) {
  const overlay = document.createElement('div')
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:1000000;display:flex;align-items:center;justify-content:center;'
  const box = document.createElement('div')
  box.style.cssText = 'width:min(640px,92vw);max-height:80vh;background:#fff;border-radius:10px;display:flex;flex-direction:column;overflow:hidden;font-family:system-ui,sans-serif;'
  box.innerHTML = `
    <div style="padding:12px 16px;border-bottom:1px solid #eee;font-weight:600;display:flex;justify-content:space-between;align-items:center;">
      <span>选择服务器文件</span>
      <span style="cursor:pointer;color:#999;" class="fb-close">✕</span>
    </div>
    <div class="fb-path" style="padding:8px 16px;background:#f7f7f9;font-size:12px;color:#555;word-break:break-all;"></div>
    <div class="fb-list" style="flex:1;overflow:auto;padding:6px 8px;min-height:200px;"></div>
    <div style="padding:10px 16px;border-top:1px solid #eee;display:flex;justify-content:flex-end;gap:8px;">
      <button class="fb-cancel" style="padding:6px 14px;border:1px solid #ccc;background:#fff;border-radius:6px;cursor:pointer;">取消</button>
      <button class="fb-ok" style="padding:6px 14px;border:none;background:#07c160;color:#fff;border-radius:6px;cursor:pointer;">选择此文件</button>
    </div>`
  overlay.appendChild(box)
  document.body.appendChild(overlay)

  const pathEl = box.querySelector('.fb-path')
  const listEl = box.querySelector('.fb-list')
  let currentPath = ''
  let selectedFile = ''

  function render(data) {
    currentPath = data.path
    pathEl.textContent = data.path || '/'
    listEl.innerHTML = ''
    if (data.parent) {
      const up = document.createElement('div')
      up.style.cssText = 'padding:8px 10px;cursor:pointer;border-radius:6px;color:#07c160;'
      up.textContent = '📁 .. (上级目录)'
      up.onclick = () => load(data.parent)
      listEl.appendChild(up)
    }
    ;(data.items || []).forEach(it => {
      const row = document.createElement('div')
      row.style.cssText = 'padding:8px 10px;cursor:pointer;border-radius:6px;display:flex;gap:8px;align-items:center;'
      const icon = it.type === 'dir' ? '📁' : '📄'
      const size = it.type === 'file' ? ` (${formatSize(it.size)})` : ''
      const nameEl = document.createElement('span')
      nameEl.textContent = icon + ' ' + it.name + size
      row.appendChild(nameEl)
      row.onmouseenter = () => row.style.background = '#f0f7ff'
      row.onmouseleave = () => row.style.background = it.name === selectedFile ? '#e6f7ff' : ''
      if (it.type === 'dir') {
        row.onclick = () => load(currentPath.replace(/\/$/, '') + '/' + it.name)
      } else {
        row.onclick = () => {
          selectedFile = it.name
          ;[...listEl.children].forEach(c => c.style.background = '')
          row.style.background = '#e6f7ff'
        }
        if (data.preselect && it.name === data.preselect) {
          selectedFile = it.name
          row.style.background = '#e6f7ff'
        }
      }
      listEl.appendChild(row)
    })
  }

  function load(p) {
    selectedFile = ''
    fetch('/api/files/browse?path=' + encodeURIComponent(p), authOpts())
      .then(r => r.json())
      .then(r => { if (r.status) render(r.data); else alert(r.msg || '浏览失败') })
      .catch(e => alert('浏览失败: ' + e))
  }

  function close() { document.body.removeChild(overlay) }
  function pick() {
    if (!selectedFile) { alert('请先选择一个文件'); return }
    const full = currentPath.replace(/\/$/, '') + '/' + selectedFile
    close()
    if (cb) cb(full)
  }

  box.querySelector('.fb-close').onclick = close
  box.querySelector('.fb-cancel').onclick = close
  box.querySelector('.fb-ok').onclick = pick
  overlay.onclick = (e) => { if (e.target === overlay) close() }

  load(startPath || '')
}

function formatSize(n) {
  if (n < 1024) return n + 'B'
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + 'KB'
  return (n / 1024 / 1024).toFixed(1) + 'MB'
}

// Expose marked globally for components that call window.marked
window.marked = marked

// Set default marked options
marked.setOptions({
  breaks: true,
  gfm: true
})

createApp(App).mount('#app')
