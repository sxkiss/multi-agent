#!/usr/bin/env python
"""
图片（多模态）链路测试（unittest）

存在意义：上游实测支持 vision（模型 auto 能正确识别图片内容），但 BT 侧
有三条链路会把图片吃掉，导致"AG 看不见图"：

  1) chat_start 的 message 只收文本，图片根本没有入口
  2) _filter_file_blocks 把 type="file" 整块删除 —— webfetch 抓的图、
     前端传的图都以 file 块存在，全被静默丢弃
  3) file 块的形状也不符合视觉协议（应为 image_url），即使放行也认不出

另有两个衍生坑：
  4) base64 载荷会被按字符估算 token，一张图误算成十几万 token → 误触发压缩
  5) 图片存在历史里每轮重发 → 上下文膨胀 + 重复计费

运行：
    .venv/bin/python tests/test_image_multimodal.py
"""

import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from chat_client.agent import (  # noqa: E402
    Agent,
    _content_for_token_estimate,
    _drop_image_blocks,
    _file_block_to_image_url,
)


def _filter_file_blocks_helper(content):
    """_filter_file_blocks 是 Agent 的方法；这里跳过重型 __init__ 直接调用"""
    return Agent.__new__(Agent)._filter_file_blocks(content)

PNG_DATA_URL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


class FileBlockToImageUrlTest(unittest.TestCase):
    """图片 file 块必须转成标准 image_url，而不是被丢掉"""

    def test_image_file_block_converted(self):
        got = _file_block_to_image_url({
            "type": "file", "mime": "image/png", "url": PNG_DATA_URL,
        })
        self.assertEqual(got, {"type": "image_url",
                               "image_url": {"url": PNG_DATA_URL}})

    def test_data_image_url_without_mime_still_converted(self):
        """mime 缺失时靠 data:image/ 前缀兜底识别"""
        got = _file_block_to_image_url({"type": "file", "url": PNG_DATA_URL})
        self.assertIsNotNone(got)

    def test_non_image_file_block_returns_none(self):
        """普通附件不应被误当图片发给模型"""
        self.assertIsNone(_file_block_to_image_url({
            "type": "file", "mime": "application/pdf", "url": "data:application/pdf;base64,xx",
        }))

    def test_text_block_returns_none(self):
        self.assertIsNone(_file_block_to_image_url({"type": "text", "text": "hi"}))

    def test_empty_url_returns_none(self):
        self.assertIsNone(_file_block_to_image_url({"type": "file", "mime": "image/png", "url": ""}))


class FilterFileBlocksTest(unittest.TestCase):
    """回归核心 bug：图片曾被 type='file' 过滤整块删除"""

    def test_image_survives_as_image_url(self):
        out = _filter_file_blocks_helper([
            {"type": "text", "text": "看这张图"},
            {"type": "file", "mime": "image/png", "url": PNG_DATA_URL},
        ])
        kinds = [b["type"] for b in out]
        self.assertIn("image_url", kinds, "图片被丢弃了（AG 永远看不见图）")
        self.assertIn("text", kinds)

    def test_existing_image_url_passthrough(self):
        blk = {"type": "image_url", "image_url": {"url": PNG_DATA_URL}}
        self.assertEqual(_filter_file_blocks_helper([blk]), [blk])

    def test_non_image_file_dropped(self):
        out = _filter_file_blocks_helper([
            {"type": "file", "mime": "application/zip", "url": "data:application/zip;base64,xx"},
            {"type": "text", "text": "keep me"},
        ])
        self.assertEqual([b["type"] for b in out], ["text"])

    def test_str_content_unchanged(self):
        self.assertEqual(_filter_file_blocks_helper("纯文本"), "纯文本")


class TokenEstimateTest(unittest.TestCase):
    """base64 不得按字符计入 token，否则一张图就误触发上下文压缩"""

    def test_image_payload_replaced_by_placeholder(self):
        big = "data:image/png;base64," + ("A" * 500000)
        out = _content_for_token_estimate([
            {"type": "text", "text": "hi"},
            {"type": "image_url", "image_url": {"url": big}},
        ])
        joined = str(out)
        self.assertNotIn("A" * 1000, joined, "base64 载荷未从估算中剔除")
        self.assertIn("[image]", joined)

    def test_plain_str_untouched(self):
        self.assertEqual(_content_for_token_estimate("abc"), "abc")


class DropImageBlocksTest(unittest.TestCase):
    """历史轮次的图片降级为占位文本，避免每轮重发 base64"""

    def test_images_dropped_and_noted(self):
        out = _drop_image_blocks([
            {"type": "text", "text": "旧消息"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ])
        self.assertTrue(all(b.get("type") != "image_url" for b in out))
        self.assertTrue(any("图片已省略" in str(b.get("text", "")) for b in out))

    def test_no_images_leaves_content_alone(self):
        src = [{"type": "text", "text": "旧消息"}]
        self.assertEqual(_drop_image_blocks(src), src)


class NormalizeImageBlocksTest(unittest.TestCase):
    """chat_start 的 images 参数归一（入口打通）"""

    def setUp(self):
        import web_server as W
        # 跳过重型 __init__，只取归一方法
        self.srv = W.AgentMain.__new__(W.AgentMain)
        self.W = W

    def test_data_uri_passthrough(self):
        got = self.srv._normalize_image_blocks([PNG_DATA_URL])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["image_url"]["url"], PNG_DATA_URL)

    def test_http_url_passthrough(self):
        got = self.srv._normalize_image_blocks(["https://example.com/a.png"])
        self.assertEqual(len(got), 1)

    def test_dict_form_supported(self):
        got = self.srv._normalize_image_blocks([{"url": PNG_DATA_URL}])
        self.assertEqual(len(got), 1)

    def test_local_path_outside_roots_rejected(self):
        """越界路径必须被拒（防目录穿越读任意文件）"""
        got = self.srv._normalize_image_blocks(["/etc/passwd"])
        self.assertEqual(got, [])

    def test_unsupported_extension_skipped(self):
        got = self.srv._normalize_image_blocks(["sessions/whatever.txt"])
        self.assertEqual(got, [])

    def test_empty_and_garbage_tolerated(self):
        self.assertEqual(self.srv._normalize_image_blocks([]), [])
        self.assertEqual(self.srv._normalize_image_blocks(["", None]), [])


