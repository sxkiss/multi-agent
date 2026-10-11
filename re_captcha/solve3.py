import asyncio, json, io, time
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
    a=pc[:,:,3]; ys,xs=np.where(a>60)
    if len(xs)==0: return 0,0
    x0,x1,y0,y1=xs.min(),xs.max()+1,ys.min(),ys.max()+1
    pcrop=pc[y0:y1,x0:x1]
    gb=cv2.Canny(cv2.cvtColor(bg,cv2.COLOR_RGB2GRAY),100,200)
    gp=cv2.Canny(cv2.cvtColor(pcrop[:,:,:3],cv2.COLOR_RGBA2GRAY),100,200)
    m=(pcrop[:,:,3]>60).astype(np.uint8)
    gp=gp*m
    res=cv2.matchTemplate(gb,gp,cv2.TM_CCORR_NORMED)
    _,mx,_,loc=cv2.minMaxLoc(res)
    return int(loc[0]-x0), float(mx)

async def main():
    cap=[]; results=[]
    async with async_playwright() as p:
        b=await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx=b.contexts[0]; pg=await ctx.new_page()
        gets=[]
        async def on_resp(r):
            if 'captcha/get' in r.url:
                try: gets.append(json.loads(await r.text()))
                except Exception: pass
            if 'captcha/verify' in r.url:
                try: results.append(("VERIFY_RESP", r.status, (await r.text())[:4000]))
                except Exception: pass
        async def on_req(r):
            if 'captcha/verify' in r.url:
                cap.append({"url":r.url,"body":(r.post_data or "")[:200], "len":len(r.post_data or "")})
        pg.on("response",lambda r: asyncio.ensure_future(on_resp(r)))
        pg.on("request",lambda r: asyncio.ensure_future(on_req(r)))
        await pg.set_content(HTML,wait_until="load"); await pg.wait_for_timeout(3500)
        await pg.evaluate("([v,i])=>window.__boot(v,i)",[json.dumps(VD,ensure_ascii=False),IDS])
        await pg.wait_for_timeout(8000)
        geom=await pg.evaluate("""()=>{const bg=document.querySelector('#captcha_verify_image');
          const btn=document.querySelector('.captcha-slider-btn')||document.querySelector('.captcha_verify_slide--button .dragger-box');
          const r=btn.getBoundingClientRect();
          return {bgNat:bg.naturalWidth,bgDisp:bg.getBoundingClientRect().width,x:r.x+r.width/2,y:r.y+r.height/2,w:r.width};}""")
        scale=geom['bgDisp']/geom['bgNat']
        print("geom",geom,"scale",round(scale,3))
        for attempt in range(6):
            await pg.wait_for_timeout(2500)
            if not gets: print("no get"); continue
            d=gets[-1].get('data') or {}
            q=d.get('question') or {}
            if not q.get('url1'): print("attempt",attempt,"no question"); continue
            try:
                bb=requests.get(q['url1'],timeout=20).content; pb=requests.get(q['url2'],timeout=20).content
            except Exception as e:
                print("img err",e); continue
            xnat,score=detect(bb,pb)
            dist=xnat*scale
            print(f"attempt {attempt}: xnat={xnat} score={round(score,3)} → drag {round(dist,1)}px")
            await pg.mouse.move(geom['x'],geom['y']); await pg.mouse.down()
            steps=40
            for i in range(1,steps+1):
                t=i/steps; e=1-(1-t)**2.5
                await pg.mouse.move(geom['x']+dist*e, geom['y'], steps=1)
                await asyncio.sleep(0.022)
            await asyncio.sleep(0.2); await pg.mouse.up()
            await pg.wait_for_timeout(3500)
            succ=await pg.evaluate("()=>{try{return window.__success?JSON.stringify(window.__success):null}catch(e){return null}}")
            print("   success?", (succ or '')[:600])
            if succ: break
        out={"geom":geom,"verify_reqs":cap,"verify_resps":results,
             "succ":await pg.evaluate("()=>{try{return window.__success?JSON.stringify(window.__success):null}catch(e){return null}}"),
             "log":await pg.evaluate("()=>window.__log"),
             "gets":[{k:v for k,v in g.items() if k!='data'} for g in gets]}
        open("solve3_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        await pg.screenshot(path="solve3_state.png",full_page=True)
        print("SAVED")
        await pg.close()
asyncio.run(main())
