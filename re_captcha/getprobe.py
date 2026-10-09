import asyncio, json, urllib.parse
from playwright.async_api import async_playwright

async def main():
    IDS=json.load(open('ids.json'))
    VERIFY=json.load(open('real_verify.json'))
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        pg = await ctx.new_page()
        await pg.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(2000)
        base="https://verify.zijieapi.com/captcha/get"
        qs={"aid":"582478","app_name":"doubao","lang":"zh","pageId":"27032",
            "bd_version":"1.0.0.739","subtype":"slide","mode":"slide",
            "h5_check_version":"4.0.16","os_name":"other","platform":"pc",
            "os_type":"2","h5_sdk_version":"3.5.76","webdriver":"false"}
        variants={
            "detail+did+fp+scene": dict(qs, detail=VERIFY["detail"], did=IDS["did"], fp=IDS["fp"], verify_scene=VERIFY.get("verify_scene","")),
            "detail+did+fp":       dict(qs, detail=VERIFY["detail"], did=IDS["did"], fp=IDS["fp"]),
            "detail only":         dict(qs, detail=VERIFY["detail"]),
            "no detail":           dict(qs, did=IDS["did"], fp=IDS["fp"]),
        }
        for name, q in variants.items():
            q={k:v for k,v in q.items() if v!=""}
            url=base+"?"+urllib.parse.urlencode(q)
            try:
                r = await pg.evaluate("(u)=>fetch(u,{method:'GET'}).then(r=>r.text())", url)
                print(f"[{name}] -> {r[:400]}")
            except Exception as e:
                print(f"[{name}] ERR {str(e)[:200]}")
        await pg.close()
asyncio.run(main())
