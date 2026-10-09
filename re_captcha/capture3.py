import asyncio, json, sys
from playwright.async_api import async_playwright

SDK = "https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"

HOOK_JS = r"""
window.__net = [];
function __push(o){ window.__net.push(o); }
const _of = window.fetch;
window.fetch = function(url, init){
  const u = (typeof url==='string')?url:(url&&url.url)||'';
  const p = _of.apply(this, arguments);
  p.then(r=>{ try{ r.clone().text().then(t=>__push({kind:'fetch',url:u,method:(init&&init.method)||'GET',body:(init&&init.body)?String(init.body).slice(0,8000):'',status:r.status,resp:t.slice(0,12000)})); }catch(e){} });
  return p;
};
const _x=XMLHttpRequest.prototype;
const _open=_x.open,_send=_x.send;
XMLHttpRequest.prototype.open=function(m,u){this.__m=m;this.__u=u;return _open.apply(this,arguments)};
XMLHttpRequest.prototype.send=function(b){
  this.__b=b;
  this.addEventListener('load',()=>{try{__push({kind:'xhr',url:this.__u,method:this.__m,body:this.__b?String(this.__b).slice(0,8000):'',status:this.status,resp:String(this.responseText||'').slice(0,12000)})}catch(e){}});
  return _send.apply(this,arguments);
};
function __push(o){ window.__x.push(o); }
'ok'
"""

DRIVE_JS = """
async (verifyData, sdkUrl) => {
  const log = [];
  if (typeof window.bdCaptcha === 'undefined') {
    const r = await new Promise((res,rej)=>{
      const s=document.createElement('script');
      s.src=sdkUrl; s.crossOrigin='anonymous';
      s.onload=()=>res('ok'); s.onerror=(e)=>rej(new Error('loadfail'));
      document.head.appendChild(s);
    });
    log.push('script load: '+r);
  }
  log.push('bdCaptcha: '+(typeof window.bdCaptcha)+' keys='+JSON.stringify(window.bdCaptcha?Object.keys(window.bdCaptcha):[]));
  let box=document.getElementById('__vc_box');
  if(!box){ box=document.createElement('div'); box.id='__vc_box'; box.style.cssText='position:fixed;top:20px;left:20px;z-index:99999;background:#fff;'; document.body.appendChild(box); }
  window.__success=undefined; window.__err=undefined;
  try {
    const inst = new window.bdCaptcha.CaptchaVerify({
      info: {aid:'582478', appName:'doubao', lang:'zh', did:'', fp:'', pageId:'27032'},
      ele: '__vc_box',
      host: 'https://verify.zijieapi.com',
      env: {h5_check_version:'4.0.16', product_host:'https://www.doubao.com', vc_version:'1.0.0.739'},
      successCb: (result)=>{ window.__success=result; log.push('SUCCESS: '+JSON.stringify(result).slice(0,4000)); },
      closeCb: ()=>{ log.push('CLOSE'); },
      errorCb: (e)=>{ window.__err=e&&e.message?e.message:JSON.stringify(e); log.push('ERRCB: '+window.__err); },
      log: (d)=>{ log.push('sdklog: '+JSON.stringify(d).slice(0,300)); }
    });
    log.push('inst ok');
    if (inst.init) { inst.init(); log.push('init ok'); }
    inst.render(verifyData);
    log.push('render ok');
  } catch(e){ window.__err=e.stack||String(e); log.push('EXC: '+window.__err.slice(0,800)); }
  return log;
}
"""

async def main():
    verify = sys.argv[1] if len(sys.argv)>1 else '{"type":"verify","subtype":"slide","verify_scene":"doubao_message_web","detail":""}'
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = await ctx.new_page()
        cdp = await ctx.new_cdp_session(pg)
        await cdp.send("Page.enable")
        await cdp.send("Page.setBypassCSP", {"enabled": True})
        await pg.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(2000)
        await pg.evaluate(HOOK_JS)
        log = await pg.evaluate(DRIVE_JS, [verify, SDK])
        print("DRIVER LOG:")
        for l in log: print("  -", l)
        await pg.wait_for_timeout(9000)
        net = await pg.evaluate("()=>window.__net||[]")
        rel = [n for n in net if any(k in n['url'] for k in ['verify','captcha','frontier','bdms','i18n','feedback','zjs','rc-verify'])]
        open("net3.json","w").write(json.dumps(rel, ensure_ascii=False, indent=1))
        print("\n=== RELATED NETWORK (%d) ==="%len(rel))
        for n in rel:
            print(f"\n[{n['type']}] {n['method']} {n['url']}  -> {n['status']}")
            if n.get('body'): print("   body:", n['body'][:1200])
            if n.get('resp'): print("   resp:", n['resp'][:2500])
        succ = await pg.evaluate("()=>{ const s=window.__success; return s?JSON.stringify(s):null }")
        err = await pg.evaluate("()=>window.__err?String(window.__err):null")
        print("\n=== SUCCESS ===", (succ or '')[:3000])
        print("=== ERR ===", (err or '')[:800])
        await pg.screenshot(path="captcha_state2.png")
        await pg.close()

asyncio.run(main())
