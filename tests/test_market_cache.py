"""市场缓存测试：内存+落盘/跨重启/过期清理"""
import json
import os
import sys
import tempfile
import time
import unittest

# 在导入前替换缓存目录，避免污染真实 market_cache/
import chat_client.tools.market as _m
_orig_cache_dir = None


class MarketCacheTest(unittest.TestCase):
    def setUp(self):
        global _orig_cache_dir
        self._tmpdir = tempfile.mkdtemp()
        _orig_cache_dir = _m._MARKET_CACHE_DIR
        _m._MARKET_CACHE_DIR = self._tmpdir
        _m._market_cache_state.clear()

    def tearDown(self):
        global _orig_cache_dir
        if _orig_cache_dir is not None:
            _m._MARKET_CACHE_DIR = _orig_cache_dir
        else:
            delattr(_m, '_MARKET_CACHE_DIR')

    def test_save_and_load(self):
        _m._market_cache_save("k1", {"a": 1})
        self.assertEqual(_m._market_cache_load("k1"), {"a": 1})

    def test_not_exist_returns_none(self):
        self.assertIsNone(_m._market_cache_load("nonexistent"))

    def test_disk_persistence(self):
        # 保存后清空内存，验证磁盘回读
        _m._market_cache_save("k2", {"x": 2})
        _m._market_cache_state.clear()
        self.assertEqual(_m._market_cache_load("k2"), {"x": 2})

    def test_disk_file_structure(self):
        _m._market_cache_save("k3", {"data": "v"})
        path = os.path.join(self._tmpdir, "k3.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            d = json.load(f)
        self.assertEqual(d["data"], {"data": "v"})
        self.assertIn("saved_at", d)

    def test_ttl_expiry(self):
        _m._market_cache_save("k4", {"val": 42})
        # 手动让磁盘记录的时间过期
        path = os.path.join(self._tmpdir, "k4.json")
        with open(path) as f:
            d = json.load(f)
        d["saved_at"] = time.time() - 7200  # 2小时前
        with open(path, "w") as f:
            json.dump(d, f)
        # 同时清空内存确保走磁盘路径
        _m._market_cache_state.clear()
        self.assertIsNone(_m._market_cache_load("k4"))
        # 再次保存则正常
        _m._market_cache_save("k4", {"val": 42})
        self.assertEqual(_m._market_cache_load("k4"), {"val": 42})

    def test_empty_string_keys(self):
        self.assertIsNone(_m._market_cache_load(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