class BuildMessagesKeepsImageTest(unittest.TestCase):
    """端到端：图片必须活着穿过 _build_messages 到 API 载荷"""

    def test_image_survives_into_api_payload(self):
        from chat_client.memory import MemoryManager
        tmp = tempfile.mkdtemp()
        mem = MemoryManager(session_id="t_img", sessions_dir=tmp)
        mem.add_message("user", [
            {"type": "text", "text": "这是什么颜色"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ])
        a = Agent.__new__(Agent)
        a.system_prompt = "你是助手"
        a.memory = mem
        a.context_window_kb = 0
        out = a._build_messages("")
        n = sum(1 for m in out for b in (m.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "image_url")
        self.assertEqual(n, 1, "图片在构建过程中丢失，模型收不到")

    def test_old_turn_image_degraded_latest_kept(self):
        """历史轮次的图降级，最新一轮的图保留"""
        from chat_client.memory import MemoryManager
        tmp = tempfile.mkdtemp()
        mem = MemoryManager(session_id="t_img2", sessions_dir=tmp)
        mem.add_message("user", [
            {"type": "text", "text": "第一张"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ])
        mem.add_message("assistant", "好的")
        mem.add_message("user", [
            {"type": "text", "text": "第二张"},
            {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
        ])
        a = Agent.__new__(Agent)
        a.system_prompt = "你是助手"
        a.memory = mem
        a.context_window_kb = 0
        out = a._build_messages("")
        total = sum(1 for m in out for b in (m.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "image_url")
        self.assertEqual(total, 1, "应只保留最新一轮的图（历史图需降级）")


class SymlinkEscapeTest(unittest.TestCase):
    """软链不得绕过白名单

    _safe_resolve / _safe_resolve_serve 曾用 os.path.abspath —— 它**不解析软链**，
    白名单根内只要存在一个指向外部的软链（如 uploads/ 或 /tmp 下），
    前缀校验就会通过，从而越界读取任意文件。改用 realpath 后软链先被解析，
    真实位置落在白名单外即拒绝。
    """

    def setUp(self):
        import web_server as W
        self.W = W
        self.tmp = tempfile.mkdtemp()  # 默认位于 /tmp，而 /tmp 在白名单内

    def test_safe_resolve_serve_rejects_escaping_symlink(self):
        link = os.path.join(self.tmp, "escape.txt")
        os.symlink("/etc/passwd", link)
        self.assertIsNone(
            self.W._safe_resolve_serve(link),
            "软链绕过白名单成功（应被拒绝）—— 可越界读取任意文件",
        )

    def test_safe_resolve_rejects_escaping_symlink(self):
        link = os.path.join(self.tmp, "escape2.txt")
        os.symlink("/etc/passwd", link)
        self.assertIsNone(self.W._safe_resolve(link), "浏览端点软链越界未被拒")

    def test_image_path_symlink_escape_rejected(self):
        """图片入口同样不得被软链绕过（abspath → realpath）"""
        srv = self.W.AgentMain.__new__(self.W.AgentMain)
        link = os.path.join(self.tmp, "esc.png")
        os.symlink("/etc/passwd", link)
        self.assertEqual(srv._normalize_image_blocks([link]), [],
                         "图片软链越界未被拒")

    def test_normal_path_still_allowed(self):
        """修复不能误伤正常路径（同目录内的普通文件仍可访问）"""
        p = os.path.join(self.tmp, "ok.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("hi")
        self.assertIsNotNone(self.W._safe_resolve_serve(p))


class TokenBudgetImageTest(unittest.TestCase):
    """大图不得撑爆 token 估算（否则误触发压缩/硬截断）

    图片以 base64 存在消息里，若按字符估算，一张 400KB 的图（base64 更长）
    会被算成十几万 token，瞬间超过预算 → 触发压缩甚至硬截断，把图丢掉。
    这里用"压缩一旦被调用就抛异常"的假 client 来证明它没有被触发。
    """

    def test_large_image_does_not_trigger_compression(self):
        from types import SimpleNamespace
        from chat_client.memory import MemoryManager

        def _boom(*a, **k):
            raise AssertionError("大图把 token 估算撑爆，误触发了压缩")

        big = "data:image/png;base64," + ("A" * 400000)
        tmp = tempfile.mkdtemp()
        mem = MemoryManager(session_id="t_big", sessions_dir=tmp)
        mem.add_message("user", [
            {"type": "text", "text": "看看这张图"},
            {"type": "image_url", "image_url": {"url": big}},
        ])

        a = Agent.__new__(Agent)
        a.system_prompt = "你是助手"
        a.memory = mem
        a.context_window_kb = 32          # 32KB 预算，远小于 base64 字符数
        a._compress_round = 0
        a._compress_fail_streak = 0
        a.model_name = "auto"
        a.client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=_boom)))

        out = a._build_messages("")
        n = sum(1 for m in out for b in (m.get("content") or [])
                if isinstance(b, dict) and b.get("type") == "image_url")
        self.assertEqual(n, 1, "图片在构建过程中丢失")


if __name__ == "__main__":
    unittest.main(verbosity=2)
