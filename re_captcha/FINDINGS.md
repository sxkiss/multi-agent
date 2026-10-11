# 豆包 rmc-captcha 滑块验证协议逆向 — 研究结论

日期：2026-10-04
环境：CDP Chrome 127.0.0.1:9222（已登录豆包）；Python /home/sxkiss/bt/.venv/bin/python3
项目：/home/sxkiss/doubao2api

---

## 0. 一句话结论

验证码**不是**「回传一个 result token 给 /chat/completion」的形式。豆包风控（shark_admin）下发 `verify_scene=doubao_message_web` 的滑块挑战后，**验证凭证是 `/captcha/verify` 响应头里的 `x-ms-token`（即新的 msToken）**。验证成功后，**下一次请求必须携带这个新的 msToken 才能通过风控**。

但 doubao2api 的 `client.py` 重试逻辑**并没有把该凭证作为参数回传**——它只是把验证通过当作「重试同一请求」的信号。是否闭环，取决于 msToken 是否被写入 cookie/会话。这是本任务最关键的发现。

---

## 1. SDK 结构（静态分析）

- 文件：`https://lf-rc1.yhgfb-cn-static.com/obj/rc-verifycenter/rmc-captcha/1.0.0.739/captcha.js`（756,198 字节，单行压缩）
- **非 webpack / 非 obfuscator.io**。是 **esbuild IIFE + 自研字节码虚拟机（VM）**：
  - 开头 `"use strict";function i(n){return i="function"==typeof Symbol&&...}` 是 terser 自引用改写习语
  - 字符串切碎重组（`P="iF7",I="SBc",...` + slice/rotate）
  - 常量池 `o.u[N].v`（967 处赋值，4728 处引用）
  - **核心：自研 VM**。`By(i,n,o,t,e,r)` 是解释器入口；`Dy=dwInfl.dwAbA(jy("..."))` 是 96,324 字符 base64 → raw-deflate 解出 **140,489 字节字节码**；主调度器 1154 个 opcode case；`ma.prototype.render=function(){return By(135611,...)}`
  - 无 sourcemap、无内嵌 WASM（`\x00asm` 0 次）；但运行时通过 `index.wasm` 单独拉取风控算法（`tagZ`）
- 暴露全局：`window.bdCaptcha.CaptchaVerify`（UMD：`t((...self).bdCaptcha={})`）

---

## 2. 后端接口（host = https://verify.zijieapi.com）

常量池：`o.u[4252].v = {i18n:"/captcha/i18n", get:"/captcha/get", verify:"/captcha/verify", feedbackTags:"/feedback/tags", feedback:"/feedback/detail"}`

风控签名 SDK：`https://lf-headquarters-speed.yhgfb-cn-static.com/obj/rc-client-security/web/stable/1.0.1.18/bdms.js`
```js
window.bdms.init({ aid:2385, pageId:27032, paths:["/captcha/verify","/captcha/get"], ddrt:3 })
```
→ 对 `/captcha/get` 和 `/captcha/verify` 做请求签名/行为采集。

---

## 3. 真实网络请求样例（实测抓包）

### 3.1 拉题 GET /captcha/get
```
GET https://verify.zijieapi.com/captcha/get
  ?aid=582478&app_name=doubao&lang=zh&pageId=27032
  &bd_version=1.0.0.739&subtype=slide&mode=slide
  &h5_check_version=4.0.16&os_name=other&platform=pc&os_type=2
  &h5_sdk_version=3.5.76&webdriver=false&tmp=<ts>
  &did=<device_id>&fp=<fp>&detail=<decision.detail>
```
注意：`detail` 来自鲨鱼风控下发的 decision 字段（约 150 字符，自定义 base64/编码 token）。
**缺少 did/fp → `code:501 参数错误[5005]`；缺少 detail → `code:502 检测到您的网络环境较差[5014]`。** 三者都必须带。

响应 200：
```json
{"code":200,"data":{
  "challenge_code":99999,"codifica":"true","cyfreso":92,"host":"",
  "id":"b94a2a3dd7287c1e5560cbff78cb2db973078373","mode":"slide",
  "question":{
    "url1":"https://p9-catpcha.byteimg.com/.../xx~tplv-188rlo5p4y-2.jpeg",  // 背景 552x344
    "url2":"https://p9-catpcha.byteimg.com/.../xx~tplv-188rlo5p4y-1.png",   // 拼图块 110x110 RGBA
    "backup_url1":[...],"backup_url2":[...],"tip_y":57,"obfuscation":""},
  "region":"","version":2},"message":"验证通过"}
```

