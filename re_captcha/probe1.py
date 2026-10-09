import asyncio, json
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctxs = b.contexts
        print("contexts:", len(ctxs))
        for c in ctxs:
            print(" ctx pages:", len(c.pages))
            for pg in c.pages:
                print("   -", pg.url[:90])
        # find doubao page
        target=None
        for c in ctxs:
            for pg in c.pages:
                if "doubao.com" in pg.url:
                    target=(c,pg); break
            if target: break
        if not target:
            print("NO doubao page"); return
        c,pg = target
        print("target page:", pg.url)
        # check for existing captcha sdk / globals in doubao page
        r = await pg.evaluate("""() => {
          const out = {};
          out.hasBdCaptcha = typeof window.bdCaptcha;
          out.bdCaptchaKeys = window.bdCaptcha ? Object.keys(window.bdCaptcha) : null;
          out.hasBdms = typeof window.bdms;
          out.bdmsKeys = window.bdms ? Object.keys(window.bdms) : null;
          out.scripts = Array.from(document.scripts).map(s=>s.src).filter(s=>s && (s.includes('captcha')||s.includes('bdms')||s.includes('security')||s.includes('verify')));
          out.ls_keys = Object.keys(localStorage).filter(k=>/captcha|verify|vc_|bdms|risk/i.test(k));
          return out;
        }""")
        print(json.dumps(r, ensure_ascii=False, indent=2))

asyncio.run(main())
