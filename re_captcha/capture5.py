import asyncio, json, sys
from playwright.async_api import async_playwright

SDK = "https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"
VERIFY = sys.argv[1] if len(sys.argv)>1 else '{"type":"verify","subtype":"slide","verify_scene":"doubao_message_web","detail":""}'

HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body><div id="c"></div>
<script>
window.__log=[]; window.__success=null; window.__err=null;
window.__hookready=false;
</script>
<script src="%s" crossorigin="anonymous" onerror="window.__err='sdk-load-fail'"></script>
<script>
window.__log.push('bdCaptcha type: '+typeof window.bdCaptcha);
window.__boot = function(verifyData){
  try {
    var inst = new window.bdCaptcha.CaptchaVerify({
      info:{aid:'582478',appName:'doubao',lang:'zh',did:'',fp:'',pageId:'27032'},
      ele:'c',
      host:'https://verify.zijieapi.com',
      env:{h5_check_version:'4.0.16',product_host:'https://www.doubao.com',vc_version:'1.0.0.739'},
      successCb:function(result){ window.__success=result; window.__log.push('SUCCESS'); },
      closeCb:function(){ window.__log.push('CLOSE'); },
      errorCb:function(e){ window.__err=String(e&&e.message||e); },
      log:function(d){ window.__log.push('sdklog '+JSON.stringify(d).slice(0,300)); }
    });
    if(inst.init) inst.init();
    inst.render(verifyData);
    window.__log.push('render called');
  } catch(e){ window.__err = e.stack||String(e); }
};
</script></body></html>""" % SDK

async def main():
    captured=[]
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = await ctx.new_page()
        pg.on("console", lambda m: captured.append({"console": m.type, "text": m.text[:300]}))
        async def on_req(req):
            if any(k in req.url for k in ['verify','captcha','i18n','feedback','zjs','rc-verify','bdms','frontier']):
                captured.append({"dir":"REQ","method":req.method,"url":req.url,"headers":dict(req.headers),"body":(req.post_data or "")[:6000]})
        async def on_resp(resp):
            if any(k in resp.url for k in ['verify','captcha','i18n','feedback','zjs','rc-verify','bdms','frontier']):
                try: body=await resp.text()
                except Exception: body=""
                captured.append({"dir":"RESP","status":resp.status,"url":resp.url,"headers":dict(resp.headers),"body":body[:12000]})
        pg.on("request", lambda r: asyncio.ensure_future(on_req(r)))
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        await pg.set_content(HTML, wait_until="load")
        await pg.wait_for_timeout(4000)
        log = await pg.evaluate("()=>window.__log")
        bt  = await pg.evaluate("()=>typeof window.bdCaptcha")
        print("bdCaptcha:", bt, "log:", log)
        if bt == 'object':
            await pg.evaluate("(v)=>window.__boot(v)", VERIFY)
            await pg.wait_for_timeout(10000)
            print("log2:", await pg.evaluate("()=>window.__log"))
            succ = await pg.evaluate("()=>{try{return JSON.stringify(window.__success)}catch(e){return null}}")
            err  = await pg.evaluate("()=>window.__err?String(window.__err):null")
            print("SUCCESS:", (succ or '')[:3000])
            print("ERR:", (err or '')[:1000])
        out={"captured":captured,"log":log}
        open("capture5_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        print("\n=== CAPTURED ===")
        for c in captured:
            if "console" in c: continue
            print(f"\n[{c['dir']}] {c.get('method','')} {c['url']} status={c.get('status','')}")
            if c.get('body'): print("   body:", c['body'][:1500])
            if c.get('dir')=='RESP' and c.get('body'): print("   resp:", c['body'][:1500])
        print("\n=== CONSOLE/LOGS ===")
        for c in captured:
            if "console" in c: print("  ", c['text'])
        await pg.close()

asyncio.run(main())