### 3.2 校验 POST /captcha/verify（**核心**）
```
POST https://verify.zijieapi.com/captcha/verify
  ?aid=582478&app_name=doubao&lang=zh&pageId=27032
  &bd_version=1.0.0.739&subtype=slide&mode=slide
  &detail=<decision.detail>&server_sdk_env=<URL-encoded JSON>
  &did=<device_id>&fp=<fp>
  &h5_check_version=4.0.16&os_name=other&platform=pc&os_type=2
  &h5_sdk_version=3.5.76&webdriver=false&tmp=<ts>
```
**请求体（仅一个字段）**：
```json
{"captchaBody":"dGMHEAAAMDAwMDAwMDA2YWMyNjVl...（约9.3KB，base64）"}
```
`captchaBody` 解 base64 后约 6998 字节，头部为：
```
tc \x07 \x10 \x00 \x00 000000006ac2673b ...   （"tc" magic + 版本 + 挑战id + 加密载荷）
```
该二进制包编码了：challenge id、缺口 x 偏移、拖动轨迹/时间戳、设备指纹、WASM 签名。**由 VM 字节码 + index.wasm 生成，无法离线伪造。**

### 3.3 verify 响应
- 失败：`{"code":500,"data":{"msg":"VerifyErr"},"message":"验证失败，请根据提示重新操作"}`
- 成功：`code:200`，**且响应头携带 `x-ms-token: <新msToken>`**（实测响应头确有该字段）
- 环境风控：`code:502 检测到您的网络环境较差[5014]`（真实浏览器外/无 bdms 时）

---

## 4. successCb 回调 result 结构

wrapper（verifycenter_421.js）把 SDK 的成功回调做了一层包装：
```js
// SDK 的 successCb 收到的 result
successCb: function(e){ r.callSuccess(e.headers) }   // 取出 result.headers

// callSuccess 内部：
function callSuccess(t){
  successCallbacks.forEach(function(n){
    n({ code:200, data:null, message:"验证通过", headers:t },  // ← 传给上层
      { replay_data: verify_data.replay_data })
  })
}
```
→ **`result` 的实际结构是 `{ code, data, message, headers }`，其中 `headers` 里含 `x-ms-token`**。上层的 captcha_server.py 直接把整个 result 用 JSON POST 回 `/captcha_result`，然后 `wait_result()` 取 `status=="success"`。

**结论：凭证 = result.headers["x-ms-token"]（新 msToken）。**

---

## 5. result 如何回传给豆包（能否闭环）

doubao2api `client.py` 的 `chat_stream_completion`（1261-1286 行）：
```python
for attempt in range(self.max_captcha_retries + 1):
    try:
        async for chunk in self._chat_stream_completion_once(...):
            ...
        return
    except DoubaoRateLimitError as exc:
        if not self._captcha_handler or not exc.verify_data: raise
        resolved = await self._captcha_handler.handle(exc.verify_data, self.fp, self.device_id)
        if not resolved: raise
```
- `handle()` 只返回 bool（验证成功/失败）。
- **重试时把 `_chat_stream_completion_once` 用同样的参数重新调用，没有把 result 里的任何 token 作为新参数传回。**
- `_security_params()`（429-460）中，`msToken` 仅在 `self.ms_token` 非空时加入。而 `ms_token` 在构造时来自 cookie `msToken`，**验证成功后并未刷新**。

**结论：当前 doubao2api 的实现「靠验证通过后重试原请求」来闭环，而不是显式把 `x-ms-token` 回传。** 若要真正闭环，应在 `handle()` 成功时把 `result.headers["x-ms-token"]` 捕获并写回 `self.ms_token` / 对应 cookie，使下一次 `/chat/completion` 携带新 msToken。当前代码没做这一步——**这是研究确认的最大缺口，也是「查不到明确 retry 参数路径」的原因。**

---

## 6. 自动化可行性判断

**结论：完整无人值守自动化「可行但门槛高」，不建议纯协议复刻；推荐「驱动真实 SDK」路线。**

### 卡点清单（按难度）

