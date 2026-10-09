import asyncio, json, urllib.parse
from playwright.async_api import async_playwright

SDK = "https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"
VERIFY = open('real_verify.json', encoding='utf-8').read().strip()
IDS = json.load(open('ids.json'))

HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="background:#f5f5f5"><div id="c" style="width:360px;margin:40px auto"></div>
<script>window.__log=[];window.__success=null;window.__err=null;</script>
<script src="%s" crossorigin="anonymous" onerror="window.__err='sdkfail'"></script>
<script>
window.__boot=function(v,IDS){
 try{
  var inst=new window.bdCaptcha.CaptchaVerify({
    info:{aid:'582478',appName:'doubao',lang:'zh',did:IDS.did,fp:IDS.fp,pageId:'27032'},
    ele:'c', host:'https://verify.zijieapi.com',
    env:{h5_check_version:'4.0.16',product_host:'https://www.doubao.com',vc_version:'1.0.0.739'},
    successCb:function(r){window.__success=r;window.__log.push('SUCCESS:'+JSON.stringify(r).slice(0,4000));},
    closeCb:function(){window.__log.push('CLOSE')},
    errorCb:function(e){window.__err=String(e&&e.message||e)},
    log:function(d){window.__log.push('sdklog:'+JSON.stringify(d).slice(0,400))}
  });
  if(inst.init)inst.init();
  inst.render(d);
  window.__inst=inst;
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
            if any(k in r.url for k in ['verify.zijieapi','captcha','i18n','feedback','zjs']):
                cap.append({"d":"REQ","m":r.method,"u":r.url,"h":dict(r.headers),"b":(r.post_data or "")[:10000]})
        async def on_resp(r):
            if any(k in r.url for k in ['verify.zijieapi','captcha','i18n','feedback','zjs']):
                try: t=await r.text()
                except Exception: t=""
                cap.append({"d":"RESP","s":r.status,"u":r.url,"h":dict(r.headers),"b":t[:20000]})
        pg.on("request", lambda r: asyncio.ensure_future(on_req(r)))
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        await pg.set_content(HTML, wait_until="load")
        await pg.wait_for_timeout(4000)
        await pg.evaluate("([v,ids])=>window.__boot(v,ids)", [VERIFY, IDS])
        await pg.wait_for_timeout(8000)
        # 获取图片并识别缺口
        info = await pg.evaluate("""()=>{
          const c=document.getElementById('c');
          const bg=document.querySelector('#captcha_verify_image')||document.querySelector('img.captcha-verify-image');
          const piece=document.querySelector('img.captcha-verify-image-slide');
          const dragBtn=document.querySelector('.captcha-slider-btn, .vc-captcha-verify-slide--btn, [class*=slider] .dragger-box, .captcha_verify_slide--button');
          const out={bg: bg?bg.src:'', piece: piece?piece.src:'', bgW:bg&&bg.width, bgH:bg&&bg.height, pieceW:piece&&piece.width};
          out.all=Array.from(c.querySelectorAll('*')).map(e=>({t:e.tagName,c:(e.className||'').toString().slice(0,90),id:e.id||''})).filter(x=>/slider|dragger|button|btn|bar/i.test(x.c));
          return out;
        }""")
        json.dump(info,open('slide_dom.json','w'),ensure_ascii=False,indent=1)
        print("BG:", (info.get('bg') or '')[:200])
        print("PIECE:", (info.get('piece') or '')[:200])
        print("SIZE:", info.get('bgW'), info.get('bgH'), info.get('pieceW'))
        print("ELS:", json.dumps(info.get('out'),ensure_ascii=False)[:2000])
        await pg.screenshot(path="slide_state.png", full_page=True)
        out={"cap":cap,"log":await pg.evaluate("()=>window.__log"),
             "succ":await pg.evaluate("()=>{try{return JSON.stringify(window.__success)}catch(e){return null}}"),
             "err":await pg.evaluate("()=>window.__err?String(window.__err):null")}
        open("solve2_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        print("SAVED solve2_out.json; caps:", len(cap))
        await pg.close()

asyncio.run(main())
