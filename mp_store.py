# mp_store.py — 小程序用户数据存储（服务端权威）
#
# 存在意义：
#   小程序本地存储（wx.setStorageSync）在清缓存 / 换设备 / 卸载时会丢失。
#   签到连击这类有积累价值的数据必须落服务端；本地存储退化为离线缓存。
#
# 身份锚点：
#   token 的 sub 形如 "wx:<openid>"，由 auth.wx_session_id() 派生出
#   wx_<sha256(openid)[:24]>，与会话 ID、用户工作目录用的是同一把钥匙。
#   clawbot（微信侧）后续只要能把用户映射到同一个 user_key，即可共享
#   同一份数据与同一条会话 —— 这是两边共享的接缝点。
#
# 存储位置：workspace/users/<user_key>/profile.json
#   与用户生成的文件同目录，天然按微信用户隔离，无需额外权限体系。

import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

try:
    import fcntl  # 仅 POSIX 可用；Windows 下退化为无锁（本项目部署在 Linux）
except ImportError:  # pragma: no cover
    fcntl = None

import logging

logger = logging.getLogger(__name__)

# 签到按固定时区计算：服务器时区一旦变化，跨日判定就会错位。
# 用户群在中国，锁 UTC+8；不依赖运行环境 TZ。
CN_TZ = timezone(timedelta(hours=8))