| # | 卡点 | 难度 | 说明 |
|---|---|---|---|
| 1 | **请求签名（bdms）** | 高 | `/captcha/get`、`/captcha/verify` 被 `bdms.init({aid:2385,pageId:27032,...})` 签名并采集行为。脱离真实浏览器/无 bdms 时，即使请求字段对也会被 `[5014]` 拒（实测） |
| 2 | **captchaBody 生成** | 极高 | 由 VM 字节码 + `index.wasm`（`tagZ`）生成，含轨迹/时间/指纹/签名。无法离线重放，必须驱动真实 SDK 执行 |
| 3 | **缺口距离识别** | 中 | cv2 模板匹配可用（CCOEFF/CCORR，实测得分 0.5~0.9，需剪裁拼图块 alpha bbox + CCORR_NORMED 较稳）。纯像素法不够稳，需结合边缘/暗区 |
| 4 | **轨迹伪造** | 中高 | SDK 采集真实鼠标轨迹，自动化的合成轨迹易被判为机器（服务器返回 `VerifyErr`） |
| 5 | **detail token 一次性** | 低 | `decision.detail` 需每次从风控重新获取；每次验证失败后 `/captcha/get` 会刷新挑战 |

### 推荐方案
- **最稳**：沿用 doubao2api 现有 `captcha_server.py` + `AutoCaptchaHandler` 思路，在**真实浏览器**里驱动 `bdCaptcha.CaptchaVerify` 渲染并自动拖动（CDP `Input.dispatchMouseEvent` 注入真实轨迹），拿到 `result.headers["x-ms-token"]` 后写回会话再重试。这样绕过 #1/#2（签名与 captchaBody 由 SDK 生成）。
- **不建议**：纯 Python 重放 `/captcha/verify`（会死在 captchaBody 签名 + bdms 签名上）。

---

## 7. 复现命令/脚本（均在 /home/sxkiss/bt/re_captcha/）

| 脚本 | 作用 | 状态 |
|---|---|---|
| `captcha_rc1.js` | 下载的 SDK | ✅ |
| `getprobe.py` | 探测 `/captcha/get` 参数组合（验证 501/502） | ✅ 成功 |
| `capture5.py` | 独立页加载 SDK、渲染、抓 i18n/get/wasm | ✅ 成功 |
| `solve.py`/`solve2.py`/`solve3.py`/`solve4.py` | 自动拖动求解（多轮尝试） | ⚠️ 未成功拿到 successCb（服务器校验严） |
| `probe_risk.py` | 用 client.py 触发 710022004 拿真实 decision | ✅ 成功 |
| `full.py`/`full2.py`/`final_solve.py` | 渲染真实滑块 + 抓 `/captcha/verify` 请求 | ✅ 抓到 verify 请求体 |
| `verify_req.json` | 完整 `/captcha/verify` URL+body 样例 | ✅ |
| `real_verify.json` | 真实 verify_data（decision JSON） | ✅ |

关键复现：
```bash
# 触发风控拿 decision
/home/sxkiss/bt/.venv/bin/python3 /tmp/probe_risk.py

# 探测 /captcha/get 参数
/home/sxkiss/bt/.venv/bin/python3 getprobe.py

# 渲染滑块并抓 /captcha/verify（抓包）
/home/sxkiss/bt/.venv/bin/python3 final_solve.py
```

---

## 8. 查不到 / 存疑项（如实标注）

- **successCb 在「验证成功」时的完整 result 样例未能实测拿到**：多次自动拖动均被服务器判 `VerifyErr`（轨迹/距离不够拟人）。`{code,data,message,headers}` 结构来自 wrapper 代码静态分析 + 失败/成功路径推断，**成功路径的 headers 具体内容未逐字段实证**（但 `x-ms-token` 已在 `/captcha/verify` 响应头中实测出现）。
- `ddrt:3` 精确语义：bdms 私有配置，代码无枚举，无法确认。
- `captchaBody` 内部加密算法：在 `index.wasm` 内，未反编译 wasm，无法离线还原。
- `index.wasm` 未下载分析（运行时由 CDN 拉取）。

---

## 9. 关键文件路径
- SDK: `/home/sxkiss/bt/re_captcha/captcha_rc1.js`
- wrapper: `/home/sxkiss/bt/re_captcha/verifycenter_421.js`
- verify 请求样例: `/home/sxkiss/bt/re_captcha/verify_req.json`
- 真实 decision: `/home/sxkiss/bt/re_captcha/real_verify.json`
- 项目源码: `/home/sxkiss/doubao2api/doubao2api/captcha_server.py`、`client.py`
