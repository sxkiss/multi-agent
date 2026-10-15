"""
@input: json, logging, os, re, shutil, threading, time, yaml, base64, zipfile, requests
@output: SkillManager — 技能扫描/安装/卸载/状态管理
@position: Skills layer — 动态技能注册与生命周期管理
@auto-doc: Update header and folder INDEX.md when this file changes
"""
import json
import logging
import os
import re
import shutil
import threading
import time

import yaml

logger = logging.getLogger(__name__)


class Skill:
    def __init__(self, name: str, location: str, description: str, content: str, metadata: dict):
        self.name = name
        self.location = location
        self.description = description
        self.content = content
        self.metadata = metadata

class SkillManager:
    _instance = None
    _instance_lock = threading.Lock()  # P2-24: 双检锁保护

    # 全局 Skills 目录，默认项目根下的 skills/（本项目优先）
    # 注意：set_skills_dir() 会基于 BASE_DIR 把相对路径解析为项目 skills/，
    # 此处默认值确保即使 web_server 未注入也已指向项目目录，避免静默回退到 ~/.claude/skills
    # 环境变量 AI_AGENT_SKILLS_DIR 仍可覆盖
    _PROJECT_SKILLS_DIR = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills"
    )
    SKILLS_DIR = os.environ.get(
        "AI_AGENT_SKILLS_DIR",
        _PROJECT_SKILLS_DIR
    )
    SKILLS_STATE_FILE = os.environ.get(
        "AI_AGENT_SKILLS_STATE_FILE",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills_state.json")
    )

    def __init__(self):
        self._ensure_skills_dir()
        self._state = self._load_state()
        # 三层披露缓存：layer1=描述, layer2=SKILL.md body, layer3=references
        self._cache: dict[str, dict] = {}  # name -> {mtime, description, body, references}
        # P2-41: all() 结果缓存，按目录 mtime 失效（TTL 60s）
        # 避免每次 get()/get_enabled()/all_enabled() 都全量 os.walk
        self._all_cache: list[Skill] | None = None
        self._all_cache_mtime: float = 0.0
        self._all_cache_time: float = 0.0
        self._all_cache_ttl = int(os.environ.get("AI_AGENT_SKILLS_CACHE_TTL", "60"))
        if not os.path.exists(self.skills_dir):
            try:
                os.makedirs(self.skills_dir)
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)

    def _ensure_skills_dir(self):
        if not os.path.exists(self.skills_dir):
            try:
                os.makedirs(self.skills_dir)
            except OSError:
                logger.warning("异常被静默吞掉，已记录", exc_info=True)

    def set_skills_dir(self, dir_path: str) -> None:
        """运行时切换技能目录（由 AgentMain 从 config.json 注入），清缓存使新目录立即生效"""
        if not dir_path:
            return
        # 相对路径基于项目根目录解析，避免依赖进程 CWD
        if not os.path.isabs(dir_path):
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            dir_path = os.path.normpath(os.path.join(project_root, dir_path))
        if not os.path.isdir(dir_path):
            logger.warning("[SkillManager] skills_dir 不存在，忽略: %s", dir_path)
            return
        self._skills_dir = dir_path
        self.invalidate_all_cache()
        logger.info("[SkillManager] skills_dir 切换为: %s", dir_path)

    # 市场源配置（从 config.json 的 skills_market 注入），支持多源合并
    _DEFAULT_MARKET_REPOS = [
        {"owner": "openai", "name": "skills", "path": "skills/.curated", "ref": "main", "enabled": True},
    ]

    # 市场清单本地缓存：文件落盘于项目根，TTL 60 分钟
    # 关键防护：GitHub 错误响应（限流/404/HTML）绝不落盘，也不覆盖旧缓存
    MARKET_CACHE_FILE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills_market_cache.json"
    )
    MARKET_CACHE_TTL = 60 * 60  # 秒

    def set_market_config(self, repos: list[dict] | None) -> None:
        """注入市场仓库列表（config.json -> skills_market）。
        仅保留 enabled=True 且 owner/name 完整的条目。空列表/None 回退到默认源。"""
        if not repos:
            self._market_repos = list(self._DEFAULT_MARKET_REPOS)
            return
        cleaned = []
        for r in repos:
            if not isinstance(r, dict):
                continue
            if not r.get("enabled", False):
                continue
            if not (r.get("owner") and r.get("name")):
                continue
            cleaned.append({
                "owner": r["owner"],
                "name": r["name"],
                "path": str(r.get("path", "")).strip("/"),
                "ref": r.get("ref", "main"),
            })
        self._market_repos = cleaned or list(self._DEFAULT_MARKET_REPOS)
        logger.info("[SkillManager] 市场源配置为 %d 个仓库", len(self._market_repos))

    @property
    def market_repos(self) -> list[dict]:
        return getattr(self, "_market_repos", None) or list(self._DEFAULT_MARKET_REPOS)

    @property
    def skills_dir(self) -> str:
        """优先用运行时注入的目录，回退到类级别默认值"""
        return getattr(self, '_skills_dir', None) or self.SKILLS_DIR

    def _get_cache(self, name: str) -> dict | None:
        """获取缓存的技能数据"""
        return self._cache.get(name)

    def _set_cache(self, name: str, data: dict) -> None:
        """设置缓存的技能数据"""
        self._cache[name] = data

    def _invalidate_cache(self, name: str) -> None:
        """使指定技能的缓存失效"""
        self._cache.pop(name, None)

    def invalidate_all_cache(self) -> None:
        """清空所有缓存"""
        self._cache.clear()
        self._all_cache = None

    @classmethod
    def get_instance(cls):
        # P2-24: 双检锁，防止多线程首次调用时实例化两次
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _load_state(self) -> dict:
        default_state = {"disabled_skills": []}
        if not os.path.exists(self.SKILLS_STATE_FILE):
            return default_state
        try:
            with open(self.SKILLS_STATE_FILE, "r", encoding="utf-8") as f:
                state = yaml.safe_load(f)
            if not isinstance(state, dict):
                return default_state
            disabled = state.get("disabled_skills", [])
            if not isinstance(disabled, list):
                disabled = []
            return {"disabled_skills": [str(name).strip() for name in disabled if str(name).strip()]}
        except Exception:
            return default_state

    def _save_state(self) -> bool:
        try:
            state_dir = os.path.dirname(self.SKILLS_STATE_FILE)
            if state_dir and not os.path.exists(state_dir):
                os.makedirs(state_dir)
            with open(self.SKILLS_STATE_FILE, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._state, f, allow_unicode=True, sort_keys=False)
            return True
        except Exception:
            return False

    def _normalize_names(self, names: list[str]) -> list[str]:
        if not isinstance(names, list):
            return []
        normalized = []
        for name in names:
            if name is None:
                continue
            normalized_name = str(name).strip()
            if normalized_name and normalized_name not in normalized:
                normalized.append(normalized_name)
        return normalized

    def _disabled_name_set(self) -> set:
        disabled_names = self._state.get("disabled_skills", [])
        normalized = self._normalize_names(disabled_names)
        self._state["disabled_skills"] = normalized
        return set(normalized)

    def all(self) -> list[Skill]:
        # P2-41: 按目录 mtime 缓存，TTL 内直接返回避免反复 os.walk
        try:
            dir_mtime = os.path.getmtime(self.skills_dir)
        except OSError:
            dir_mtime = 0.0
        now = time.time()
        if (self._all_cache is not None
                and self._all_cache_mtime == dir_mtime
                and now - self._all_cache_time < self._all_cache_ttl):
            return self._all_cache

        skills = []
        if not os.path.exists(self.skills_dir):
            self._all_cache = skills
            self._all_cache_mtime = dir_mtime
            self._all_cache_time = now
            return skills

        # 递归扫描所有子目录
        # followlinks=True：技能目录常以软链接形式挂载（如 .agents/skills 下
        # 大量指向 .cc-switch/skills 的符号链接），默认 os.walk 不进入软链目录，
        # 会导致这些"链接的技能"无法被加载。
        # 同时用 realpath 集合做循环保护，避免软链接成环导致无限遍历。
        # 注意：不要预置顶层 realpath，否则 os.walk 首层（符号链接路径）的
        # realpath 与之相等会被误判为已访问而整体跳过。
        _seen_real = set()
        skills_dir_real = os.path.realpath(self.skills_dir)
        for root, dirs, files in os.walk(self.skills_dir, followlinks=True):
            root_real = os.path.realpath(root)
            if root_real in _seen_real or root_real == skills_dir_real and root != self.skills_dir:
                dirs[:] = []
                continue
            if root != self.skills_dir:
                _seen_real.add(root_real)
            if 'SKILL.md' in files:
                skill = self._load_skill_from_dir(root)
                if skill:
                    skills.append(skill)

        # 更新缓存
        self._all_cache = skills
        self._all_cache_mtime = dir_mtime
        self._all_cache_time = now
        return skills

    def get(self, name: str) -> Skill | None:
        # 遍历所有 skills 查找匹配的名字
        for skill in self.all():
            if skill.name == name:
                return skill
        return None

    def get_enabled(self, name: str) -> Skill | None:
        skill = self.get(name)
        if not skill:
            return None
        if not self.is_enabled(name):
            return None
        return skill

    def all_enabled(self) -> list[Skill]:
        disabled_names = self._disabled_name_set()
        return [skill for skill in self.all() if skill.name not in disabled_names]

    def is_enabled(self, name: str) -> bool:
        if not self.get(name):
            return False
        disabled_names = self._disabled_name_set()
        return name not in disabled_names

    def set_skill_enabled(self, name: str, enabled: bool) -> dict:
        skill = self.get(name)
        if not skill:
            return {"status": False, "msg": f"技能不存在: {name}"}
        disabled_names = self._disabled_name_set()
        if enabled:
            disabled_names.discard(name)
        else:
            disabled_names.add(name)
        self._state["disabled_skills"] = sorted(disabled_names)
        if not self._save_state():
            return {"status": False, "msg": "保存技能状态失败"}
        return {"status": True, "msg": "设置成功"}

    def set_enabled_skills(self, enabled_names: list[str]) -> dict:
        normalized_enabled = set(self._normalize_names(enabled_names))
        all_skills = self.all()
        all_names = {skill.name for skill in all_skills}
        invalid_names = sorted([name for name in normalized_enabled if name not in all_names])
        final_enabled = sorted([name for name in normalized_enabled if name in all_names])
        disabled_names = sorted(list(all_names - set(final_enabled)))
        self._state["disabled_skills"] = disabled_names
        if not self._save_state():
            return {"status": False, "msg": "保存技能状态失败"}
        return {
            "status": True,
            "msg": "设置成功",
            "enabled_skills": final_enabled,
            "disabled_skills": disabled_names,
            "invalid_skills": invalid_names
        }

    def get_all_skills_info(self) -> list[dict]:
        disabled_names = self._disabled_name_set()
        infos = []
        for skill in self.all():
            infos.append({
                "name": skill.name,
                "description": skill.description,
                "location": skill.location,
                "enabled": skill.name not in disabled_names,
                "metadata": skill.metadata
            })
        return infos

    def _load_skill_from_dir(self, skill_dir: str) -> Skill | None:
        # 查找 SKILL.md
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.exists(skill_md_path):
            return None

        try:
            with open(skill_md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            metadata, body = self._parse_frontmatter(content)
            
            name = metadata.get('name', os.path.basename(skill_dir))
            description = metadata.get('description', '')
            
            return Skill(
                name=name,
                location=skill_md_path,
                description=description,
                content=body,
                metadata=metadata
            )
        except Exception:
            # print(f"Error loading skill from {skill_dir}: {e}")
            return None

    def _parse_frontmatter(self, content: str) -> (dict, str):
        """
        解析 YAML Frontmatter
        格式:
        ---
        key: value
        ---
        body
        """
        frontmatter_regex = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)
        match = frontmatter_regex.match(content)
        
        metadata = {}
        body = content
        
        if match:
            yaml_content = match.group(1)
            body = content[match.end():]
            
            # 使用 PyYAML 解析
            try:
                parsed_yaml = yaml.safe_load(yaml_content)
                if isinstance(parsed_yaml, dict):
                    metadata = parsed_yaml
            except yaml.YAMLError:
                # 解析失败时保留空字典或根据需求处理
                pass
        
        return metadata, body

    def list_files(self, skill_dir: str, limit: int = 50) -> list[str]:
        """
        列出 skill 目录下的文件，排除隐藏文件和 SKILL.md
        """
        files = []
        for root, dirs, filenames in os.walk(skill_dir):
            # 排除隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            
            for filename in filenames:
                if filename == "SKILL.md" or filename.startswith('.'):
                    continue
                
                full_path = os.path.join(root, filename)
                files.append(full_path)
                
                if len(files) >= limit:
                    return files
        return files

    # ==================== 安装 / 卸载 ====================

    _NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
    _MAX_TOTAL_UNCOMPRESSED = 50 * 1024 * 1024   # 50MB
    # 成员数上限：仅统计实际文件条目（目录占位条目不计入），默认 2000，
    # 可通过环境变量 AI_AGENT_SKILL_MAX_MEMBERS 覆盖；
    # 硬上界 _MEMBERS_HARD_CEILING 不可被配置突破（防止配置超大值使校验形同虚设）
    _MEMBERS_HARD_CEILING = 50000
    try:
        _cfg_members = int(os.environ.get("AI_AGENT_SKILL_MAX_MEMBERS", "2000"))
    except (TypeError, ValueError):
        _cfg_members = 2000
    _MAX_MEMBERS = min(max(_cfg_members, 1), _MEMBERS_HARD_CEILING)
    _MAX_SINGLE_FILE = 20 * 1024 * 1024   # 单个文件解压后上限 20MB
    _ZIP_BOMB_RATIO = 1000                # 压缩比异常阈值（file_size / compress_size）

    def install_from_zip(self, zip_bytes: bytes, overwrite: bool = False) -> dict:
        """
        从 ZIP 字节流安装技能包。
        支持两种布局：SKILL.md 位于压缩包根目录，或位于唯一一级子目录内。
        技能名优先取 SKILL.md frontmatter 的 name 字段，否则用子目录名。
        内置 Zip-Slip / 路径穿越 / 体积限制防护。
        """
        import io
        import zipfile as _zipfile

        try:
            zf = _zipfile.ZipFile(io.BytesIO(zip_bytes))
        except Exception as e:
            return {"status": False, "msg": f"无效的 ZIP 文件: {e!s}"}

        names = zf.namelist()
        # 仅统计实际文件条目，目录占位条目（以 / 结尾或 is_dir()）不计入成员数
        file_infos = [zi for zi in zf.infolist()
                      if not (zi.is_dir() or zi.filename.endswith("/"))]
        if not names or not file_infos or len(file_infos) > self._MAX_MEMBERS:
            return {"status": False, "msg": f"压缩包文件数为 {len(file_infos)}，超出限制 (1~{self._MAX_MEMBERS})"}

        total_size = sum(zi.file_size for zi in zf.infolist())
        if total_size > self._MAX_TOTAL_UNCOMPRESSED:
            return {"status": False, "msg": f"解压后总体积 {total_size // 1024 // 1024}MB 超出 50MB 限制"}

        # 单文件解压体积上限 + 压缩比异常检测（防 zip 炸弹）
        for zi in zf.infolist():
            if zi.file_size > self._MAX_SINGLE_FILE:
                return {"status": False, "msg": f"单文件解压后体积 {zi.filename} 超出 20MB 限制"}
            compress_size = zi.compress_size or 0
            if compress_size > 0 and zi.file_size > 0 \
               and zi.file_size / compress_size > self._ZIP_BOMB_RATIO:
                return {"status": False, "msg": f"检测到压缩比异常成员（疑似 zip 炸弹）: {zi.filename}"}
            if compress_size == 0 and zi.file_size > 0:
                # M-8: compress_size=0 会跳过比例检测；此处显式告警，
                # 并依赖下方 total_size / _MAX_SINGLE_FILE 与解压时实时写入累计作为兜底防线。
                logger.warning(
                    "[ZipBombGuard] 成员 %s compress_size=0（file_size=%d），"
                    "无法做压缩比校验，改由总体积+实时累计兜底",
                    zi.filename, zi.file_size,
                )

        # 路径安全校验 + 确定公共前缀
        for n in names:
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
                return {"status": False, "msg": f"发现非法路径成员（疑似路径穿越攻击）: {n}"}

        md_member = None
        prefix = ""
        direct = [n for n in names if not n.endswith("/") and n == "SKILL.md"]
        nested = [n for n in names if not n.endswith("/") and n.count("/") == 1 and n.endswith("/SKILL.md")]
        top_dirs = {n.split("/")[0] for n in names if "/" in n}
        if direct:
            md_member = "SKILL.md"
            prefix = ""
        elif len(nested) >= 1 and len(top_dirs) == 1:
            prefix = list(top_dirs)[0] + "/"
            md_member = prefix + "SKILL.md"
        else:
            return {"status": False, "msg": "未找到 SKILL.md（须位于压缩包根目录或唯一一级子目录内）"}

        # 解析技能名：frontmatter name > 子目录名
        skill_name = ""
        try:
            head = zf.read(md_member).decode("utf-8", errors="replace")[:2048]
            m = re.search(r"^name:\s*['\"]?([A-Za-z0-9_\-]+)['\"]?\s*$", head, re.MULTILINE)
            if m:
                skill_name = m.group(1)
        except Exception:
            logger.warning("未处理的异常", exc_info=True)
        if not skill_name and prefix:
            skill_name = prefix.rstrip("/")
        if not skill_name or not self._NAME_RE.match(skill_name):
            return {"status": False, "msg": f"无法确定合法技能名（frontmatter name 或目录名需匹配 [A-Za-z0-9_-]，当前: {skill_name!r}）"}

        target_dir = os.path.join(self.skills_dir, skill_name)
        if os.path.exists(target_dir):
            if not overwrite:
                return {"status": False, "msg": f"技能已存在: {skill_name}（如需覆盖请勾选覆盖安装）"}
            shutil.rmtree(target_dir, ignore_errors=True)

        os.makedirs(target_dir, exist_ok=True)
        dest_real = os.path.realpath(target_dir)
        extracted = 0
        written_total = 0   # 实际写入字节累计（ZIP 元数据可伪造，此处为运行时兜底防线）
        try:
            for zi in zf.infolist():
                # 目录占位条目跳过；符号链接成员一律拒绝（防止链接逃逸或指向敏感文件）
                if zi.is_dir() or ((zi.external_attr >> 16) & 0o170000) == 0o120000:
                    continue
                rel = zi.filename[len(prefix):] if prefix else zi.filename
                if not rel:
                    continue
                dest_path = os.path.realpath(os.path.join(dest_real, rel))
                if not (dest_path == dest_real or dest_path.startswith(dest_real + os.sep)):
                    return {"status": False, "msg": "成员越界（路径穿越），已中止并清理"}
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with zf.open(zi) as src_f, open(dest_path, "wb") as out_f:
                    while True:
                        chunk = src_f.read(1024 * 1024)
                        if not chunk:
                            break
                        written_total += len(chunk)
                        if written_total > self._MAX_TOTAL_UNCOMPRESSED:
                            raise ValueError(
                                f"实际解压写入总量超出 "
                                f"{self._MAX_TOTAL_UNCOMPRESSED // 1024 // 1024}MB 限制（疑似 zip 炸弹）"
                            )
                        out_f.write(chunk)
                extracted += 1
        except Exception as e:
            shutil.rmtree(target_dir, ignore_errors=True)
            return {"status": False, "msg": f"解压失败已回滚: {e!s}"}

        # 校验最终可被扫描识别
        if not os.path.isfile(os.path.join(target_dir, "SKILL.md")):
            shutil.rmtree(target_dir, ignore_errors=True)
            return {"status": False, "msg": "安装后未找到 SKILL.md，已回滚"}

        # P2-41 bugfix: 安装后清缓存，否则 all() 仍返回旧列表（不含新技能）
        self.invalidate_all_cache()
        return {
            "status": True,
            "msg": f"技能 {skill_name} 安装成功（{extracted} 个文件）",
            "name": skill_name,
            "path": target_dir,
        }

    def uninstall(self, name: str) -> dict:
        """卸载技能：删除其目录（仅允许删除 skills 根的直接子目录），保留禁用状态记录无碍"""
        name = str(name).strip()
        if not name:
            return {"status": False, "msg": "缺少技能名"}
        skill = self.get(name)
        if not skill:
            return {"status": False, "msg": f"技能不存在: {name}"}

        skill_root = os.path.dirname(skill.location)
        skills_root_real = os.path.realpath(self.skills_dir)
        root_real = os.path.realpath(skill_root)
        parent = os.path.dirname(root_real)
        if parent != skills_root_real:
            return {"status": False, "msg": "仅允许卸载位于 skills/ 一级子目录的技能"}
        if root_real == skills_root_real:
            return {"status": False, "msg": "非法目标"}

        shutil.rmtree(root_real, ignore_errors=True)
        if os.path.exists(root_real):
            return {"status": False, "msg": "删除失败，请检查文件权限"}
        # 同步清理禁用状态
        disabled = self._disabled_name_set()
        if name in disabled:
            disabled.discard(name)
            self._state["disabled_skills"] = sorted(disabled)
            self._save_state()
        # P2-41 bugfix: 卸载后清缓存，否则 all() 仍返回旧列表
        self.invalidate_all_cache()
        return {"status": True, "msg": f"技能 {name} 已卸载"}

    # ==================== URL 安装 ====================

    _MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024  # 30MB

    @staticmethod
    def _normalize_skill_source_url(url: str) -> str:
        """
        归一化技能来源 URL：
        - GitHub 仓库页/tree/blob 链接 → codeload zip 直链（默认 main 分支）
        - 其余原样返回
        """
        m = re.match(
            r"^https://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+)"
            r"(?:/(?:tree|blob)/([A-Za-z0-9_./\-]+?))?(?:\.git)?/?$",
            url.strip(),
        )
        if m:
            user, repo, branch = m.group(1), m.group(2), m.group(3)
            branch = branch or "main"
            return f"https://codeload.github.com/{user}/{repo}/zip/refs/heads/{branch}"
        return url.strip()

    @staticmethod
    def _is_private_host(host: str) -> bool:
        """拦截明显的内网/本机地址，防止 SSRF 探测内网"""
        h = (host or "").strip().lower()
        if h in ("localhost", "::1") or h.endswith(".local"):
            return True
        m = re.match(r"^(\d{1,3})(\.\d{1,3}){3}$", h)
        if m:
            parts = [int(x) for x in h.split(".")]
            if parts[0] in (0, 10, 127) or (parts[0] == 172 and 16 <= parts[1] <= 31) \
               or (parts[0] == 192 and parts[1] == 168) or (parts[0] == 169 and parts[1] == 254):
                return True
        return False

    def install_from_url(self, url: str, overwrite: bool = False) -> dict:
        """
        从 URL 下载技能 ZIP 并安装。
        支持：直链 .zip；GitHub 仓库页/tree/blob 链接自动转为 zip 包下载。
        仅允许 http/https，拒绝内网地址，下载上限 30MB。
        """
        import requests as _requests

        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"status": False, "msg": "仅支持 http:// 或 https:// 链接"}

        # 内网地址默认拒绝（防 SSRF）；设置 AI_AGENT_SKILL_ALLOW_PRIVATE=1 可放行内网技能仓库
        allow_private = os.environ.get("AI_AGENT_SKILL_ALLOW_PRIVATE", "").strip().lower() in ("1", "true", "yes")
        try:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
            if not allow_private and self._is_private_host(host):
                return {"status": False, "msg": f"拒绝访问内网/本机地址: {host}（如需允许内网源，设置环境变量 AI_AGENT_SKILL_ALLOW_PRIVATE=1）"}
        except Exception:
            logger.warning("未处理的异常", exc_info=True)
        download_url = self._normalize_skill_source_url(url)

        try:
            resp = _requests.get(download_url, timeout=(10, 60), stream=True,
                                 headers={"User-Agent": "ai-agent-skill-installer"})
        except _requests.Timeout:
            return {"status": False, "msg": "下载超时"}
        except Exception as e:
            return {"status": False, "msg": f"下载失败: {e!s}"}

        if resp.status_code != 200:
            return {"status": False, "msg": f"下载失败，HTTP {resp.status_code}: {download_url}"}

        chunks = []
        total = 0
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > self._MAX_DOWNLOAD_BYTES:
                    return {"status": False, "msg": "文件超过 30MB 下载上限"}
                chunks.append(chunk)
        except Exception as e:
            return {"status": False, "msg": f"下载中断: {e!s}"}

        data = b"".join(chunks)
        if not data:
            return {"status": False, "msg": "下载内容为空"}
        # 简单校验确实是 ZIP（PK 头），避免把 HTML 错误页当包安装
        if not data.startswith(b"PK"):
            return {"status": False, "msg": "链接返回的不是有效的 ZIP 文件（若为 GitHub 页面请使用仓库主页面链接）"}

        result = self.install_from_zip(data, overwrite=overwrite)
        if result.get("status"):
            src_note = "（GitHub 仓库）" if download_url != url.strip() else ""
            result["msg"] = result.get("msg", "安装成功") + f"，来源: {download_url}{src_note}"
        return result

    def install_from_dir(self, src_dir: str, overwrite: bool = False) -> dict:
        """
        从本地目录安装技能（目录须含 SKILL.md 或唯一一级子目录含 SKILL.md）。
        安装目标锁定当前 skills_dir（项目技能目录），与 workspace 隔离。
        """
        src_dir = os.path.realpath(os.path.abspath(str(src_dir).strip()))
        if not os.path.isdir(src_dir):
            return {"status": False, "msg": f"源目录不存在: {src_dir}"}

        # 定位 SKILL.md：根目录或唯一一级子目录
        if os.path.isfile(os.path.join(src_dir, "SKILL.md")):
            skill_root = src_dir
        else:
            sub = [d for d in os.listdir(src_dir)
                   if os.path.isdir(os.path.join(src_dir, d)) and not d.startswith(".")]
            skill_md_dirs = [d for d in sub if os.path.isfile(os.path.join(src_dir, d, "SKILL.md"))]
            if len(skill_md_dirs) == 1:
                skill_root = os.path.join(src_dir, skill_md_dirs[0])
            elif len(skill_md_dirs) == 0:
                return {"status": False, "msg": "未找到 SKILL.md（须位于目录根或唯一一级子目录）"}
            else:
                return {"status": False, "msg": f"发现多个候选技能目录，请指定具体技能目录: {skill_md_dirs}"}

        # 解析技能名
        skill_name = ""
        try:
            head = open(os.path.join(skill_root, "SKILL.md"), "r", encoding="utf-8", errors="replace").read()[:2048]
            m = re.search(r"^name:\s*['\"]?([A-Za-z0-9_\-]+)['\"]?\s*$", head, re.MULTILINE)
            if m:
                skill_name = m.group(1)
        except Exception:
            logger.warning("读取技能名失败", exc_info=True)
        if not skill_name:
            skill_name = os.path.basename(skill_root)
        if not self._NAME_RE.match(skill_name):
            return {"status": False, "msg": f"非法技能名: {skill_name!r}"}

        target_dir = os.path.join(self.skills_dir, skill_name)
        if os.path.exists(target_dir):
            if not overwrite:
                return {"status": False, "msg": f"技能已存在: {skill_name}（如需覆盖请设置 overwrite=true）"}
            shutil.rmtree(target_dir, ignore_errors=True)
        shutil.copytree(skill_root, target_dir)
        self.invalidate_all_cache()
        return {"status": True, "msg": f"技能 {skill_name} 已安装（来源目录: {skill_root}）",
                "name": skill_name, "path": target_dir}

    def fetch_market_list(self) -> dict:
        """
        从配置的市场仓库（config.json -> skills_market，多源）拉取可用技能清单并合并。
        每个仓库条目：{owner, name, path, ref, enabled}
        返回 {status, items:[{name, installed, url, repo}], msg}

        缓存策略：成功结果落盘 skills_market_cache.json（TTL 60min）。
        严格防护：GitHub 错误响应（限流/404/HTML）绝不落盘、绝不覆盖旧缓存；
        读取时校验文件结构合法且源签名匹配，否则忽略并重新拉取。
        """
        import requests as _requests

        # 1) 尝试读取有效缓存（未过期 + 结构合法 + 源签名匹配）
        cached = self._load_market_cache()
        if cached is not None:
            return cached

        repos = self.market_repos
        if not repos:
            return {"status": False, "msg": "未配置任何市场源（skills_market 为空）"}

        installed = {s.name for s in self.all()}
        merged: dict[str, dict] = {}   # name -> item（多源同名去重，先到先得）
        errors = []
        for repo in repos:
            owner, name = repo["owner"], repo["name"]
            path = repo.get("path", "")
            ref = repo.get("ref", "main")
            api_path = f"{path}/" if path else ""
            api_url = f"https://api.github.com/repos/{owner}/{name}/contents/{api_path}?ref={ref}"
            try:
                resp = _requests.get(api_url, timeout=(10, 30),
                                     headers={"User-Agent": "ai-agent-skill-market",
                                               "Accept": "application/vnd.github+json"})
            except _requests.Timeout:
                errors.append(f"{owner}/{name}: 请求超时")
                continue
            except Exception as e:
                errors.append(f"{owner}/{name}: {e!s}")
                continue
            if resp.status_code != 200:
                errors.append(f"{owner}/{name}: HTTP {resp.status_code}")
                continue
            try:
                data = resp.json()
            except Exception:
                errors.append(f"{owner}/{name}: 响应解析失败")
                continue
            if not isinstance(data, list):
                errors.append(f"{owner}/{name}: 格式异常")
                continue
            for item in data:
                if item.get("type") != "dir":
                    continue
                sname = item.get("name", "")
                if not sname or sname in merged:
                    continue
                gh_path = f"{path}/{sname}" if path else sname
                merged[sname] = {
                    "name": sname,
                    "installed": sname in installed,
                    "repo": f"{owner}/{name}",
                    "url": f"https://github.com/{owner}/{name}/tree/{ref}/{gh_path}",
                }

        items = list(merged.values())
        msg = f"市场共 {len(items)} 个技能（来自 {len(repos)} 个源）"
        if errors:
            msg += f"；{len(errors)} 个源失败: {'; '.join(errors)}"

        result = {"status": True, "msg": msg, "items": items}

        # 2) 仅当拉取真实成功（有内容且结构合法）才写入缓存；错误响应不覆盖旧缓存
        self._save_market_cache(result)
        return result

    # ---- 市场清单缓存（本地文件，TTL 60min，强校验防错误数据）----

    def _market_cache_signature(self) -> str:
        """源列表指纹：配置变更（增删/改源）时缓存自动失效"""
        import hashlib
        raw = json.dumps(self.market_repos, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _is_valid_market_item(self, it: object) -> bool:
        """校验单个市场条目结构合法（防缓存到错误/损坏数据）"""
        if not isinstance(it, dict):
            return False
        if not isinstance(it.get("name"), str) or not it["name"]:
            return False
        if not isinstance(it.get("url"), str) or not it["url"].startswith("http"):
            return False
        if not isinstance(it.get("repo"), str) or "/" not in it.get("repo", ""):
            return False
        return True

    def _load_market_cache(self) -> dict | None:
        """读取并校验缓存。任一校验失败返回 None（触发重新拉取）。"""
        try:
            if not os.path.exists(self.MARKET_CACHE_FILE):
                return None
            with open(self.MARKET_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            logger.warning("[market cache] 读取失败，忽略缓存", exc_info=True)
            return None

        # 结构校验
        if not isinstance(data, dict):
            return None
        if data.get("status") is not True:
            return None
        items = data.get("items")
        if not isinstance(items, list) or len(items) == 0:
            return None
        if not all(self._is_valid_market_item(it) for it in items):
            logger.warning("[market cache] 含非法条目，忽略缓存")
            return None
        # 源签名匹配（配置变更后失效）
        if data.get("signature") != self._market_cache_signature():
            logger.info("[market cache] 源签名不匹配，缓存失效")
            return None
        # 时效性校验
        saved_at = data.get("saved_at", 0)
        try:
            saved_at = float(saved_at)
        except (TypeError, ValueError):
            return None
        if time.time() - saved_at > self.MARKET_CACHE_TTL:
            return None
        logger.info("[market cache] 命中有效缓存（%d 个技能）", len(items))
        return data

    def _save_market_cache(self, result: dict) -> None:
        """原子写入缓存。仅当 result 真实成功且结构合法时落盘；失败静默跳过。"""
        if not isinstance(result, dict) or result.get("status") is not True:
            return
        items = result.get("items")
        if not isinstance(items, list) or len(items) == 0:
            return
        if not all(self._is_valid_market_item(it) for it in items):
            logger.warning("[market cache] 跳过写入：结果含非法条目")
            return
        payload = {
            "status": True,
            "msg": result.get("msg", ""),
            "items": items,
            "signature": self._market_cache_signature(),
            "saved_at": time.time(),
        }
        tmp = self.MARKET_CACHE_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.MARKET_CACHE_FILE)  # 原子替换，防半截写入
        except Exception:
            logger.warning("[market cache] 写入失败", exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass

    def generate_skill_summary(self) -> str:
        """生成紧凑的技能发现列表（Codex 式三层披露 Layer 1）"""
        skills = self.all_enabled()
        if not skills:
            return ""

        # 按关键词分类，只保留前 6 个示例
        categories = {}
        for s in skills:
            name = s.name
            desc = s.description[:200] if s.description else ""
            cat = "其他"
            if any(k in name for k in ["seo", "geo", "content", "keyword", "rank", "meta", "schema", "backlink", "serp", "competitor", "domain", "alert", "on-page", "technical-seo", "internal-link", "topic", "refresher"]):
                cat = "SEO/GEO"
            elif any(k in name for k in ["excel", "csv", "chart", "visual", "pie", "bar", "line", "scatter", "stat", "kpi", "trend", "large-file", "parquet"]):
                cat = "数据分析"
            elif any(k in name for k in ["ppt", "pptx", "deck", "slide"]):
                cat = "PPT/演示"
            elif any(k in name for k in ["agent", "workflow", "codex", "skill", "hermes", "openclaw", "memory", "team", "task", "coordination", "reflection", "session", "mac"]):
                cat = "Agent/工作流"
            elif any(k in name for k in ["git", "docker", "k8s", "linux", "bash", "deploy", "ci", "test", "shellcheck", "bats"]):
                cat = "DevOps/运维"
            elif any(k in name for k in ["web", "browser", "playwright", "scrap", "crawl", "web-browse"]):
                cat = "Web/浏览器"
            elif any(k in name for k in ["weixin", "wechat", "tieba", "danmaku", "video", "image", "social", "publishing", "doubao", "yuanbao"]):
                cat = "社媒/多媒体"
            elif any(k in name for k in ["reverse", "pwn", "ida", "radare", "firmware", "pentest", "security", "debug", "edr", "binary", "exploit", "malware", "apk"]):
                cat = "安全/逆向"
            elif any(k in name for k in ["python", "rust", "go", "dotnet", "nodejs", "js", "type", "async"]):
                cat = "编程语言"
            elif any(k in name for k in ["frontend", "react", "angular", "vue", "nextjs", "css", "design", "responsive", "component", "tailwind", "mobile", "ios", "android"]):
                cat = "前端/移动端"
            elif any(k in name for k in ["database", "sql", "postgres", "migration", "cassandra"]):
                cat = "数据库"
            elif any(k in name for k in ["api", "rest", "graphql", "openapi"]):
                cat = "API设计"
            elif any(k in name for k in ["cloud", "aws", "azure", "gcp", "terraform", "helm", "prometheus", "grafana", "cost", "slo", "incident", "postmortem", "on-call"]):
                cat = "云/基础设施"
            elif any(k in name for k in ["sn-"]):
                cat = "SN技能"
            elif any(k in name for k in ["opencode", "ccload", "sub2api", "migrate"]):
                cat = "工具集成"
            categories.setdefault(cat, []).append({"name": name, "desc": desc})

        lines = [f"## 可用技能（共 {len(skills)} 个，按 {len(categories)} 个类别分组）"]
        lines.append("当用户请求涉及特定领域时，使用 `Skills(name=\"技能名\")` 加载该技能的详细工作流。")
        lines.append("")
        for cat, items in sorted(categories.items()):
            sample = items[:6]
            more = f" 等共{len(items)}个" if len(items) > 6 else ""
            parts = []
            for s in sample:
                d = (s['desc'] or "").strip()
                if d:
                    # 截断到 120 字符，避免单行过长；保留触发场景（description 前部通常是用途/触发词）
                    d = d if len(d) <= 120 else d[:120] + "…"
                    parts.append(f"`{s['name']}`（{d}）")
                else:
                    parts.append(f"`{s['name']}`")
            names = ', '.join(parts)
            lines.append(f"- **{cat}**{more}: {names}")
        return "\n".join(lines)

    # ==================== P2-a: 技能包（复用/导出/导入） ====================
    # 技能包 = 一组技能的清单（manifest），可导出为 ZIP 跨项目/跨会话复用。
    # 参照 Octop 的 skill_package 概念：把精选技能打成一个可分享单元。

    PACKAGE_FORMAT = "bt-skill-package/v1"

    def export_package(self, names: list[str], package_name: str = "") -> bytes:
        """把指定技能打包为 ZIP（含 manifest.json）。返回 zip 字节。"""
        import io
        import zipfile
        names = [str(n).strip() for n in (names or []) if str(n).strip()]
        if not names:
            raise ValueError("技能列表为空")
        found = {}
        for nm in names:
            sk = self.get(nm)
            if not sk:
                raise ValueError(f"技能不存在: {nm}")
            found[nm] = sk
        pkg_name = (package_name or "skill-package").strip()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "format": self.PACKAGE_FORMAT,
                "name": pkg_name,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "skills": [],
            }
            for nm, sk in found.items():
                skill_dir = os.path.dirname(sk.location)
                # 技能目录下的所有文件（含 SKILL.md 与附属脚本/参考）
                for root, dirs, files in os.walk(skill_dir):
                    dirs[:] = [d for d in dirs if not d.startswith(".")]
                    for fn in files:
                        if fn.startswith("."):
                            continue
                        full = os.path.join(root, fn)
                        rel = os.path.relpath(full, skill_dir)
                        arc = f"skills/{nm}/{rel}".replace(os.sep, "/")
                        zf.write(full, arc)
                manifest["skills"].append({
                    "name": nm,
                    "description": sk.description,
                    "files": len(self.list_files(skill_dir)),
                })
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        return buf.getvalue()

    def import_package(self, zip_bytes: bytes, overwrite: bool = False) -> dict:
        """导入技能包 ZIP（可含多个技能）。逐个技能安装，返回汇总结果。"""
        import io
        import zipfile
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except Exception as e:
            return {"status": False, "msg": f"不是有效的 ZIP: {e!s}"}
        # 校验 manifest（可选，用于报告包名）
        pkg_name = ""
        try:
            if "manifest.json" in zf.namelist():
                mf = json.loads(zf.read("manifest.json").decode("utf-8"))
                if isinstance(mf, dict):
                    pkg_name = mf.get("name", "")
                    if mf.get("format") and mf["format"] != self.PACKAGE_FORMAT:
                        return {"status": False, "msg": f"不支持的包格式: {mf.get('format')}"}
        except Exception:
            pass

        # 按 skills/<name>/ 分组，逐个技能构造单技能 zip 再交给 install_from_zip，
        # 复用其全部安全校验（Zip-Slip/体积/压缩比/路径穿越）。
        groups: dict[str, list[str]] = {}
        for item in zf.namelist():
            if item == "manifest.json" or item.endswith("/"):
                continue
            parts = item.split("/")
            if len(parts) >= 3 and parts[0] == "skills":
                groups.setdefault(parts[1], []).append(item)
        if not groups:
            # 兼容：包内直接是单技能（根或唯一一级子目录），直接透传
            res = self.install_from_zip(zip_bytes, overwrite=overwrite)
            if isinstance(res, dict):
                res.setdefault("package_name", pkg_name)
            return res

        installed, skipped, errors = [], [], []
        for sk_name, members in groups.items():
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
                for item in members:
                    rel = item[len("skills/" + sk_name + "/"):]
                    if not rel:
                        continue
                    out.writestr(rel, zf.read(item))
            res = self.install_from_zip(buf.getvalue(), overwrite=overwrite)
            if isinstance(res, dict) and res.get("status"):
                installed.append(res.get("name", sk_name))
            elif isinstance(res, dict) and "已存在" in str(res.get("msg", "")):
                skipped.append(sk_name)
            else:
                errors.append(f"{sk_name}: {(res or {}).get('msg', '未知错误')}")

        return {
            "status": bool(installed) or (not errors and not skipped),
            "msg": f"技能包 {pkg_name or ''} 导入完成：安装 {len(installed)}，跳过 {len(skipped)}，失败 {len(errors)}",
            "package_name": pkg_name,
            "installed": installed,
            "skipped": skipped,
            "errors": errors,
        }

    # ==================== P2-b: 技能呈现层（icon/分类/触发词） ====================
    # 从 SKILL.md frontmatter 提取展示元数据，供前端/市场呈现，不影响技能执行。

    _CATEGORY_KEYWORDS = {
        "SEO/GEO": ["seo", "geo", "content", "keyword", "rank", "meta", "schema", "backlink", "serp"],
        "数据分析": ["excel", "csv", "chart", "visual", "stat", "kpi", "trend", "parquet", "pandas"],
        "PPT/演示": ["ppt", "pptx", "deck", "slide", "presentation"],
        "Agent/工作流": ["agent", "workflow", "codex", "skill", "hermes", "memory", "team", "task"],
        "DevOps/运维": ["git", "docker", "k8s", "linux", "bash", "deploy", "ci", "test", "shell"],
        "Web/浏览器": ["web", "browser", "playwright", "scrap", "crawl"],
        "社媒/多媒体": ["weixin", "wechat", "tieba", "video", "image", "social", "doubao", "yuanbao"],
        "安全/逆向": ["reverse", "pwn", "ida", "radare", "firmware", "security", "exploit", "malware"],
        "编程语言": ["python", "rust", "go", "dotnet", "nodejs", "typescript", "async"],
        "前端/移动端": ["frontend", "react", "vue", "nextjs", "css", "mobile", "android", "ios"],
        "数据库": ["database", "sql", "postgres", "migration"],
        "API设计": ["api", "rest", "graphql", "openapi"],
        "云/基础设施": ["cloud", "aws", "azure", "gcp", "terraform", "k8s", "grafana", "cost"],
        "SN技能": ["sn-"],
        "工具集成": ["opencode", "ccload", "sub2api"],
    }

    def _infer_category(self, name: str, description: str = "") -> str:
        blob = f"{name} {description}".lower()
        for cat, kws in self._CATEGORY_KEYWORDS.items():
            if any(k in blob for k in kws):
                return cat
        return "其他"

    def get_skill_presentation(self, name: str) -> dict:
        """返回技能的展示元数据（icon/category/triggers/when_to_use）。"""
        sk = self.get(name)
        if not sk:
            return {}
        md = sk.metadata or {}
        # frontmatter 可显式指定 icon/category/triggers
        icon = str(md.get("icon") or md.get("emoji") or "").strip()
        category = str(md.get("category") or "").strip() or self._infer_category(sk.name, sk.description)
        triggers = md.get("triggers") or md.get("trigger") or []
        if isinstance(triggers, str):
            triggers = [t.strip() for t in triggers.split(",") if t.strip()]
        when_to_use = str(md.get("when_to_use") or "").strip()
        return {
            "name": sk.name,
            "icon": icon,
            "category": category,
            "triggers": list(triggers) if isinstance(triggers, list) else [],
            "when_to_use": when_to_use,
            "description": sk.description,
        }

    def get_all_presentations(self) -> list[dict]:
        """返回所有已启用技能的展示元数据列表。"""
        return [self.get_skill_presentation(s.name) for s in self.all_enabled()]


# 全局单例
skill_manager = SkillManager.get_instance()
