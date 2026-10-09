import asyncio, json, io, time, random
import numpy as np, cv2, requests
from PIL import Image
from playwright.async_api import async_playwright

SDK="https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js"
IDS=json.load(open('ids.json')); VD=json.load(open('real_verify.json'))
HTML="""<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="background:#f5f5f5">
<div id="c" style="width:360px;margin:40px auto"></div>
<script>window.__log=[];window.__success=null;window.__err=null;</script>
<script src="%s" crossorigin="anonymous"></script>
<script>window.__boot=function(v,IDS){try{var inst=new window.bdCaptcha.CaptchaVerify({
 info:{aid:'582478',appName:'doubao',lang:'zh',did:IDS.did,fp:IDS.fp,pageId:'27032'},ele:'c',
 host:'https://verify.zijieapi.com',
 env:{h5_check_version:'4.0.16',product_host:'https://www.doubao.com',vc_version:'1.0.0.739'},
 successCb:function(r){window.__success=r;window.__log.push('SUCCESS');},
 closeCb:function(){window.__log.push('CLOSE')},
 errorCb:function(e){window.__err=String(e&&e.message||e)},
 log:function(d){}});
 if(inst.init)inst.init();inst.render(v);window.__log.push('render');
}catch(e){window.__err=e.stack||String(e)}};</script></body></html>"""%SDK

def detect(bg_bytes,pc_bytes):
    bg=np.array(Image.open(io.BytesIO(bg_bytes)).convert('RGB'))
    pc=np.array(Image.open(io.BytesIO(pc_bytes)).convert('RGBA'))
    g=cv2.cvtColor(bg,cv2.COLOR_RGB2GRAY)
    ys,xs=np.where(pc[:,:,3]>40); x0,x1,y0,y1=xs.min(),xs.max()+1,ys.min(),ys.max()+1
    pcr=pc[y0:y1,x0:x1]; gp=cv2.cvtColor(pcr[:,:,:3],cv2.COLOR_RGBA2GRAY); m=(pcr[:,:,3]>40).astype(np.uint8)
    r=cv2.matchTemplate(g.astype(np.float32),gp.astype(np.float32),cv2.TM_CCORR_NORMED,mask=m)
    _,mx,_,loc=cv2.minMaxLoc(r)
    return int(loc[0]-x0), float(mx)

async def drag(pg, geom, dist):
    x=geom['x']; y=geom['y']
    await pg.mouse.move(x,y); await pg.mouse.down()
    steps=random.randint(38,52)
    pts=[(x+dist*(1-(1-t/ steps)**2.8), y+random.uniform(-0.6,0.6)) for t in range(1,steps+1)]
    pts[-1]=(x+dist,y)
    for px,py in pts:
        await pg.mouse.move(px,py,steps=1); await asyncio.sleep(random.uniform(0.015,0.04))
    await asyncio.sleep(0.15); await pg.mouse.up()

async def main():
    cap=[]; gets=[]; resps=[]
    async with async_playwright() as p:
        b=await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx=b.contexts[0]; pg=await ctx.new_page()
        async def on_resp(r):
            if 'captcha/get' in r.url:
                try: gets.append(json.loads(await r.text()))
                except Exception: pass
            if 'captcha/verify' in r.url:
                try: resps.append((r.status,(await r.text())[:600]))
                except Exception: pass
        async def on_req(r):
            if 'captcha/verify' in r.url:
                open('verify_req.json','w').write(json.dumps({"url":r.url,"body":r.post_data or ""},ensure_ascii=False,indent=1))
        pg.on("response",lambda r: asyncio.ensure_future(on_resp(r)))
        pg.on("request",lambda r: asyncio.ensure_future(on_req(r)))
        await pg.set_content(HTML,wait_until="load"); await pg.wait_for_timeout(3500)
        await pg.evaluate("([v,i])=>window.__boot(v,i)",[json.dumps(VD,ensure_ascii=False),IDS])
        await pg.wait_for_timeout(9000)
        geom=await pg.evaluate("""()=>{const bg=document.querySelector('#captcha_verify_image');
          const btn=document.querySelector('.captcha-slider-btn')||document.querySelector('.captcha_verify_slide--button .dragger-box')||document.querySelector('.dragger-box');
          if(!btn)return null; const r=btn.getBoundingClientRect();
          return {bgNat:bg?bg.naturalWidth:552,bgDisp:bg?bg.getBoundingClientRect().width:340,x:r.x+r.width/2,y:r.y+r.height/2};}""")
        if not geom: print("NO BTN"); return
        nat=geom.get('bgNat') or 552; disp=geom.get('bgDisp') or 340
        scale=disp/nat
        print("geom",geom,"scale",round(scale,3))
        for attempt in range(9):
            await pg.wait_for_timeout(2000)
            if not gets: print("no get"); continue
            q=(gets[-1].get('data') or {}).get('question') or {}
            if not q.get('url1'): print("no q"); continue
            bb=requests.get(q['url1'],timeout=20).content; pb=requests.get(q['url2'],timeout=20).content
            xnat,score=detect(bb,pb)
            candidates=[xnat, xnat-3, xnat+3, xnat-6, xnat+6]
            print(f"attempt{attempt}: xnat={xnat} s={score:.2f} try {candidates}")
            for c in candidates:
                await drag(pg,geom,c*scale)
                await pg.wait_for_timeout(2500)
                succ=await pg.evaluate("()=>{try{return window.__success?JSON.stringify(window.__success):null}catch(e){return null}}")
                if succ:
                    out={"succ":succ,"gets":gets,"resps":resps}
                    open("solve4_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
                    print("SUCCESS!",succ[:2000]); return
        out={"succ":await pg.evaluate("()=>{try{return window.__success?JSON.stringify(window.__success):null}catch(e){return null}}"),"gets":gets,"resps":resps}
        open("solve4_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        print("done, resps:",resps[-3:])
asyncio.run(main())
