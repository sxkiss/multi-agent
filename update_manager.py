"""
@input: os, json, re, time, shutil, subprocess, tarfile, zipfile, urllib, tempfile, threading
@output: bootstrap_version / check_update / get_status / apply_update / rollback
@auto-doc: Update header and folder INDEX.md when this file changes

框架自动更新管理器（检测与执行分离，绝不自动安装）。

设计要点：
  1. version.json 是「本地运行时锚点」——不提交仓库（.gitignore 排除），
     启动时没有就自动生成（记为当前基线），有就检查更新。
  2. 检测目标是 main 分支（可用 config.json update.branch 覆盖）。
  3. 检测/更新内容走 GitHub API + codeload（git 协议 TLS 可能不通）。
     匿名 API 会限流，token 优先取 config.update.token，否则从 git remote 提取。
  4. 双通道：当前分支==目标分支且有 git 命令 → git 增量；否则 zip 覆盖。
     zip 通道只覆盖/新增，从不删除本地文件；保留清单跳过配置文件。
  5. 更新前做文件级回滚点，失败自动恢复；成功后写回新 sha 并标记需重启。
  6. 本模块只提供 apply_update 供前端点按钮后调用，绝不自行安装。

返回约定：公开函数返回 {"ok": bool, "msg": str, "data": dict}，
由 update_routes.py 转成 public.return_data 统一响应。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# 默认配置（config.json 的 update 段缺失时逐项兜底）
# ---------------------------------------------------------------------------
DEFAULT_CFG: dict[str, Any] = {
    "enabled": True,
    "repo": "sxkiss/multi-agent",
    "branch": "main",                 # 用户明确要求：检测 main 分支
    "api_base": "https://api.github.com",
    "zip_base": "https://codeload.github.com",
    "zip_mirror_prefix": "",          # 国内可填 "https://ghfast.top/"
    "check_interval_min": 60,
    "channel": "auto",                # auto | git | zip
    "token": "",                      # 留空则自动从 git remote 提取
    "http_timeout": 10,
}

VERSION_FILE = "version.json"
UPDATE_LOCK = threading.Lock()

# 保留清单：命中则 zip 覆盖时跳过（配置/数据/凭证/锚点本身）。
# git 仓库里被 .gitignore 的文件本就不在 zip 包内；此清单是第二道保险，
# 并保护「tracked 但本地视为配置」的文件。
PRESERVE_EXACT = {
    "config.json", "auth.json", "gateway.json", "connector_hub.json",
    "version.json", ".update_state.json", "agents.json",
}
PRESERVE_PREFIX = (
    "agents_data/", "sessions/", "mcp/config.json", "logs/", "uploads/",
    "backups/", "work/", ".venv/", "node_modules/", "frontend/dist/",
    "static/frontend/", "static/vue/", "static/assets/",
)
PRESERVE_SUFFIX = (
    ".bak", ".key", ".pem", ".sqlite", ".sqlite-shm", ".sqlite-wal",
    ".log", ".pid", ".lock", ".tmp",
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _ok(data: dict | None = None, msg: str = "") -> dict:
    return {"ok": True, "msg": msg, "data": data or {}}


def _fail(msg: str, data: dict | None = None) -> dict:
    return {"ok": False, "msg": msg, "data": data or {}}


def load_cfg(base_dir: str) -> dict:
    """读取 config.json 的 update 段并与默认值合并。"""
    cfg = dict(DEFAULT_CFG)
    try:
        with open(os.path.join(base_dir, "config.json"), "r", encoding="utf-8") as f:
            doc = json.load(f)
        sec = doc.get("update") or {}
        if isinstance(sec, dict):
            for k in DEFAULT_CFG:
                if k in sec:
                    cfg[k] = sec[k]
    except Exception:
        pass
    # 分支白名单校验，防止拼进 URL / 命令造成注入
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", str(cfg.get("branch") or "")):
        cfg["branch"] = DEFAULT_CFG["branch"]
    return cfg


def _extract_token(base_dir: str) -> str:
    """从 config.update.token 或 git remote 提取 GitHub token（纯文本优先）。"""
    cfg = load_cfg(base_dir)
    tok = str(cfg.get("token") or "").strip()
    if tok:
        return tok
    url = ""
    # 1) 纯文本读 .git/config（无 git 命令也能工作）
    gcfg = os.path.join(base_dir, ".git", "config")
    try:
        text = open(gcfg, "r", encoding="utf-8", errors="ignore").read()
        for line in text.splitlines():
            s = line.strip()
            if s.lower().startswith("url") and "=" in s and "@" in s:
                candidate = s.split("=", 1)[1].strip()
                if "github.com" in candidate:
                    url = candidate
                    break
    except Exception:
        pass
    # 2) 兜底用 git 命令
    if not url:
        try:
            out = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=base_dir, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if "@" in out:
                url = out
        except Exception:
            pass
    if url and "@" in url:
        try:
            userinfo = urllib.parse.urlparse(url).netloc.rsplit("@", 1)[0]
            if ":" in userinfo:
                return userinfo.split(":", 1)[1]
        except Exception:
            pass
    return ""


def _api_get(base_dir: str, path: str) -> tuple[Any | None, str]:
    """GitHub API GET。返回 (json数据, 错误)。"""
    cfg = load_cfg(base_dir)
    base = str(cfg.get("api_base") or DEFAULT_CFG["api_base"]).rstrip("/")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "bt-framework-updater",
    }
    tok = _extract_token(base_dir)
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(base + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=int(cfg.get("http_timeout") or 10)) as resp:
            return json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        if e.code == 403 and "rate limit" in detail.lower():
            return None, "GitHub API 匿名限流（403），请在 config.json 的 update.token 配置 token"
        return None, f"API HTTP {e.code}: {detail}"
    except Exception as e:
        return None, f"API 请求失败: {e}"


def _head_sha_from_gitdir(base_dir: str) -> str | None:
    """纯文本读 .git/HEAD → ref → sha，无需 git 命令。"""
    gitdir = os.path.join(base_dir, ".git")
    head_file = os.path.join(gitdir, "HEAD")
    if not os.path.isfile(head_file):
        return None
    try:
        head = open(head_file, "r", encoding="utf-8").read().strip()
    except Exception:
        return None
    if not head.startswith("ref:"):
        return head  # detached HEAD
    ref = head[4:].strip()
    ref_file = os.path.join(gitdir, *ref.split("/"))
    if os.path.isfile(ref_file):
        try:
            return open(ref_file, "r", encoding="utf-8").read().strip() or None
        except Exception:
            pass
    packed = os.path.join(gitdir, "packed-refs")
    if os.path.isfile(packed):
        try:
            for line in open(packed, "r", encoding="utf-8"):
                line = line.strip()
                if not line or line[0] in "#^":
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0]
        except Exception:
            pass
    return None


def _current_branch(base_dir: str) -> str | None:
    head_file = os.path.join(base_dir, ".git", "HEAD")
    if not os.path.isfile(head_file):
        return None
    try:
        head = open(head_file, "r", encoding="utf-8").read().strip()
        return head[4:].strip().split("/")[-1] if head.startswith("ref:") else None
    except Exception:
        return None


def _http_get_bytes(url: str, timeout: int, headers: dict | None = None) -> tuple[bytes, str]:
    """通用 HTTPS GET（可能被环境代理影响，失败返回错误）。

    注意：本机 git 全局配置把 https 重写成 ssh、并设了 socks 代理，
    但这些是 git 层配置，不影响 urllib（除非进程环境有 http_proxy 变量）。
    """
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "bt-framework-updater"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), ""
    except Exception as e:
        return b"", str(e)


def _remote_sha_via_inforefs(base_dir: str) -> tuple[str | None, str]:
    """【无 token 首选】git smart HTTP info/refs 拿远端分支 SHA。

    走 https://github.com/<repo>.git/info/refs?service=git-upload-pack，
    这是 GitHub 网页通道，不经过 api.github.com 的 60 次/小时限流，
    也不需要 git 命令、不需要 token。实测匿名直连 HTTP 200 且含 refs/heads/main。
    """
    cfg = load_cfg(base_dir)
    repo, branch = cfg.get("repo"), cfg.get("branch")
    url = f"https://github.com/{repo}.git/info/refs?service=git-upload-pack"
    raw, err = _http_get_bytes(url, int(cfg.get("http_timeout") or 10))
    if err:
        return None, err
    # pkt-line: 4字节十六进制长度 + 载荷；sha 与 ref 之间可能有空格或 NUL
    target = f"refs/heads/{branch}"
    i = 0
    while i + 4 <= len(raw):
        try:
            n = int(raw[i:i + 4], 16)
        except ValueError:
            break
        if n <= 4:
            i += 4
            continue
        line = raw[i + 4:i + n].decode("utf-8", "replace")
        i += n
        for chunk in line.split("\x00"):
            p = chunk.strip()
            if " " not in p:
                continue
            sha, ref = p.split(" ", 1)
            if ref == target and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower()):
                return sha, ""
    return None, "info/refs 中未找到分支 " + target


def _remote_latest(base_dir: str) -> tuple[str | None, str]:
    """取远端 main 分支最新 sha。

    三级降级（都不依赖 git 命令）：
      ① info/refs  — 匿名、无限流、无 token（首选）
      ② GitHub API — 有 token 时更精确；匿名限流时自动跳过
    """
    cfg = load_cfg(base_dir)

    # ① 无 token 也能用的 info/refs
    sha, err1 = _remote_sha_via_inforefs(base_dir)
    if sha:
        return sha, ""

    # ② GitHub API（token 有效时）
    repo, branch = cfg.get("repo"), cfg.get("branch")
    data, err2 = _api_get(base_dir, f"/repos/{repo}/commits?sha={branch}&per_page=1")
    if not err2 and isinstance(data, list) and data:
        return data[0].get("sha", ""), ""

    return None, f"无法获取远端版本（info/refs: {err1}; API: {err2}）"


# ---------------------------------------------------------------------------
# version.json 锚点
# ---------------------------------------------------------------------------
def read_version(base_dir: str) -> dict:
    p = os.path.join(base_dir, VERSION_FILE)
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_version(base_dir: str, data: dict) -> None:
    p = os.path.join(base_dir, VERSION_FILE)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def bootstrap_version(base_dir: str) -> dict:
    """没有 version.json 时生成基线；有则原样返回。首次生成不提示更新。"""
    p = os.path.join(base_dir, VERSION_FILE)
    if os.path.isfile(p):
        existing = read_version(base_dir)
        if existing.get("sha"):
            return _ok({"bootstrapped": False, "version": existing}, "version.json 已存在")

    sha = _head_sha_from_gitdir(base_dir)
    source = "git"
    if not sha:
        # 无 .git（纯 zip 部署）：以远端最新为基线，避免首次就提示
        sha, err = _remote_latest(base_dir)
        source = "remote"
        if err:
            sha = ""
    version = {
        "sha": sha or "unknown",
        "short_sha": (sha or "")[:7],
        "branch": load_cfg(base_dir).get("branch"),
        "source": source,
        "installed_at": _now_iso(),
        "last_check": "",
        "pending_update": None,
    }
    write_version(base_dir, version)
    return _ok({"bootstrapped": True, "version": version},
               "已生成 version.json 基线" + ("" if sha else "（离线，sha=unknown）"))


# ---------------------------------------------------------------------------
# 更新内容展示
# ---------------------------------------------------------------------------
def _build_update_info(base_dir: str, local_sha: str, remote_sha: str) -> dict:
    """产出更新内容（commits / files）。

    两条路：
      ① compare API（需 token；匿名限流时不可用）——能精确给出 ahead_by 与文件数；
      ② commits atom feed（匿名、无限流、无需 token）——降级展示最近的 commit 列表。
    """
    cfg = load_cfg(base_dir)
    repo, branch = cfg.get("repo"), cfg.get("branch")
    info: dict[str, Any] = {
        "from": local_sha[:7],
        "to": remote_sha[:7],
        "remote_sha": remote_sha,
        "ahead_by": None,
        "commits": [],
        "files_changed": 0,
        "changelog_md": "",
        "detail_source": "",
    }

    def _finalize(commits: list, source: str) -> dict:
        info["commits"] = commits
        info["detail_source"] = source
        head = f"发现新版本 {remote_sha[:7]}"
        if info["ahead_by"] is not None:
            head += f"，落后 {info['ahead_by']} 个提交"
        if info["files_changed"]:
            head += f"，涉及 {info['files_changed']} 个文件"
        lines = [head + "："]
        lines += [f"- `{c['sha']}` {c['msg']}" for c in commits[:20]]
        info["changelog_md"] = "\n".join(lines)
        return info

    # ① compare API（token 有效时）
    data, err = _api_get(base_dir, f"/repos/{repo}/compare/{local_sha}...{branch}")
    if not err and isinstance(data, dict):
        info["ahead_by"] = data.get("ahead_by")
        commits = []
        for c in data.get("commits", [])[:50]:
            cm = c.get("commit", {})
            commits.append({
                "sha": c.get("sha", "")[:7],
                "msg": (cm.get("message") or "").splitlines()[0],
                "author": cm.get("author", {}).get("name", ""),
                "date": (cm.get("author", {}).get("date") or "")[:10],
            })
        info["files_changed"] = len(data.get("files", []))
        return _finalize(commits, "compare-api")

    # ② 无 token 降级：commits atom feed（匿名、无限流）
    atom_commits, atom_err = _commits_via_atom(base_dir, local_sha)
    if atom_commits:
        # atom 按时间倒序；遇到本地 sha 即停，得到的条数即近似的 ahead_by
        info["ahead_by"] = len(atom_commits)
        return _finalize(atom_commits, "atom-feed")

    info["detail_source"] = "unavailable"
    info["changelog_md"] = (
        f"发现新提交 {remote_sha[:7]}（更新详情获取失败：{err or atom_err}）"
    )
    return info


def _commits_via_atom(base_dir: str, local_sha: str) -> tuple[list, str]:
    """【无 token 降级】解析 commits/main.atom 拿 commit 列表。

    atom feed 匿名可访问、不计入 API 限流。按时间倒序列出提交；
    遇到本地 sha（或其短 forms）即停止，因此列表长度≈领先提交数（上限20）。
    """
    import xml.etree.ElementTree as ET

    cfg = load_cfg(base_dir)
    repo, branch = cfg.get("repo"), cfg.get("branch")
    url = f"https://github.com/{repo}/commits/{branch}.atom"
    raw, err = _http_get_bytes(url, int(cfg.get("http_timeout") or 10))
    if err:
        return [], err
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        return [], f"atom 解析失败：{e}"
    ns = {"a": "http://www.w3.org/2005/Atom"}
    short = local_sha[:7]
    commits: list[dict] = []
    for e in root.findall("a:entry", ns):
        full_sha = (e.findtext("a:id", "", ns) or "").rsplit("/", 1)[-1]
        if full_sha[:7] == short:
            break  # 到基线了，之后都是本地已有的
        title = " ".join((e.findtext("a:title", "", ns) or "").split())
        updated = (e.findtext("a:updated", "", ns) or "")[:10]
        an = e.find("a:author/a:name", ns)
        author = (an.text or "") if an is not None else ""
        commits.append({"sha": full_sha[:7], "msg": title, "author": author, "date": updated})
    return commits, ""


# ---------------------------------------------------------------------------
# 核心：检测（只读，不安装）
# ---------------------------------------------------------------------------
def _parse_age(iso: str) -> float:
    try:
        return time.time() - datetime.fromisoformat(iso).timestamp()
    except Exception:
        return float("inf")


def check_update(base_dir: str, force: bool = False) -> dict:
    """检测 main 分支是否有更新。只写 version.json 缓存，绝不安装。"""
    cfg = load_cfg(base_dir)
    if not cfg.get("enabled"):
        return _ok({"has_update": False, "disabled": True}, "更新检测已禁用")

    ver = read_version(base_dir)
    if not ver.get("sha"):
        boot = bootstrap_version(base_dir)
        return _ok({"has_update": False, "bootstrapped": True,
                    "version": boot["data"].get("version")}, "首次生成基线，暂不提示更新")

    # 节流：非强制且距上次检查未超间隔 → 返回缓存
    if not force:
        last = ver.get("last_check") or ""
        interval = int(cfg.get("check_interval_min") or 60) * 60
        if last and _parse_age(last) < interval:
            pending = ver.get("pending_update")
            return _ok({"has_update": bool(pending), "pending": pending,
                        "cached": True, "sha": ver.get("sha")}, "使用缓存的检测结果")

    remote_sha, err = _remote_latest(base_dir)
    if err:
        # 网络失败静默降级，不打扰用户
        return _ok({"has_update": False, "error": err, "sha": ver.get("sha")},
                   f"检测跳过：{err}")

    local_sha = ver.get("sha") or ""
    if remote_sha == local_sha or local_sha == "unknown":
        ver["pending_update"] = None
        ver["last_check"] = _now_iso()
        if local_sha == "unknown":
            ver["sha"] = remote_sha
            ver["short_sha"] = remote_sha[:7]
        write_version(base_dir, ver)
        return _ok({"has_update": False, "sha": remote_sha}, "已是最新版本")

    info = _build_update_info(base_dir, local_sha, remote_sha)
    ver["pending_update"] = info
    ver["last_check"] = _now_iso()
    write_version(base_dir, ver)
    return _ok({"has_update": True, "pending": info, "sha": local_sha}, "发现新版本")


def get_status(base_dir: str) -> dict:
    """当前版本 + 待更新情报 + 更新通道，供前端展示。"""
    cfg = load_cfg(base_dir)
    ver = read_version(base_dir)
    return _ok({
        "version": ver,
        "pending": ver.get("pending_update"),
        "has_update": bool(ver.get("pending_update")),
        "channel": _choose_channel(base_dir, cfg),
        "branch": cfg.get("branch"),
        "enabled": cfg.get("enabled"),
    })


# ---------------------------------------------------------------------------
# 通道选择 & 配置保留
# ---------------------------------------------------------------------------
def _git_available(base_dir: str) -> bool:
    if not shutil.which("git"):
        return False
    if not os.path.isdir(os.path.join(base_dir, ".git")):
        return False
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           cwd=base_dir, capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _choose_channel(base_dir: str, cfg: dict) -> str:
    want = str(cfg.get("channel") or "auto")
    if want in ("git", "zip"):
        return want
    # auto：当前分支==目标分支且有 git 才用 git 增量，否则 zip（安全默认）
    if _git_available(base_dir) and _current_branch(base_dir) == cfg.get("branch"):
        return "git"
    return "zip"


def is_preserved(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    # 只精确去除前导 "./"，不能用 lstrip("./)（会误伤 .venv/ / .gitignore 的前导点）
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel:
        return True
    if rel in PRESERVE_EXACT:
        return True
    for p in PRESERVE_PREFIX:
        if rel == p.rstrip("/") or rel.startswith(p):
            return True
    for s in PRESERVE_SUFFIX:
        if rel.endswith(s):
            return True
    if rel.startswith("."):
        return True  # 隐藏目录/文件（.git/.venv/.hermes/.gitignore...）永不覆盖
    return False


# ---------------------------------------------------------------------------
# 回滚点（文件级，供 zip 通道使用）
# ---------------------------------------------------------------------------
def _make_rollback_point(base_dir: str, rel_files: list[str]) -> str:
    """把将被覆盖的文件按相对路径镜像到 backups/_update_rollback_<ts>/。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    root = os.path.join(base_dir, "backups", f"_update_rollback_{ts}")
    manifest = {"created_at": _now_iso(), "overwritten": [], "created": []}
    for rel in rel_files:
        src = os.path.join(base_dir, rel)
        dst = os.path.join(root, rel)
        if os.path.isfile(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            manifest["overwritten"].append(rel)
        else:
            manifest["created"].append(rel)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return os.path.basename(root)


def rollback(base_dir: str, name: str = "") -> dict:
    """回滚到指定更新前快照；name 为空则自动取最新。"""
    bdir = os.path.join(base_dir, "backups")
    if not name:
        candidates = sorted(
            [d for d in os.listdir(bdir) if d.startswith("_update_rollback_")],
            reverse=True,
        ) if os.path.isdir(bdir) else []
        if not candidates:
            return _fail("未找到可回滚的更新快照")
        name = candidates[0]
    root = os.path.join(bdir, name)
    mpath = os.path.join(root, "_manifest.json")
    if not os.path.isfile(mpath):
        return _fail(f"快照清单不存在：{name}")
    try:
        manifest = json.load(open(mpath, "r", encoding="utf-8"))
        for rel in manifest.get("overwritten", []):
            src = os.path.join(root, rel)
            dst = os.path.join(base_dir, rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        for rel in manifest.get("created", []):
            if is_preserved(rel):
                continue
            dst = os.path.join(base_dir, rel)
            if os.path.isfile(dst):
                os.remove(dst)
        ver = read_version(base_dir)
        if ver.get("last_applied_snapshot") == name:
            ver["pending_update"] = None
            write_version(base_dir, ver)
        return _ok({"name": name}, f"已回滚到 {name}")
    except Exception as e:
        return _fail(f"回滚失败：{e}")


# ---------------------------------------------------------------------------
# 通道实现：git 增量
# ---------------------------------------------------------------------------
def _git_cmd(args: list[str], base_dir: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """执行 git 命令，并临时中和本机有害的全局配置。

    实测本机 git 全局有：
      url.git@github.com:.insteadOf=https://github.com/  （把 https 重写成 ssh → 公钥失败）
      http.proxy / https.proxy = socks5://127.0.0.1:7890 （代理不稳时卡死）
    这些是 git 层配置，用 -c 临时覆盖即可，不改用户全局配置。
    """
    cmd = [
        "git",
        "-c", "url.https://github.com/.insteadOf=",   # 清空重写：让 https 保持 https
        "-c", "http.proxy=",                          # 清空代理：让其能用环境/直连
        "-c", "https.proxy=",
    ] + args
    return subprocess.run(cmd, cwd=base_dir, capture_output=True, text=True, timeout=timeout)


def _apply_git(base_dir: str, cfg: dict, remote_sha: str) -> dict:
    branch = cfg.get("branch")
    old_sha = _head_sha_from_gitdir(base_dir) or ""
    # 保护本地代码改动：tracked 文件有改动则拒绝（配置文件本就不 tracked）
    st = _git_cmd(["status", "--porcelain", "--untracked-files=no"], base_dir, timeout=10)
    if st.returncode != 0:
        return _fail(f"git status 失败：{st.stderr[:200]}")
    if st.stdout.strip():
        return _fail("检测到本地已跟踪代码文件有改动，已中止更新以防覆盖。请先提交或还原这些改动。")
    # fetch：直接 fetch 显式 https URL（带可选 token），绕开 remote 的 ssh 重写
    tok = _extract_token(base_dir)
    fetch_url = f"https://x-access-token:{tok}@github.com/{cfg.get('repo')}.git" if tok \
        else f"https://github.com/{cfg.get('repo')}.git"
    fr = _git_cmd(["fetch", fetch_url, f"refs/heads/{branch}"], base_dir, timeout=90)
    if fr.returncode != 0:
        return _fail(f"git fetch 失败（将自动降级 zip）：{fr.stderr[:200]}")
    mr = _git_cmd(["merge", "--ff-only", "FETCH_HEAD"], base_dir, timeout=90)
    if mr.returncode != 0:
        return _fail(f"git merge 失败（将自动降级 zip）：{mr.stderr[:200]}")
    new_sha = _head_sha_from_gitdir(base_dir) or remote_sha
    return _ok({"old_sha": old_sha, "new_sha": new_sha, "method": "git"})


# ---------------------------------------------------------------------------
# 通道实现：zip 覆盖（无 git 命令时也能工作）
# ---------------------------------------------------------------------------
def _download_zip(base_dir: str, cfg: dict) -> tuple[str, str]:
    repo, branch = cfg.get("repo"), cfg.get("branch")
    url = f"{cfg.get('zip_base')}/{repo}/zip/refs/heads/{branch}"
    prefix = str(cfg.get("zip_mirror_prefix") or "").strip()
    if prefix:
        url = prefix.rstrip("/") + "/" + url
    fd, tmp = tempfile.mkstemp(prefix="bt-update-", suffix=".zip")
    os.close(fd)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bt-framework-updater"})
        with urllib.request.urlopen(req, timeout=int(cfg.get("http_timeout") or 10)) as resp, \
                open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        return tmp, ""
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return "", f"zip 下载失败：{e}（可配置 update.zip_mirror_prefix 走镜像）"


def _apply_zip(base_dir: str, cfg: dict, remote_sha: str) -> dict:
    zip_path, err = _download_zip(base_dir, cfg)
    if err:
        return _fail(err)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if not names:
                return _fail("zip 包为空")
            top = names[0].split("/")[0] + "/"
            # 计算要覆盖的相对路径（跳过保留清单与顶层目录外内容）
            targets: dict[str, str] = {}  # rel -> zip name
            for n in names:
                if not n.startswith(top):
                    continue
                rel = n[len(top):]
                if not rel or is_preserved(rel):
                    continue
                targets[rel] = n
            if not targets:
                return _fail("zip 包中没有可更新的代码文件")
            snapshot = _make_rollback_point(base_dir, list(targets))
            written = 0
            for rel, zname in targets.items():
                dst = os.path.join(base_dir, rel)
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with zf.open(zname) as src, open(dst, "wb") as out:
                        shutil.copyfileobj(src, out)
                    written += 1
                except Exception as e:
                    return _fail(f"写入 {rel} 失败：{e}（快照 {snapshot} 可回滚）",
                                 {"snapshot": snapshot})
        return _ok({"written": written, "method": "zip", "snapshot": snapshot,
                    "new_sha": remote_sha})
    except zipfile.BadZipFile:
        return _fail("zip 包损坏或下载到的是 HTML 兜底页，请检查 zip_base/镜像配置")
    except Exception as e:
        return _fail(f"zip 更新失败：{e}")
    finally:
        try:
            os.remove(zip_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 校验：更新后关键 py 文件能否编译
# ---------------------------------------------------------------------------
def _verify(base_dir: str) -> tuple[bool, str]:
    for rel in ("web_server.py", "update_manager.py", "update_routes.py"):
        p = os.path.join(base_dir, rel)
        if not os.path.isfile(p):
            continue
        try:
            compile(open(p, "r", encoding="utf-8").read(), p, "exec")
        except Exception as e:
            return False, f"{rel} 编译失败：{e}"
    return True, ""


# ---------------------------------------------------------------------------
# 核心：执行更新（仅由前端点按钮后调用）
# ---------------------------------------------------------------------------
def apply_update(base_dir: str, channel: str | None = None) -> dict:
    """执行更新。绝不自动调用；由前端「立即更新」按钮触发。"""
    if not UPDATE_LOCK.acquire(blocking=False):
        return _fail("已有更新任务正在执行，请勿重复操作")
    try:
        cfg = load_cfg(base_dir)
        if not cfg.get("enabled"):
            return _fail("更新功能已禁用")
        ver = read_version(base_dir)
        pending = ver.get("pending_update") or {}
        remote_sha = pending.get("remote_sha") or ""
        if not remote_sha:
            chk = check_update(base_dir, force=True)
            if not chk["ok"] or not chk["data"].get("has_update"):
                return _fail("当前没有可用更新")
            remote_sha = chk["data"]["pending"]["remote_sha"]

        chosen = channel or _choose_channel(base_dir, cfg)
        if chosen == "git":
            result = _apply_git(base_dir, cfg, remote_sha)
            if not result["ok"]:
                chosen = "zip"  # git 通道失败自动降级 zip
        if chosen == "zip":
            result = _apply_zip(base_dir, cfg, remote_sha)

        if not result["ok"]:
            return result

        ok, why = _verify(base_dir)
        if not ok:
            snap = result["data"].get("snapshot")
            if snap:
                rollback(base_dir, snap)
            ver = read_version(base_dir)
            ver["pending_update"] = None
            write_version(base_dir, ver)
            return _fail(f"更新后校验失败，已回滚：{why}")

        new_sha = result["data"].get("new_sha") or remote_sha
        ver = read_version(base_dir)
        ver.update({
            "sha": new_sha,
            "short_sha": new_sha[:7],
            "installed_at": _now_iso(),
            "last_check": _now_iso(),
            "last_applied_snapshot": result["data"].get("snapshot", ""),
            "pending_update": None,
        })
        write_version(base_dir, ver)
        return _ok({
            "method": result["data"].get("method"),
            "from": pending.get("from", "")[:7],
            "to": new_sha[:7],
            "need_restart": True,
            "snapshot": result["data"].get("snapshot", ""),
        }, "更新完成，请重启服务使新代码生效")
    finally:
        UPDATE_LOCK.release()
