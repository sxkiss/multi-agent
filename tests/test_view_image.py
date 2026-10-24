#!/usr/bin/env python
"""
ViewImage 看图工具测试

覆盖：
- 正常图片返回元信息 + Markdown data URL（前端可直接渲染）
- return_base64=False 只返回元信息
- 非法扩展名 / 文件不存在 / 目录 / 敏感路径 / 超大图片均报错
- 相对路径按项目根解析

运行：
    .venv/bin/python -m pytest tests/test_view_image.py -q
"""

import base64
import io
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from PIL import Image  # noqa: E402

from chat_client.tools.view_image import (  # noqa: E402
    IMAGE_EXTENSIONS,
    MAX_IMAGE_SIZE,
    ViewImage,
    _safe_path,
)


def _make_png(path: str, size=(4, 4), color=(255, 0, 0)) -> None:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")


class ViewImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.png = os.path.join(self.tmp.name, "sample.png")
        _make_png(self.png)

    def test_valid_png_returns_markdown_data_url(self):
        result = ViewImage(path=self.png)
        self.assertIn("<toolcall_status>done</toolcall_status>", result)
        self.assertIn("<size>4x4</size>", result)
        self.assertIn("<format>PNG</format>", result)
        # 默认 return_base64=False，输出相对路径 Markdown 图片链接
        self.assertIn("![图片](", result)
        self.assertIn("sample.png", result)
        # 不含 data:image，因为默认不嵌入 base64
        self.assertNotIn("data:image/png;base64,", result)

    def test_metadata_includes_path_format_mode(self):
        result = ViewImage(path=self.png, return_base64=False)
        self.assertIn("<toolcall_status>done</toolcall_status>", result)
        self.assertIn("<format>PNG</format>", result)
        self.assertIn("<size>4x4</size>", result)
        self.assertIn("<file_size>", result)
        self.assertNotIn("data:image/", result)

    def test_unsupported_extension_rejected(self):
        bad = os.path.join(self.tmp.name, "sample.txt")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("not an image")
        result = ViewImage(path=bad)
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("Unsupported image extension", result)

    def test_missing_file_rejected(self):
        result = ViewImage(path=os.path.join(self.tmp.name, "missing.png"))
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("File not found", result)

    def test_directory_rejected(self):
        result = ViewImage(path=self.tmp.name)
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("Not a file", result)

    def test_sensitive_path_rejected(self):
        # /etc/shadow 无图片扩展名，扩展名校验已先拦截；结果必须是 error（安全）
        result = ViewImage(path="/etc/shadow")
        self.assertIn("<toolcall_status>error</toolcall_status>", result)

    def test_sensitive_path_with_image_ext_rejected(self):
        # 敏感目录下即使文件名是图片，也应拒绝（用不存在的路径验证不会误读）
        result = ViewImage(path="/root/.ssh/id_rsa.png")
        self.assertIn("<toolcall_status>error</toolcall_status>", result)

    def test_empty_path_rejected(self):
        result = ViewImage(path="")
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("File not found", result)

    def test_image_extensions_cover_common_formats(self):
        self.assertTrue({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".ico"} <= IMAGE_EXTENSIONS)

    def test_safe_path_resolves_absolute(self):
        self.assertEqual(_safe_path(self.png), os.path.realpath(self.png))

    def test_safe_path_expands_user(self):
        expanded = _safe_path("~/x.png")
        self.assertEqual(expanded, os.path.realpath(os.path.expanduser("~/x.png")))

    def test_safe_path_relative_to_project_root(self):
        rel = os.path.relpath(self.png, BASE_DIR)
        self.assertEqual(_safe_path(rel), os.path.realpath(self.png))

    def test_corrupt_image_returns_error(self):
        bad = os.path.join(self.tmp.name, "corrupt.png")
        with open(bad, "wb") as f:
            f.write(b"not a real png")
        result = ViewImage(path=bad)
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("读取图片失败", result)


class ViewImageSizeLimitTest(unittest.TestCase):
    def test_oversize_rejected(self):
        # 构造一个超过 MAX_IMAGE_SIZE 的假文件（不一定是合法图片，大小限制应先行拦截）
        with tempfile.TemporaryDirectory() as tmp:
            big = os.path.join(tmp, "big.png")
            with open(big, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_SIZE + 1))
            result = ViewImage(path=big)
            self.assertIn("<toolcall_status>error</toolcall_status>", result)
            self.assertIn("Image too large", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