_USER_KEY_RE = re.compile(r"[^0-9A-Za-z_.-]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_FONTS = {"small", "mid", "large"}

DEFAULT_DOC: Dict[str, Any] = {
    "version": 1,
    "profile": {"nickname": "", "avatar": "", "updated_at": 0},
    "checkin": {"last": "", "streak": 0, "total": 0, "dates": []},
    "settings": {"font": "mid"},
    "stats": {"first_seen": "", "used_days": 0},
    "updated_at": 0,
}

# 允许前端直接写的白名单字段（其余字段服务端自管，不接受外部写入）
_WRITABLE = {
    "profile": {"nickname", "avatar"},
    "settings": {"font"},
}

_MAX_DATES = 400          # 签到日期留存上限，防止无限增长
_MAX_STR = 512            # 字符串字段长度上限


def _clean_user_key(user_key: str) -> str:
    """清洗用户键：绝不允许出现路径分隔符或 `..` 片段，防目录穿越。

    路由层的 key 来自 wx_session_id() 天然安全，但这里是防御性边界：
    若清洗不彻底（如放行 ".."），一旦上游调用方换了来源就会直接逃出
    workspace/users 根目录。
    """
    s = _USER_KEY_RE.sub("_", str(user_key or ""))[:64]
    # ".." 本身由正则放行（点号是合法字符），但作为路径片段会指向上级目录
    while ".." in s:
        s = s.replace("..", "_")
    s = s.strip(".")          # 首尾孤点同样无意义
    return s or "default"


def _clean_date(value: Any) -> str:
    """校验 YYYY-MM-DD 日期串；非法返回空串。

    日期会被用来算连击天数，格式不对会导致 strptime 抛错或算出荒谬结果，
    故一律先校验再入库。
    """
    s = str(value or "").strip()
    if not _DATE_RE.match(s):
        return ""
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return ""
    return s


class MpStore:
    """按用户为粒度的 JSON 文档存储。

    并发安全：读-改-写全程持文件锁（checkin 场景必须串行，否则多设备
    同时签到会互相覆盖）。写入走临时文件 + os.replace 原子替换，避免
    进程被杀时留下半截文件。
    """

    def __init__(self, root_dir: str):
        self.root = root_dir
        os.makedirs(self.root, exist_ok=True)

    # ---------- 路径 ----------

    def _doc_path(self, user_key: str) -> str:
        return os.path.join(self.root, _clean_user_key(user_key), "profile.json")

    def _lock_path(self, user_key: str) -> str:
        return self._doc_path(user_key) + ".lock"

    # ---------- 读写 ----------

    def read(self, user_key: str) -> Dict[str, Any]:
        """读取文档；不存在或损坏时返回默认文档（绝不抛给调用方）。"""
        path = self._doc_path(user_key)
        if not os.path.exists(path):
            return json.loads(json.dumps(DEFAULT_DOC))
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("doc not a dict")
        except Exception:
            logger.warning("mp_store 读取失败，回退默认值: %s", path, exc_info=True)
            return json.loads(json.dumps(DEFAULT_DOC))
        return self._normalize(data)

    def _normalize(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """补齐缺失字段并夹紧类型：前端拿到的数据结构始终完整。"""
        out = json.loads(json.dumps(DEFAULT_DOC))
        for section in ("profile", "checkin", "settings", "stats"):
            got = data.get(section)
            if isinstance(got, dict):
                out[section].update(got)
        ck = out["checkin"]
        ck["last"] = str(ck.get("last") or "")
        ck["streak"] = max(0, min(int(ck.get("streak") or 0), 99999))
        ck["total"] = max(0, min(int(ck.get("total") or 0), 999999))
        # 去重并保序：重复日期会让日历统计出现重影，且 dates 是签到次数的事实来源
        dates = ck.get("dates")
        clean: list = []
        if isinstance(dates, list):
            for d in dates:
                s = str(d)
                if s and s not in clean:
                    clean.append(s)
        ck["dates"] = clean[-_MAX_DATES:]
        ck["total"] = max(ck["total"], len(ck["dates"]))
        st = out["stats"]
        st["first_seen"] = str(st.get("first_seen") or "")
        st["used_days"] = max(0, int(st.get("used_days") or 0))
        return out

    def _write_locked(self, user_key: str, doc: Dict[str, Any]) -> None:
        """在持锁状态下落盘（调用方必须先拿到锁）。"""
        path = self._doc_path(user_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc["updated_at"] = int(time.time())
        # 原子写：同目录临时文件 + replace，避免半截文件
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- 事务 ----------

    def mutate(self, user_key: str, fn) -> Dict[str, Any]:
        """读-改-写事务：fn(doc) 内修改文档，全程持锁。

        fn 抛异常时不落盘，直接向上抛出（调用方转成错误响应）。
        """
        lock_path = self._lock_path(user_key)
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "a+") as lock_f:
            if fcntl is not None:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                doc = self.read(user_key)
                fn(doc)
                self._write_locked(user_key, doc)
                return doc
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)

    # ---------- 业务 ----------

    def checkin(self, user_key: str) -> Dict[str, Any]:
        """签到。返回 {ok, doc, streak, total, already}。

        连续判定：上次签到是昨天 → streak+1；否则重置为 1。
        同一天重复签到返回 already=True，不重复计数。
        """
        today = self.today()
        state = {}

        def _fn(doc: Dict[str, Any]) -> None:
            ck = doc["checkin"]
            if ck.get("last") == today:
                state["already"] = True
                return
            last = ck.get("last") or ""
            if last:
                try:
                    gap = (datetime.strptime(today, "%Y-%m-%d").date()
                           - datetime.strptime(last, "%Y-%m-%d").date()).days
                except ValueError:
                    gap = -1
            else:
                gap = -1
            ck["streak"] = int(ck.get("streak") or 0) + 1 if gap == 1 else 1
            ck["total"] = int(ck.get("total") or 0) + 1
            ck["last"] = today
            dates = ck.get("dates") or []
            if today not in dates:
                dates.append(today)
            ck["dates"] = dates[-_MAX_DATES:]
            state["already"] = False

        doc = self.mutate(user_key, _fn)
        ck = doc["checkin"]
        return {
            "ok": True,
            "already": bool(state.get("already")),
            "streak": ck["streak"],
            "total": ck["total"],
            "last": ck["last"],
            "doc": doc,
        }

    def touch_visit(self, user_key: str) -> Dict[str, Any]:
        """刷新首次/最近访问，返回使用天数。"""
        today = self.today()

        def _fn(doc: Dict[str, Any]) -> None:
            st = doc["stats"]
            if not st.get("first_seen"):
                st["first_seen"] = today
            st["used_days"] = self._days_between(st["first_seen"], today) + 1

        doc = self.mutate(user_key, _fn)
        return doc

    def update(self, user_key: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """局部更新白名单字段（profile.nickname/avatar、settings.font）。

        只接受白名单字段：签到/统计由服务端根据日期自算，客户端传入一律忽略，
        否则连击天数可以被前端随意伪造成 999。
        """
        def _fn(doc: Dict[str, Any]) -> None:
            for section, fields in _WRITABLE.items():
                got = patch.get(section)
                if not isinstance(got, dict):
                    continue
                for field in fields:
                    if field not in got:
                        continue
                    val = got[field]
                    if isinstance(val, str):
                        val = val.strip()[:_MAX_STR]
                    # 字号只接受已知档位：非法值会让前端取不到对应像素，排版崩坏
                    if section == "settings" and field == "font" and val not in _FONTS:
                        continue
                    doc[section][field] = val
                    if section == "profile":
                        doc[section]["updated_at"] = int(time.time())

        return self.mutate(user_key, _fn)

    def import_data(self, user_key: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """首次迁移：把客户端本地数据并入服务端，只填空、不覆盖。

        场景：老用户本机已有签到记录，上线服务端存储时若直接以服务端为准，
        这些记录会"一夜清零"。故首次同步时由前端把本地数据传上来，仅当
        服务端对应字段为空时写入；此后服务端即为唯一权威。
        """
        def _fn(doc: Dict[str, Any]) -> None:
            ck = doc["checkin"]
            src_ck = data.get("checkin") if isinstance(data.get("checkin"), dict) else {}
            if not ck.get("last") and not ck.get("dates") and not ck.get("total"):
                # last 与 dates 分别校验：last 非法（或被伪造）不应连坐丢弃整批日期，
                # 历史记录能救回多少算多少。
                dates = src_ck.get("dates")
                clean = []
                if isinstance(dates, list):
                    for d in dates[:_MAX_DATES]:
                        d = _clean_date(d)
                        if d and d not in clean:
                            clean.append(d)
                last = _clean_date(src_ck.get("last"))
                if last and last not in clean:
                    clean.append(last)
                clean = sorted(clean)[-_MAX_DATES:]
                if clean or last:
                    streak = max(0, min(int(src_ck.get("streak") or 0), 9999))
                    total = max(0, min(int(src_ck.get("total") or 0), 99999))
                    ck["last"] = last
                    ck["streak"] = streak
                    ck["total"] = max(total, len(clean))
                    ck["dates"] = clean

            prof = doc["profile"]
            src_p = data.get("profile") if isinstance(data.get("profile"), dict) else {}
            if not prof.get("nickname") and isinstance(src_p.get("nickname"), str):
                prof["nickname"] = src_p["nickname"].strip()[:_MAX_STR]
            if not prof.get("avatar") and isinstance(src_p.get("avatar"), str):
                prof["avatar"] = src_p["avatar"].strip()[:_MAX_STR]

            st = doc["settings"]
            src_s = data.get("settings") if isinstance(data.get("settings"), dict) else {}
            # 仅当服务端仍是默认值时采纳本地字号，避免覆盖其它设备的改动
            if st.get("font") == DEFAULT_DOC["settings"]["font"] and src_s.get("font") in _FONTS:
                st["font"] = src_s["font"]

            stats = doc["stats"]
            src_st = data.get("stats") if isinstance(data.get("stats"), dict) else {}
            first = _clean_date(src_st.get("first_seen"))
            if first and (not stats.get("first_seen") or first < stats["first_seen"]):
                stats["first_seen"] = first
                stats["used_days"] = self._days_between(first, self.today()) + 1

        return self.mutate(user_key, _fn)

    # ---------- 工具 ----------

    @staticmethod
    def today() -> str:
        return datetime.now(CN_TZ).strftime("%Y-%m-%d")

    @staticmethod
    def _days_between(a: str, b: str) -> int:
        try:
            da = datetime.strptime(a, "%Y-%m-%d").date()
            db = datetime.strptime(b, "%Y-%m-%d").date()
            return (db - da).days
        except (ValueError, TypeError):
            return 0
