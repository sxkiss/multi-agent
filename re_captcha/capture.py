import asyncio, json, sys
from playwright.async_api import async_playwright

SDK = "https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"

HOOK_JS = r"""
window.__net = [];
function __push(o){ window.__net.push(o); }
// hook fetch
const _of = window.fetch;
window.fetch = function(url, init){
  const u = (typeof url==='string')?url:(url&&url.url)||'';
  const p = _of.apply(this, arguments);
  p.then(r=>{ try{ r.clone().text().then(t=>__push({kind:'fetch',url:u,method:(init&&init.method)||'GET',headers:(init&&init.headers)||{},body:(init&&init.body)?String(init.body).slice(0,6000):'',status:r.status,resp:t.slice(0,8000)})); }catch(e){} });
  return p;
};
// hook XHR
const _open = XMLHttpRequest.prototype.open, _send = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.open = function(m,u){ this.__m=m; this.__u=u; this.__h={}; return _open.apply(this,arguments); };
XMLHttpRequest.prototype.setRequestHeader = function(k,v){ this.__h[k]=v; return true; };
XMLHttpRequest.prototype.send = function(b){
  this.__b=b;
  this.addEventListener('load', ()=>{ try{ __push({kind:'xhr',url:this.__u,method:this.__m,headers:this.__h,body:this.__b?String(this.__b).slice(0,6000):'',status:this.status,resp:String(this.responseText||'').slice(0,8000)}); }catch(e){} });
  return _send.apply(this,arguments);
};
'ok'
"""

DRIVE_JS = r"""
async (verifyData) => {
  const log = [];
  window.__drvlog = log;
  const lg = (m)=>log.push(m);
  // load SDK
  if (typeof window.bdCaptcha === 'undefined') {
    await new Promise((res,rej)=>{
      const s=document.createElement('script'); s.src=arguments[1]||SDKURL;
      s.onload=res; s.onerror=(e)=>rej(new Error('sdk load fail')); document.head.appendChild(s);
    });
    lg('sdk loaded: '+typeof window.bdCaptcha);
  }
  // container
  let box=document.getElementById('__vc_box');
  if(!box){ box=document.createElement('div'); box.id='__vc_box'; document.body.appendChild(box); }
  let inst;
  try {
    inst = new window.bdCaptcha.CaptchaVerify({
      info: {aid:'582478', appName:'doubao', lang:'zh', did:'', fp:'', pageId:'27032'},
      ele: '__vc_box',
      host: 'https://verify.zijieapi.com',
      env: {h5_check_version:'4.0.16', product_host:'https://www.doubao.com', vc_version:'1.0.0.739'},
      successCb: (result)=>{ window.__success = result; lg('SUCCESS '+JSON.stringify(result).slice(0,3000)); },
      closeCb: ()=>{ lg('CLOSE'); },
      log: (d)=>{ lg('sdklog '+JSON.stringify(d).slice(0,500)); }
    });
    lg('inst created');
    if (inst.init) inst.init();
    lg('init called');
    inst.render(verifyData);
    lg('render called');
  } catch(e){ lg('ERR '+e.message); }
  return log;
}
"""

async def main():
    verify = sys.argv[1] if len(sys.argv)>1 else '{"type":"verify","subtype":"slide","verify_scene":"doubao_message_web","detail":""}'
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = await ctx.new_page()
        pg.on("console", lambda m: print("  [console]", m.text[:200]))
        await pg.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(3000)
        await pg.evaluate("(s)=>{window.SDKURL=s;}", SDK)
        await pg.evaluate(HOOK_JS)
        print("hooked. bdms present:", await pg.evaluate("()=>typeof window.bdms"))
        # inject driver as a real function
        js = "(async (verifyData, SDKURL) => {" + DRIVE_JS.split("async (verifyData) => {",1)[1]
        try:
            log = await pg.evaluate(js, [verify, SDK])
            print("DRIVER LOG:")
            for l in log: print("  -", l)
        except Exception as e:
            print("driver error:", e)
        await pg.wait_for_timeout(6000)
        net = await pg.evaluate("()=>window.__net||[]")
        open("net_%s.json"%("custom"),"w").write(json.dumps(net, ensure_ascii=False, indent=1))
        print("\n=== NETWORK (%d) ==="%len(net))
        for n in net:
            u=n['url']
            print(f"\n[{n['kind']}] {n['method']} {u}  -> {n.get('status')}")
            print("   headers:", json.dumps(n.get('headers'),ensure_ascii=False)[:500])
            if n.get('body'): print("   body:", n['body'][:800])
            if n.get('resp'): print("   resp:", n['resp'][:1200])
        succ = await pg.evaluate("()=>window.__success||null")
        print("\n=== SUCCESS RESULT ===", json.dumps(succ, ensure_ascii=False)[:3000] if succ else None)
        await pg.close()

asyncio.run(main())
