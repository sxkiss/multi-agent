---
name: wechat-875-route
description: |
  通过 875 协议网关发送微信消息（文本/图片/视频/语音/链接/文件/名片/音乐卡片）。
  当 WeChatRoute 工具带 route="875" 时由本技能接管发送；与 869 发送端完全隔离。
  Use when: 需要走 875 协议出站微信消息，或 869 通道不可用需切换协议。
metadata:
  project: Multi-Agent
  category: wechat
  protocol: "875"
  tier: 2
  user_visible: true
triggers:
  - "875 发送"
  - "微信 875"
  - "wechatpad 发送"
---

# WeChatRoute875 — 875 协议微信发送技能

## 定位

`WeChatRoute` 工具的 **875 协议发送后端**。独立技能、独立 CLI、独立配置，
与 `wechat-869-media-sender` **零耦合、互不干扰**：配置各读各的、失败互不影响、
不共享任何依赖（875 语音按 Type 直传，不需要 pydub/pysilk 转码）。

调用方一般不用直接操作本技能——`WeChatRoute(..., route="875")` 会自动转发。
需要单独调试时可直接跑 CLI。

## 目录

```
wechat-875-route/
├── SKILL.md
├── .gitignore                       # 屏蔽真实卡密配置
├── config/
│   ├── wechat-875.json.example      # 模板（占位符，可入库）
│   └── wechat-875.json              # 真实卡密（已 ignore，绝不入库）
└── scripts/
    └── send_875.py                  # 统一 CLI，8 个子命令
```

## 配置

从模板复制并填入 875 网关地址与卡密：

```bash
cp config/wechat-875.json.example config/wechat-875.json
```

```json
{
  "base_url": "http://172.17.0.1:18062",
  "authcode": "<875 卡密>",
  "timeout": 30
}
```

读取优先级：`--config <path>` > 技能内 `config/wechat-875.json` >
`~/.openclaw/credentials/wechat-875.json`。卡密绝不打印到 stdout/stderr/日志。

调用约定（对齐 allbot 的 875 直连封装）：
`POST {base}/api{path}?authcode=<卡密>`，头带 `X-Access-Token: <卡密>`。
swagger basePath = `/api`，故 `/Msg/SendTxt` → `/api/Msg/SendTxt`。

## 8 种消息类型 → 875 接口

| 子命令 | 接口 | 关键字段 |
|---|---|---|
| text | `/api/Msg/SendTxt` | `ToWxid`,`Content`,`Type=1`,`At`(群@逗号分隔) |
| image | `/api/Msg/UploadImg` | `ToWxid`,`Base64` |
| video | `/api/Msg/SendVideo` | `ToWxid`,`Base64`,`PlayLength`(秒) |
| voice | `/api/Msg/SendVoice` | `ToWxid`,`Base64`,`Type`,`VoiceTime`(毫秒) |
| file | `/api/Msg/SendFile` | `ToWxid`,`Base64`,`FileName` |
| link | `/api/Msg/SendAppMessage` | `items:[{kind:"link",link:{title,url,description,thumbUrl}}]` |
| card | `/api/Msg/ShareCard` | `ToWxid`,`CardWxId`,`CardNickName`,`CardAlias` |
| music | `/api/Msg/SendApp` | `ToWxid`,`Type=3`,`Xml`(appmsg，**appid 显式写入**)；默认从内置 appid 池随机抽 |

媒体全部 **Base64 直传**，无 CDN 分步上传；不要碰 `SendCDN*`（那是转发已有消息用的）。

> **语音长度上限（2026-11-16 实测）**：`/Msg/SendVoice` 单次语音 >65000 字节会被网关拒收
> （`Code=-2`、`Offset=65000`，错码误标 `INVALID_CREDENTIAL`）。`voice` 子命令已内置分片：
> SILK 按帧边界切、每片补回帧头，逐片 POST（每片独立成一条语音，长语音会变多条短语音）。
> 短语音（≤65000 字节）仍是单次发送，行为不变。`--seconds` 会按片数均分到各片 `VoiceTime`。


语音 `Type`：`AMR=0 / SPEEX=1 / MP3=2 / WAVE=3 / SILK=4`，`VoiceTime` 毫秒（1000=1秒）。

## CLI 用法

```bash
python3 scripts/send_875.py text  --to <wxid|xxx@chatroom> --text "你好" [--at wxid1 --at wxid2]
python3 scripts/send_875.py image --to <wxid> --path /abs/a.jpg
python3 scripts/send_875.py video --to <wxid> --path /abs/a.mp4 --seconds 10
python3 scripts/send_875.py voice --to <wxid> --path /abs/a.mp3 --format mp3 --seconds 3
python3 scripts/send_875.py file  --to <wxid> --path /abs/a.pdf --name a.pdf
python3 scripts/send_875.py link  --to <wxid> --url https://x --title 标题 --desc 摘要 --thumb-url https://x/t.jpg
python3 scripts/send_875.py card  --to <wxid> --card-wxid wxid_xxx --card-nickname 昵称
python3 scripts/send_875.py music --to <wxid> --music-url https://x/a.mp3 --title 歌名 --singer 歌手 --cover-url https://x/c.jpg
```

### 音乐卡片 appid（卡片id）

875 结构化端点 `/Msg/SendAppMessage` 的 `music` **没有 appid 入参**，网关会用默认值，
卡片id 就"消失"了。所以 `music` 改走 `/Msg/SendApp`（`ToWxid/Type=3/Xml`），自己拼
`appmsg appid="..."` 把 appid 显式带上，与 869 的音乐卡片一致。

- 内置池：`assets/card_ids.txt`（307 条，与 869 技能同源，自包含一份），每行 `wxid<TAB>app名`。
- 默认随机抽一个，输出 JSON 带 `pickedCardId` / `pickedCardAppName` / `cardIdPool`。
- `--card-id <appid>`：显式指定卡片id（不走池）。
- `--card-id-pool <path>`：换自定义池文件。
- `--card-type 摇一摇搜歌|原卡片`：模板不同（默认 appid / version / appname 不同）。
- 池缺失时回退到固定默认 appid（摇一摇搜歌 `wx485a97c844086dc9` / 原卡片 `wx79f2c4418704b4f8`）。

```bash
# 随机卡片id（默认）
python3 scripts/send_875.py music --to <wxid> --music-url https://x/a.mp3 --title 歌名 --singer 歌手
# 指定卡片id
python3 scripts/send_875.py music --to <wxid> --music-url https://x/a.mp3 --title 歌名 --card-id wx1234567890abcdef
```

输出统一 JSON：`{"status":true/false,"msg":"...","data":<875 Data>}`，失败非零退出码。
成功判定以 `Code==0` 为准，不信 `Success` 布尔。

## 门禁

875 网关需有效卡密 + 账号在线。只读探活：

```bash
curl -s -H "X-Access-Token: <卡密>" "http://172.17.0.1:18062/api/Login/GetLoginStatus"
# Code=0 且 账号在线 才可用
```

卡密停用（`Code=-3`）时发送会全部失败——先启用有效卡密，不要尝试绕过厂商授权。
