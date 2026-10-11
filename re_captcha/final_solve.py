import asyncio, json, urllib.parse, base64, io, sys
import numpy as np, cv2
from PIL import Image
from playwright.async_api import async_playwright

SDK = "https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"
IDS = json.load(open('ids.json'))
VD  = json.load(open('real_verify.json'))

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
    successCb:function(r){window.__success=r;window.__log.push('SUCCESS:'+JSON.stringify(r).slice(0,5000));},
    closeCb:function(){window.__log.push('CLOSE')},
    errorCb:function(e){window.__err=String(e&&e.message||e)},
    log:function(d){window.__log.push('sdklog:'+JSON.stringify(d).slice(0,300))}
  });
  if(inst.init)inst.init();
  inst.render(v);
  window.__inst=inst; window.__log.push('render called');
 }catch(e){window.__err=e.stack||String(e)}
};
</script></body></html>""" % SDK

def fetch_img(pg_url):
    import requests
    r=requests.get(pg_url, timeout=30)
    return r.content

def detect(bg_bytes, piece_bytes):
    bg=Image.open(io.BytesIO(bg_bytes)).convert('RGB')
    pc=Image.open(io.BytesIO(piece_bytes)).convert('RGBA')
    bgn=np.array(bg); pn=np.array(pc)
    mask=(pn[:,:,3]>30).astype(np.uint8)*255
    gray_bg=cv2.cvtColor(bgn,cv2.COLOR_RGB2GRAY)
    gray_pc=cv2.cvtColor(pn[:,:,:3],cv2.COLOR_RGBA2GRAY)
    res=cv2.matchTemplate(gray_bg, gray_pc, cv2.TM_CCOEFF_NORMED, mask=mask)
    _,mx,_,loc=cv2.minMaxLoc(res)
    return int(loc[0]), float(mx), bg.size, pc.size

async def main():
    cap=[]
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = await ctx.new_page()
        async def on_req(r):
            if any(k in r.url for k in ['verify.zijieapi','captcha','i18n','feedback']):
                cap.append({"d":"REQ","m":r.method,"u":r.url,"h":dict(r.headers),"b":(r.post_data or "")[:12000]})
        async def on_resp(r):
            if any(k in r.url for k in ['verify.zijieapi','captcha','i18n','feedback']):
                try: t=await r.text()
                except Exception: t=""
                cap.append({"d":"RESP","s":r.status,"u":r.url,"h":dict(r.headers),"b":t[:20000]})
        pg.on("request", lambda r: asyncio.ensure_future(on_req(r)))
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        await pg.set_content(HTML, wait_until="load")
        await pg.wait_for_timeout(4000)
        await pg.evaluate("([v,ids])=>window.__boot(v,ids)", [json.dumps(VD,ensure_ascii=False), IDS])
        await pg.wait_for_timeout(9000)

        # find the /captcha/get response to grab image urls
        urls=None
        for c in cap:
            if 'captcha/get' in c['u'] and c['d']=='RESP':
                try: urls=json.loads(c['b'])
                except Exception: pass
        if not urls or not urls.get('data'):
            print("NO CAPTCHA GET DATA. caps:", [c['u'][:80] for c in cap])
            out={"cap":cap,"log":await pg.evaluate("()=>window.__log"),
                 "succ":await pg.evaluate("()=>{try{return JSON.stringify(window.__success)}catch(e){return null}}"),
                 "err":await pg.evaluate("()=>window.__err?String(window.__err):null")}
            open("final_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
            await pg.close(); return
        q=urls['data']['question']
        print("challenge_code:", urls['data'].get('challenge_code'), "id:", urls['data'].get('id'))
        print("url1:", q.get('url1','')[:120]); print("url2:", q.get('url2','')[:120])

        bb=fetch_img(q['url1']); pb=fetch_img(q['url2'])
        x,score,bgsz,pcsz=detect(bb,pb)
        print("detected x(nat)=",x,"score=",round(score,3),"bg=",bgsz,"piece=",pcsz)

        geom=await pg.evaluate("""()=>{
          const bg=document.querySelector('#captcha_verify_image');
          const pc=document.querySelector('#captcha_verify_img_slide')||document.querySelector('img.captcha-verify-image-slide');
          const btn=document.querySelector('.captcha-slider-btn')||document.querySelector('.captcha_verify_slide--button .dragger-box')||document.querySelector('.captcha_verify_slide--button');
          const r=btn?btn.getBoundingClientRect():null;
          return {bgDisp: bg?bg.getBoundingClientRect().width:0, bgNat: bg?bg.naturalWidth:0,
                  pcDisp: pc?pc.getBoundingClientRect().width:0,
                  btn: r?{x:r.x,y:r.y,w:r.width,h:r.height}:null};
        }""")
        print("geom:", json.dumps(geom))
        if not geom.get('btn'):
            print("NO DRAG BUTTON"); out={"cap":cap,"geom":geom}
            open("final_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1)); await pg.close(); return

        scale = geom['bgDisp']/geom['bgNat'] if geom['bgNat'] else 1.0
        dist = x*scale
        print("drag distance(px) =", round(dist,1), "scale", round(scale,3))
        bx=geom['btn']['x']+geom['btn']['w']/2; by=geom['btn']['y']+geom['btn']['h']/2
        await pg.mouse.move(bx,by); await pg.mouse.down()
        steps=28
        for i in range(1,steps+1):
            t=i/steps
            e=(1-(1-t)**3)
            await pg.mouse.move(bx+dist*e, by+ (0 if i<steps-3 else 1), steps=1)
            await asyncio.sleep(0.035)
        await asyncio.sleep(0.25)
        await pg.mouse.up()
        await pg.wait_for_timeout(6000)

        out={"cap":cap,
             "geom":geom,"dist":dist,"score":score,
             "log":await pg.evaluate("()=>window.__log"),
             "succ":await pg.evaluate("()=>{try{return JSON.stringify(window.__success)}catch(e){return null}}"),
             "err":await pg.evaluate("()=>window.__err?String(window.__err):null")}
        open("final_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        print("SAVED final_out.json")
        await pg.screenshot(path="final_state.png", full_page=True)
        await pg.close()

asyncio.run(main())
