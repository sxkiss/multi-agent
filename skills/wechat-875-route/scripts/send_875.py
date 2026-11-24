#!/usr/bin/env python3
"""WeChatRoute875 发送 CLI —— 直连 875 协议网关（/api/Msg/*），与 869 完全隔离。

用法：send_875.py <子命令> [参数]，子命令：
    text / image / video / voice / link / file / card / music

输出统一 JSON：{"status":true/false,"msg":"...","data":<875 Data 或完整响应>}
失败返回非零退出码。配置（base_url/authcode）从 config/wechat-875.json 读取，
绝不打印卡密。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape

_HERE = Path(__file__).resolve().parent
_SKILL = _HERE.parent

# 语音格式 → 875 Type（swagger: AMR=0 SPEEX=1 MP3=2 WAVE=3 SILK=4）
_VOICE_TYPE = {"amr": 0, "speex": 1, "mp3": 2, "wav": 3, "wave": 3, "silk": 4}

# 音乐卡片 appid 池（与 869 技能同源，随本技能自包含一份）
CARD_ID_POOL_PATH = _SKILL / "assets" / "card_ids.txt"
# 摇一摇搜歌 / 原卡片 默认 appid（与 869 技能一致）
_DEFAULT_APPID_SHAKE = "wx485a97c844086dc9"
_DEFAULT_APPID_NATIVE = "wx79f2c4418704b4f8"


def _load_card_pool(pool_path: str) -> list[tuple[str, str]]:
    """解析 appid 池：每行 'wxid<TAB或空格>app名'，返回 [(card_id, app_name)]。"""
    p = Path(pool_path).expanduser() if pool_path else CARD_ID_POOL_PATH
    if not p.exists():
        return []
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.search(r"[A-Za-z0-9_\-]{8,}", s)
        if not m or m.group(0) in seen:
            continue
        cid = m.group(0)
        seen.add(cid)
        entries.append((cid, s[m.end():].strip()))
    return entries


def _pick_card_id(explicit: str, pool_path: str) -> tuple[str, str, str]:
    """返回 (card_id, app_name, pool_path_used)。explicit 优先，否则从池随机抽。"""
    if explicit.strip():
        return explicit.strip(), "", "(显式指定)"
    p = pool_path.strip() or str(CARD_ID_POOL_PATH)
    pool = _load_card_pool(p)
    if pool:
        cid, name = random.choice(pool)
        return cid, name, p
    return "", "", p  # 池缺失时由 _build_music 回退默认 appid


def _build_music_xml(*, title, singer, music_url, cover_url, lyric, jump_url,
                     card_type, appid, app_name) -> str:
    """构造微信音乐 appmsg XML（type=3），appid 显式写入（875 /Msg/SendApp 支持）。"""
    native = (card_type or "摇一摇搜歌").strip() == "原卡片"
    if not appid:
        appid = _DEFAULT_APPID_NATIVE if native else _DEFAULT_APPID_SHAKE
    if not app_name:
        app_name = "" if native else "摇一摇搜歌"
    version = "1" if native else "29"
    jump = jump_url or music_url
    appmsg = (
        f'<appmsg appid="{xml_escape(appid)}" sdkver="0">'
        f'<title>{xml_escape(title or "")}</title>'
        f'<des>{xml_escape(singer or "")}</des>'
        '<action>view</action><type>3</type><showtype>0</showtype><content/>'
        f'<url>{xml_escape(jump)}</url>'
        f'<dataurl>{xml_escape(music_url or "")}</dataurl>'
        f'<lowurl>{xml_escape(jump)}</lowurl>'
        f'<lowdataurl>{xml_escape(music_url or "")}</lowdataurl>'
        '<thumburl/>'
        f'<songlyric>{xml_escape(lyric or "")}</songlyric>'
        f'<songalbumurl>{xml_escape(cover_url or "")}</songalbumurl>'
        '<appattach><totallen>0</totallen><attachid/><emoticonmd5/><fileext/><aeskey/></appattach>'
        '<weappinfo><pagepath/><username/><appid/><appservicetype>0</appservicetype></weappinfo>'
        '</appmsg>'
    )
    tail = (
        '<fromusername></fromusername><scene>0</scene>'
        f'<appinfo><version>{version}</version><appname>{xml_escape(app_name)}</appname></appinfo>'
        '<commenturl/>'
    )
    return appmsg + tail


def _load_config(explicit: str | None) -> dict:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates += [
        _SKILL / "config" / "wechat-875.json",
        Path.home() / ".openclaw" / "credentials" / "wechat-875.json",
    ]
    for p in candidates:
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
            if "<" in str(cfg.get("authcode", "")):
                raise ValueError(f"配置文件 {p.name} 仍是模板占位符，请填入真实 875 卡密")
            if not cfg.get("base_url") or not cfg.get("authcode"):
                raise ValueError(f"配置 {p.name} 缺少 base_url / authcode")
            return cfg
    raise FileNotFoundError("未找到 875 配置（config/wechat-875.json）")


def _post(cfg: dict, path: str, body: dict) -> dict:
    """POST {base}/api{path}?authcode=<key>，返回 875 完整响应 dict。"""
    base = str(cfg["base_url"]).rstrip("/")
    key = str(cfg["authcode"])
    url = f"{base}/api{path}?authcode={urllib.parse.quote(key)}"
    headers = {"Content-Type": "application/json", "X-Access-Token": key}
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    timeout = float(cfg.get("timeout") or 30)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise RuntimeError(f"875 返回非 JSON: {raw[:160]}")


def _b64_file(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    return base64.b64encode(p.read_bytes()).decode()


def _build(sub: str, a: argparse.Namespace) -> tuple[str, dict]:
    """按 msg_type 生成 (path, body)。"""
    to = (a.to or "").strip()
    if not to:
        raise ValueError("缺少 --to")

    if sub == "text":
        at = ",".join([x for x in (a.at or []) if x])
        return "/Msg/SendTxt", {
            "ToWxid": to, "Content": a.text or "", "Type": 1, "At": at,
        }

    if sub == "image":
        return "/Msg/UploadImg", {"ToWxid": to, "Base64": _b64_file(a.path)}

    if sub == "video":
        body = {"ToWxid": to, "Base64": _b64_file(a.path), "PlayLength": int(a.seconds or 0)}
        # 875 封面可选；没有就留空，不阻塞主流程
        return "/Msg/SendVideo", body

    if sub == "voice":
        fmt = (a.format or "amr").lower()
        vtype = _VOICE_TYPE.get(fmt, 0)
        return "/Msg/SendVoice", {
            "ToWxid": to,
            "Base64": _b64_file(a.path),
            "Type": vtype,
            "VoiceTime": int(float(a.seconds or 0) * 1000),
        }

    if sub == "file":
        name = (a.name or "").strip() or os.path.basename(a.path)
        return "/Msg/SendFile", {
            "ToWxid": to, "Base64": _b64_file(a.path), "FileName": name,
        }

    if sub == "link":
        item = {
            "kind": "link", "toUserId": to,
            "link": {
                "title": a.title or "", "url": a.url or "",
                "description": a.desc or "", "thumbUrl": a.thumb_url or "",
            },
        }
        return "/Msg/SendAppMessage", {"items": [item]}

    if sub == "card":
        return "/Msg/ShareCard", {
            "ToWxid": to, "CardWxId": a.card_wxid or "",
            "CardNickName": a.card_nickname or "", "CardAlias": "",
        }

    if sub == "music":
        # 走 /Msg/SendApp 原始 XML 端点：只有它能显式带 appid（卡片id）。
        # 结构化 /Msg/SendAppMessage 无 appid 入参，网关会用默认值，卡片id"消失"。
        explicit_id = getattr(a, "card_id", "") or ""
        pool = getattr(a, "card_id_pool", "") or ""
        card_id, app_name, pool_used = _pick_card_id(explicit_id, pool)
        if not card_id:
            card_id = _DEFAULT_APPID_NATIVE if (a.card_type or "") == "原卡片" else _DEFAULT_APPID_SHAKE
        xml = _build_music_xml(
            title=a.title or "", singer=a.singer or "",
            music_url=a.music_url or "", cover_url=a.cover_url or "",
            lyric=a.lyric or "", jump_url=getattr(a, "jump_url", "") or "",
            card_type=a.card_type or "摇一摇搜歌",
            appid=card_id, app_name=app_name,
        )
        meta = {"pickedCardId": card_id, "pickedCardAppName": app_name, "cardIdPool": pool_used}
        return "/Msg/SendApp", {"ToWxid": to, "Type": 3, "Xml": xml}, meta

    raise ValueError(f"不支持的子命令: {sub}")


# 875 /api/Msg/SendVoice 单次 >65000 字节网关拒收(Code=-2 / Offset=65000，
# 错码误标 INVALID_CREDENTIAL)。长语音需按 SILK_V3 帧边界切分、每片补帧头，
# 逐片 POST（网关忽略续传字段，每片独立成一条语音）。与 allbot 875 core.py 同款。
_VOICE_MAX_BYTES = 60000
_VOICE_GW_LIMIT = 65000


def _split_voice_b64(b64: str, max_bytes: int = _VOICE_MAX_BYTES) -> list[str]:
    """把超长语音 Base64 切成多个合法分片；无需切时返回单元素列表。

    SILK_V3 帧为 2 字节小端长度前缀，逐帧累加到阈值，切点只在帧边界，
    每片补回帧头 b'\x02#!SILK_V3'（每片可独立解码）。其它格式无统一帧结构，
    退化按字节硬切并尽量补 AMR 头，至少避免 Code=-2（可播性由格式决定）。
    """
    try:
        data = base64.b64decode(b64)
    except Exception:
        return [b64]
    if len(data) <= _VOICE_GW_LIMIT:
        return [b64]
    head = b""
    for h in (b"\x02#!SILK_V3", b"#!SILK_V3", b"\x02#!SILK", b"#!AMR\n"):
        if data.startswith(h):
            head = h
            break
    payload = data[len(head):] if head else data
    frames: list[bytes] = []
    framed = bool(head) and b"SILK" in head
    if framed:
        i = 0
        while i + 2 <= len(payload):
            n = int.from_bytes(payload[i:i + 2], "little")
            if n <= 0 or i + 2 + n > len(payload):
                framed = False
                break
            frames.append(payload[i:i + 2 + n])
            i += 2 + n
        framed = framed and i == len(payload) and bool(frames)
    if framed:
        chunks, cur = [], b""
        for f in frames:
            if cur and len(head) + len(cur) + len(f) > max_bytes:
                chunks.append(head + cur)
                cur = f
            else:
                cur += f
        if cur:
            chunks.append(head + cur)
    else:
        step = max(1, max_bytes - len(head))
        chunks = [head + payload[o:o + step]
                  for o in range(0, len(payload), step) if payload[o:o + step]]
    return [base64.b64encode(c).decode() for c in chunks if c] or [b64]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WeChatRoute875 发送 CLI")
    p.add_argument("--config", help="显式指定配置文件路径")
    sub = p.add_subparsers(dest="sub", required=True)

    def add(name: str, *flags: str):
        sp = sub.add_parser(name)
        sp.add_argument("--to", required=True)
        for f in flags:
            sp.add_argument(f, default="")
        return sp

    t = add("text", "--text")
    t.add_argument("--at", action="append", default=[])
    t.add_argument("--at-name", action="append", default=[])

    add("image", "--path")
    v = add("video", "--path")
    v.add_argument("--seconds", default="0")
    vo = add("voice", "--path", "--format")
    vo.add_argument("--seconds", default="0")
    add("file", "--path", "--name")
    add("link", "--url", "--title", "--desc", "--thumb-url")
    add("card", "--card-wxid", "--card-nickname")
    m = add("music", "--title", "--singer", "--music-url", "--cover-url", "--lyric", "--card-type")
    # 音乐卡片 appid（卡片id）：默认从内置池随机抽，可显式指定或换池
    m.add_argument("--card-id", default="", help="显式指定音乐卡片 appid（卡片id）；不传则从内置池随机抽")
    m.add_argument("--card-id-pool", default="", help="自定义 appid 池文件路径；不传用技能内置 assets/card_ids.txt")
    m.add_argument("--jump-url", default="", help="点击卡片跳转 URL；不传用 music-url")
    return p


def main() -> int:
    args = _parser().parse_args()
    try:
        cfg = _load_config(args.config)
        built = _build(args.sub, args)
        if len(built) == 3:
            path, body, meta = built
        else:
            path, body = built
            meta = {}
        # voice 且 Base64 解码后超阈值 → 按 SILK 帧边界分片逐片 POST；
        # 其它子命令 / 短语音走原单次路径，行为不变。
        parts = None
        if (args.sub == "voice" and isinstance(body, dict) and body.get("Base64")):
            _parts = _split_voice_b64(str(body["Base64"]))
            if len(_parts) > 1:
                parts = _parts
        if parts:
            n = len(parts)
            total_vt = body.get("VoiceTime")
            resp = None
            for idx, b64 in enumerate(parts):
                seg = dict(body)
                seg["Base64"] = b64
                if isinstance(total_vt, int) and total_vt > 0:
                    seg["VoiceTime"] = max(1, total_vt // n)
                resp = _post(cfg, path, seg)
                if resp.get("Code") != 0:   # 某片失败即停，返回该片响应
                    break
                if idx + 1 < n:
                    time.sleep(0.35)
            meta = {**meta, "voiceParts": n}
        else:
            resp = _post(cfg, path, body)
        code = resp.get("Code")
        ok = (code == 0)
        data = resp.get("Data") if "Data" in resp else resp
        out = {
            "status": ok,
            "msg": "已发送" if ok else (resp.get("Message") or f"875 返回 Code={code}"),
            "data": data,
        }
        if meta:
            out.update(meta)  # pickedCardId / pickedCardAppName / cardIdPool
        print(json.dumps(out, ensure_ascii=False))
        return 0 if ok else 1
    except urllib.error.HTTPError as e:
        print(json.dumps({"status": False, "msg": f"875 HTTP {e.code}: {e.reason}"}, ensure_ascii=False))
        return 1
    except Exception as e:  # noqa: BLE001 - CLI 绝不抛栈
        print(json.dumps({"status": False, "msg": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
