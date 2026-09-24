#!/usr/bin/env python
"""
小程序数据存储测试（unittest）：mp_store.MpStore + /api/mp/profile 路由

存在意义：签到连击是"读-改-写"逻辑，最容易在边界上出错：
  - 昨天签过 → +1；前天签过 → 重置；同日重复 → 不重复计数
  - 日期必须按固定时区（中国）算，不能跟服务器 TZ 走
  - 客户端伪造 streak/total 必须被忽略（否则可以随便改成 999）

路由层另测身份隔离：token 只能读写自己的数据，客户端传 user_key 无效。
这两件事任一失守都会让"数据落服务端"这个改造变得比本地存储更糟。

运行：
    .venv/bin/python -m unittest tests.test_mp_store -v
或（无需 __init__.py）：
    .venv/bin/python tests/test_mp_store.py
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import mp_store  # noqa: E402
from mp_store import MpStore  # noqa: E402


class MpStoreLogicTest(unittest.TestCase):
    """存储层：签到 / 导入 / 白名单 / 容错"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = MpStore(self.tmp)
        self.uk = "wx_testuser0001"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 基础 ----------

    def test_default_doc_shape(self):
        doc = self.store.read(self.uk)
        self.assertEqual(doc["checkin"]["streak"], 0)
        self.assertEqual(doc["settings"]["font"], "mid")
        self.assertEqual(doc["profile"]["nickname"], "")
        self.assertIn("dates", doc["checkin"])

    def test_first_checkin(self):
        r = self.store.checkin(self.uk)
        self.assertEqual(r["streak"], 1)
        self.assertEqual(r["total"], 1)
        self.assertFalse(r["already"])

    def test_same_day_rejected(self):
        self.store.checkin(self.uk)
        r = self.store.checkin(self.uk)
        self.assertTrue(r["already"], "同日重复签到应返回 already")
        self.assertEqual(r["total"], 1, "累计天数不得重复累加")

    def test_yesterday_extends_streak(self):
        yesterday = (datetime.strptime(self.store.today(), "%Y-%m-%d")
                     - timedelta(days=1)).strftime("%Y-%m-%d")
        self.store.mutate(self.uk, lambda d: d["checkin"].update(
            {"last": yesterday, "streak": 5, "total": 5, "dates": [yesterday]}))
        r = self.store.checkin(self.uk)
        self.assertEqual(r["streak"], 6, "昨天签过应延续连击")
        self.assertEqual(r["total"], 6)

    def test_gap_resets_streak(self):
        old = (datetime.strptime(self.store.today(), "%Y-%m-%d")
               - timedelta(days=3)).strftime("%Y-%m-%d")
        self.store.mutate(self.uk, lambda d: d["checkin"].update(
            {"last": old, "streak": 9, "total": 20, "dates": [old]}))
        r = self.store.checkin(self.uk)
        self.assertEqual(r["streak"], 1, "断签必须重置连击")
        self.assertEqual(r["total"], 21, "累计不受断签影响")

    def test_dates_recorded_and_deduped(self):
        self.store.checkin(self.uk)
        doc = self.store.read(self.uk)
        self.assertIn(self.store.today(), doc["checkin"]["dates"])
        # 手动塞重复日期，归一化后不得重复
        self.store.mutate(self.uk, lambda d: d["checkin"]["dates"].extend(
            [self.store.today(), self.store.today()]))
        doc = self.store.read(self.uk)
        self.assertEqual(doc["checkin"]["dates"].count(self.store.today()), 1)

    # ---------- 白名单与伪造 ----------

    def test_font_whitelist(self):
        doc = self.store.update(self.uk, {"settings": {"font": "large"}})
        self.assertEqual(doc["settings"]["font"], "large")
        doc = self.store.update(self.uk, {"settings": {"font": "huge"}})
        self.assertEqual(doc["settings"]["font"], "large", "非法字号应被忽略")

    def test_profile_stripped_and_truncated(self):
        doc = self.store.update(self.uk, {"profile": {"nickname": "  昵称  "}})
        self.assertEqual(doc["profile"]["nickname"], "昵称")

    def test_client_cannot_forge_checkin(self):
        self.store.update(self.uk, {"checkin": {"streak": 999, "total": 999}})
        doc = self.store.read(self.uk)
        self.assertNotEqual(doc["checkin"]["streak"], 999, "客户端不得直接改签到天数")

    # ---------- 导入迁移 ----------

    def test_import_fills_empty_server(self):
        doc = self.store.import_data("wx_newbie", {
            "checkin": {"last": "2026-09-20", "streak": 7, "total": 23,
                        "dates": ["2026-09-19", "2026-09-20"]},
            "profile": {"nickname": "老用户", "avatar": "http://x/a.png"},
            "settings": {"font": "small"},
            "stats": {"first_seen": "2026-09-01"},
        })
        self.assertEqual(doc["checkin"]["streak"], 7)
        self.assertEqual(doc["checkin"]["total"], 23)
        self.assertEqual(doc["profile"]["nickname"], "老用户")
        self.assertEqual(doc["settings"]["font"], "small")
        self.assertEqual(doc["stats"]["first_seen"], "2026-09-01")

    def test_import_never_overwrites_existing(self):
        self.store.checkin(self.uk)
        before = self.store.read(self.uk)["checkin"]["total"]
        self.store.import_data(self.uk, {
            "checkin": {"last": "2020-01-01", "streak": 100, "total": 100,
                        "dates": ["2020-01-01"]}})
        doc = self.store.read(self.uk)
        self.assertEqual(doc["checkin"]["total"], before, "已有服务端记录不应被覆盖")

    def test_import_rejects_bad_dates(self):
        doc = self.store.import_data("wx_bad", {
            "checkin": {"last": "not-a-date", "streak": 5,
                        "dates": ["2026-13-45", "good-date", "2026-09-01"]}})
        self.assertEqual(doc["checkin"]["last"], "", "非法 last 应被拒")
        self.assertEqual(doc["checkin"]["dates"], ["2026-09-01"], "非法日期应被过滤")

    def test_import_total_not_below_dates(self):
        doc = self.store.import_data("wx_partial", {
            "checkin": {"last": "2026-09-10", "streak": 2, "total": 1,
                        "dates": ["2026-09-09", "2026-09-10"]}})
        self.assertGreaterEqual(doc["checkin"]["total"], 2, "累计不得少于日期条数")

    # ---------- 统计与容错 ----------

    def test_touch_visit_used_days(self):
        doc = self.store.touch_visit("wx_fresh")
        self.assertEqual(doc["stats"]["used_days"], 1)
        self.assertTrue(doc["stats"]["first_seen"])

    def test_corrupt_json_falls_back(self):
        user_dir = os.path.join(self.tmp, "wx_broken")
        os.makedirs(user_dir, exist_ok=True)
        with open(os.path.join(user_dir, "profile.json"), "w", encoding="utf-8") as f:
            f.write("{坏掉的")
        doc = self.store.read("wx_broken")
        self.assertEqual(doc["checkin"]["streak"], 0, "损坏文件应回退默认值")
        r = self.store.checkin("wx_broken")
        self.assertEqual(r["streak"], 1, "损坏后可正常签到")

    def test_user_key_path_traversal_cleaned(self):
        # 试图穿越：user_key 必须被清洗到 root 之内
        self.store.checkin("../../etc/passwd")
        for name in os.listdir(self.tmp):
            self.assertNotIn("/", name)
            self.assertNotIn("..", name, "清洗后不得残留 ..")

    def test_atomic_write_leaves_no_tmp(self):
        self.store.checkin(self.uk)
        leftovers = [f for f in os.listdir(os.path.join(self.tmp, self.uk))
                     if f.endswith(".tmp")]
        self.assertEqual(leftovers, [], "原子写不应残留临时文件")


