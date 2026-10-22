"""
@input: json, os, shutil, time, typing, zipfile
@position: Skills layer — 技能打包/导入/导出 + 磁盘库管理
@auto-doc: Update header and folder INDEX.md when this file changes

的轻量版。

bt 此前技能是扁平目录（skills/<name>/），无法跨会话打包迁移。本模块：

- export：把若干技能打包成 zip（含 manifest.json，记录版本/作者/依赖）
- import：解压 zip 并安全校验（防路径穿越/防覆盖敏感文件）后装到 skills/
- list_packages：列出已打包的技能（磁盘库）
- 复用 bt 现有 skills.py 的 install_from_zip 安全校验思路

"""
import json
import logging
import os
import shutil
import tempfile
import time
import typing
import zipfile

logger = logging.getLogger(__name__)

PACKAGE_DIRNAME = os.path.join("skills", "_packages")
MANIFEST_FILENAME = "package_manifest.json"


def _pkg_dir(base_dir: str) -> str:
    return os.path.join(base_dir, PACKAGE_DIRNAME)


def _safe_skill_name(name: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9_\-]", "-", str(name or ""))
    s = s.strip("-")
    if not s or len(s) > 64 or s in {".", ".."}:
        raise ValueError(f"非法技能名: {name!r}")
    if "/" in s or "\\" in s or ".." in s:
        raise ValueError(f"非法技能名: {name!r}")
    return s


def export_skill_package(base_dir: str, skill_names: list[str],
                         author: str = "", version: str = "1.0.0",
                         out_zip: str = "") -> dict:
    """把技能打包成 zip（含 manifest）。返回 {ok, path, skills:[...]}"""
    skills_root = os.path.join(base_dir, "skills")
    names = []
    for n in skill_names or []:
        names.append(_safe_skill_name(n))
    if not names:
        return {"ok": False, "error": "未指定技能"}
    missing = [n for n in names if not os.path.isdir(os.path.join(skills_root, n))]
    if missing:
        return {"ok": False, "error": f"技能不存在: {', '.join(missing)}"}
    if not out_zip:
        os.makedirs(_pkg_dir(base_dir), exist_ok=True)
        out_zip = os.path.join(_pkg_dir(base_dir),
                               f"skills-pack-{int(time.time())}.zip")
    try:
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "name": f"skills-pack-{int(time.time())}",
                "version": version, "author": author,
                "skills": names, "exported_at": time.time(),
                "package_manifest": MANIFEST_FILENAME,
            }
            zf.writestr(MANIFEST_FILENAME, json.dumps(manifest, ensure_ascii=False, indent=2))
            for n in names:
                src = os.path.join(skills_root, n)
                for root, dirs, fnames in os.walk(src):
                    dirs[:] = [d for d in dirs if not d.startswith("__")]
                    for fn in fnames:
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, skills_root)
                        zf.write(full, rel)
    except OSError as e:
        return {"ok": False, "error": f"打包失败: {e}"}
    return {"ok": True, "path": out_zip, "skills": names}


def import_skill_package(base_dir: str, zip_path: str,
                         overwrite: bool = False) -> dict:
    """解压技能包 zip 并安装到 skills/（带安全校验）。返回 {ok, installed, error}"""
    zip_path = os.path.abspath(os.path.expanduser(zip_path or ""))
    if not os.path.isfile(zip_path):
        return {"ok": False, "error": f"压缩包不存在: {zip_path}"}
    if os.path.getsize(zip_path) > 50 * 1024 * 1024:
        return {"ok": False, "error": "压缩包超过 50MB"}
    skills_root = os.path.join(base_dir, "skills")
    tmp = tempfile.mkdtemp(prefix="skill-pkg-")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # 防路径穿越：拒绝 ../ 或绝对路径成员
            for info in zf.infolist():
                name = info.filename.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    return {"ok": False, "error": f"压缩包含非法路径: {name}"}
            zf.extractall(tmp)
        # 解析 manifest（可选）
        mf = os.path.join(tmp, MANIFEST_FILENAME)
        manifest = {}
        if os.path.isfile(mf):
            try:
                manifest = json.load(open(mf, encoding="utf-8"))
            except Exception:
                pass
        # 安装：遍历 tmp 中每个技能目录（含 SKILL.md 或 manifest 声明的）
        declared = manifest.get("skills") or []
        installed = []
        entries = [d for d in os.listdir(tmp)
                   if os.path.isdir(os.path.join(tmp, d)) and not d.startswith(".")]
        for name in declared or entries:
            src = os.path.join(tmp, name)
            if not os.path.isdir(src):
                continue
            safe = _safe_skill_name(name)
            dst = os.path.join(skills_root, safe)
            if os.path.exists(dst) and not overwrite:
                logger.warning("[SkillPkg] %s 已存在，跳过（用 overwrite=True 覆盖）", safe)
                continue
            # 校验：必须含 SKILL.md
            if not os.path.isfile(os.path.join(src, "SKILL.md")):
                logger.warning("[SkillPkg] %s 缺 SKILL.md，跳过", safe)
                continue
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            installed.append(safe)
        if not installed:
            return {"ok": False, "error": "压缩包内没有可安装的技能（需含 SKILL.md）"}
        return {"ok": True, "installed": installed}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "不是有效的 zip 文件"}
    except OSError as e:
        return {"ok": False, "error": f"安装失败: {e}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def list_packages(base_dir: str) -> list[str]:
    d = _pkg_dir(base_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".zip"))
