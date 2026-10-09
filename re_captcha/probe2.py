import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctxs = b.contexts
        target=None
        for c in ctxs:
            for pg in c.pages:
                if pg.url == "https://www.doubao.com/chat":
                    target=(c,pg); break
            if target: break
        c,pg = target
        # 全局 hook：在页面上下文注入 fetch/XHR 拦截（记录到 window.__net），
        # 并用 MutationObserver 监听 captcha iframe/节点出现
        await pg.evaluate("""() => {
          if (window.__hooked) return;
          window.__hooked = true;
          window.__net = [];
          const rec = (kind, url, init, respStatus, respBody) => {
            if (url.includes('captcha') || url.includes('verify') || url.includes('frontier') || url.includes('bdms') || url.includes('rc-verify')) {
              window.__net.push({t: Date.now(), kind, url, method: init?.method||'GET', headers: init&&init.headers?JSON.stringify(init.headers):'', body: init&&init.body?String(init.body).slice(0,4000):'', status: respStatus, resp: respBody?String(respBody).slice(0,4000):''});
            }
          };
          const of = window.fetch;
          window.fetch = function(url, init){
            const p = of.apply(this, arguments).then(r => {
              const ct = r.headers.get('content-type')||'';
              if (ct.includes('json') || ct.includes('text')) {
                r.clone().text().then(t=>hook('fetch', url, init, r.status, t)).catch(()=>{});
              } else { hook('fetch', url, init, r.status, ''); }
              return r;
            });
            return p;
          };
          const ox = XMLHttpRequest.prototype.open;
          const os = XMLHttpRequest.prototype.send;
          XMLHttpRequest.prototype.open = function(m,u){ this.__u=u; this.__m=m; return ox.apply(this, arguments); };
          XMLHttpRequest.prototype.send = function(body){
            this.__b = body;
            this.addEventListener('load', ()=>{ hook('xhr', this.__u, {method:this.__m, body:this.__b}, this.status, this.responseText); });
            return os.apply(this, arguments);
          };
          // MutationObserver 监听 captcha 节点出现
          window.__muts = [];
          const mo = new MutationObserver(muts=>{
            for (const m of muts) {
              for (const n of m.addedNodes) {
                if (n.nodeType===1) {
                  const s = (n.id||'') + ' ' + (n.className||'') + ' ' + (n.tagName||'');
                  if (/captcha|verify|vc-|slide/i.test(s)) {
                    window.__muts.push({t:Date.now(), node: s.slice(0,200), src: n.src||'', href: n.href||''});
                  }
                  if (n.tagName==='IFRAME') {
                    try { n.addEventListener('load', ()=>{ window.__muts.push({t:Date.now(), iframeLoaded: n.src}); }); } catch(e){}
                  }
                }
              }
            }
          });
          mo.observe(document, {childList:true, subtree:true});
          return 'hook installed';
        }""")
        print("hook installed")
        # 2) 尝试点开输入框并发送一条消息，触发风控（如果账号被风控）
        # 先在 chat 页找一个输入框
        info = await pg.evaluate("""() => {
          const out = {inputs: []};
          document.querySelectorAll('textarea, [contenteditable=true], input[type=text]').forEach((el,i)=>{
            if (i<6) out.inputs.push({tag: el.tagName, ce: el.getAttribute('contenteditable'), ph: el.getAttribute('placeholder')||'', cls: (el.className||'').toString().slice(0,80)});
          });
          return out;
        }""")
        print("inputs:", json.dumps(info, ensure_ascii=False))
        # 等一段时间收集网络
        await asyncio.sleep(3)
        net = await pg.evaluate("()=>JSON.stringify(window.__net||[], null, 1)")
        print("=== captured so far:", net)

asyncio.run(main())
