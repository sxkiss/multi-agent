import asyncio, json, sys
from playwright.async_api import async_playwright

async def main():
    N = int(sys.argv[1]) if len(sys.argv)>1 else 3
    captured=[]
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = b.contexts[0]
        # 找一个空白/新页面用豆包域
        pg = None
        for c in ctx.pages:
            if c.url=="https://www.doubao.com/chat/":
                pg=c; break
        if pg is None:
            pg = await ctx.new_page()
            await pg.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(3000)
        # 抓所有含 completion / verify 的响应
        async def on_resp(resp):
            u=resp.url
            if 'chat/completion' in u or 'stream_call_bot' in u:
                try: body=await resp.text()
                except Exception: body=''
                captured.append({"url":u,"status":resp.status,"body":body[:8000]})
        pg.on("response", lambda r: asyncio.ensure_future(on_resp(r)))
        for i in range(N):
            try:
                await pg.click(".tiptap.ProseMirror", timeout=8000)
            except Exception as e:
                print("click fail", e)
            await pg.keyboard.type("你好，回复OK")
            await pg.keyboard.press("Enter")
            print("sent", i+1)
            await pg.wait_for_timeout(2500)
        await pg.wait_for_timeout(4000)
        out={"captured":captured}
        open("trigger_out.json","w").write(json.dumps(out,ensure_ascii=False,indent=1))
        print("saved. count=",len(captured))
        for c in captured:
            print("\n---", c['url'][:120], c['status'])
            print(c['body'][:2500])

asyncio.run(main())