class MpStoreRouteGuardTest(unittest.TestCase):
    """路由层：身份来自 token，客户端无法指定 user_key"""

    def test_user_key_requires_bearer(self):
        """无 token / 非 wx 主体 → 拒绝，绝不接受客户端传入的 user_key"""
        import asyncio
        import web_server as W

        class FakeReq:
            def __init__(self, headers=None, query=None):
                self.headers = headers or {}
                self.query_params = query or {}

            async def json(self):
                return {}

        # 无 Authorization 头
        self.assertEqual(W._mp_user_key(FakeReq()), "")
        # 空 Bearer
        self.assertEqual(W._mp_user_key(FakeReq({"authorization": "Bearer "})), "")

    def test_user_key_ignores_client_supplied_key(self):
        """即使客户端传 user_key 参数，也必须走 token 解析（返回空）"""
        import web_server as W

        class FakeReq:
            def __init__(self):
                self.headers = {}
                self.query_params = {"user_key": "wx_victim"}

            async def json(self):
                return {"user_key": "wx_victim"}

        self.assertEqual(W._mp_user_key(FakeReq()), "",
                         "身份只能来自 token，客户端传什么都不能穿透")

    def test_stable_session_mapping(self):
        """同一 openid 派生的 user_key 必须稳定（跨设备续用同一份数据）"""
        from auth import wx_session_id
        a = wx_session_id("oABC123")
        b = wx_session_id("oABC123")
        c = wx_session_id("oXYZ999")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertTrue(a.startswith("wx_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
