import asyncio, json, sys
from playwright.async_api import async_playwright

SDK = "https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"
VERIFY = open('real_verify.json', encoding='utf-8').read().strip()

HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#f5f5f5"><div id="c" style="width:360px;margin:40px auto"></div>
<script>window.__log=[];window.__success=null;window.__err=null;</script>
<script src="%s" crossorigin="anonymous" onerror="window.__err='sdkfail'"></script>
<script>
window.__boot=function(v){
 try{
  var inst=new window.bdCaptcha.CaptchaVerify({
    info:{aid:'582478',appName:'doubao',lang:'zh',did:'',fp:'',pageId:'27032'},
    ele:'c', host:'https://verify.zijieapi.com',
    env:{h5_check_version:'4.0.16',product_host:'https://www.doubao.com',vc_version:'1.0.0.739'},
    successCb:function(r){window.__success=r;window.__log.push('SUCCESS');},
    closeCb:function(){window.__log.push('CLOSE')},
    errorCb:function(e){window.__err=String(e&&e.message||e)},
    log:function(d){window.__log.push('sdklog:'+JSON.stringify(d).slice(0,400))}
  });
  if(inst.init)inst.init();
  inst.render(v);
  window.__log.push('render called');
 }catch(e){window.__err=e.stack||String(e)}
};
</script></body></html>""" % SDK

async def main():
    cap=[]
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = await ctx.new_page()
        async def on_req(r):
            if any(k in r.url for k in ['verify','captcha','i18n','feedback','zjs','rc-verify','bdms','monitor_web']):
                cap.append({"d":"REQ","m":r.method,"u":r.url,"h":dict(r.headers),"b":(r.post_data or "")[:8000]})
        async def on_resp(r):
            if any(k in r.url for k in ['verify','captcha','i18n','feedback','zjs','rc-verify','bdms','monitor_web']):
                try: t=await r.text()
                except Exception: t=""
                cap.append({"d":"RESP","s":r.status,"u":r.url,"b":t[:14000]})
        pg.on("request", lambda r: asyncio.ensure_future(on_req(r)))
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        await pg.set_content(HTML, wait_until="load")
        await pg.wait_for_timeout(4000)
        await pg.evaluate("(v)=>window.__boot(v)", VERIFY)
        await pg.wait_for_timeout(10000)
        out={"cap":cap,
             "log":await pg.evaluate("()=>window.__log"),
             "succ":await pg.evaluate("()=>{try{return JSON.stringify(window.__success)}catch(e){return null}}"),
             "err":await pg.evaluate("()=>window.__err?String(window.__err):null")}
        open("full_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        await pg.screenshot(path="full_state.png")
        await pg.close()

asyncio.run(main())
