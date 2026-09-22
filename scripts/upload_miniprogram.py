#!/usr/bin/env python3
"""
@input: miniprogram/private.key（上传密钥）, project.config.json, miniprogram-ci
@output: 上传小程序代码到微信平台（版本号/备注由命令行传入）
@position: 发布层 — 免开发者工具的命令行上传通道
@auto-doc: Update header and folder INDEX.md when this file changes

小程序上传脚本
------------------------------------------------------------------
用途：跳过微信开发者工具的 GUI 操作，用官方 miniprogram-ci 上传。
免扫码前提是后台已开启「小程序代码上传」并下载 private.key，
且当前出口 IP 在白名单内。

用法：
    python3 scripts/upload_miniprogram.py 1.0.0 "首次提交"

依赖：在 miniprogram/ 下执行过 npm install miniprogram-ci
"""

import json
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MP_DIR = os.path.join(BASE_DIR, "miniprogram")
KEY_PATH = os.path.join(MP_DIR, "private.key")
CI_BIN = os.path.join(MP_DIR, "node_modules", ".bin", "miniprogram-ci")


def _load_appid() -> str:
    with open(os.path.join(MP_DIR, "project.config.json"), "r", encoding="utf-8") as f:
        return str(json.load(f).get("appid", "")).strip()


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else "1.0.0"
    desc = sys.argv[2] if len(sys.argv) > 2 else "命令行上传"

    if not os.path.exists(KEY_PATH):
        print("缺少上传密钥 miniprogram/private.key")
        print("  获取：微信公众平台 → 开发管理 → 开发设置 → 小程序代码上传 → 下载密钥")
        print("  注意：该密钥等同部署凭据，已加入 .gitignore，请勿提交。")
        return 2
    if not os.path.exists(CI_BIN):
        print("未安装 miniprogram-ci，请先在 miniprogram/ 执行 npm install miniprogram-ci")
        return 2

    appid = _load_appid()
    if not appid:
        print("project.config.json 中未配置 appid")
        return 2

    print(f"上传中: appid={appid} version={version}")
    print(f"备注: {desc}")

    cmd = [
        CI_BIN, "upload",
        "--pp", MP_DIR,
        "--pkp", KEY_PATH,
        "--appid", appid,
        "--uv", version,
        "--ud", desc,
        "-r", "1",
        "--enable-es6", "true",
        "--enable-minify", "true",
    ]
    try:
        result = subprocess.run(cmd, cwd=MP_DIR, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("上传超时（300s）")
        return 1

    out = (result.stdout or "") + (result.stderr or "")
    print(out.strip()[-1500:])

    if result.returncode != 0:
        print(f"\n上传失败（exit={result.returncode}）")
        low = out.lower()
        if "ip" in low and ("白名单" in out or "whitelist" in low):
            print("  → 可能是当前 IP 不在上传白名单，需在后台添加")
        if "invalid" in low and "key" in low:
            print("  → 可能是 private.key 与 appid 不匹配")
        return result.returncode

    print(f"\n上传成功：版本 {version}")
    print("下一步：微信公众平台 → 版本管理 → 提交审核")
    return 0


if __name__ == "__main__":
    sys.exit(main())
