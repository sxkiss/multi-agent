#!/usr/bin/env python
"""
ViewImage 识图工具测试

对齐当前实现（c7c07fe 之后：ViewImage 改为真正的识图工具，调用多模态 API）：
- 正常图片：校验通过后调用 API，返回识图结果与图片/模型/提示词元信息
- 非法扩展名 / 文件不存在 / 目录 / 敏感路径 / 超大图片均本地报错（中文文案）
- 相对路径按项目根解析

网络一律用 mock 桩替换，避免测试真实调用上游 API。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import requests  # noqa: E402
from PIL import Image  # noqa: E402

import chat_client.tools.view_image as vi  # noqa: E402
from chat_client.tools.view_image import (  # noqa: E402
    IMAGE_EXTENSIONS,
    MAX_IMAGE_SIZE,
    ViewImage,
    _safe_path,
)


def _make_png(path: str, size=(4, 4), color=(255, 0, 0)) -> None:
    img = Image.new("RGB", size, color)
    img.save(path, format="PNG")


class _FakeResponse:
    """requests.post 的最小桩，模拟 OpenAI 兼容的 chat/completions 响应。"""

    def __init__(self, text="FAKE_RECOGNITION", status=200):
        self._text = text
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class ViewImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.png = os.path.join(self.tmp.name, "sample.png")
        _make_png(self.png)
        # 固定 API 配置，避免依赖 config.json 或走多模态 fallback 端点
        for patcher in (
            mock.patch.object(vi, "_load_api_config",
                              return_value=("http://fake.local/v1", "test-key", "fake-vl")),
            mock.patch.object(vi, "_load_mm_config", return_value=("", "", "")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_valid_png_returns_recognition_result(self):
        with mock.patch("chat_client.tools.view_image.requests.post",
                        return_value=_FakeResponse("测试识别结果")) as post:
            result = ViewImage(path=self.png)

        self.assertIn("<toolcall_status>done</toolcall_status>", result)
        self.assertIn("# 识图结果", result)
        self.assertIn("**图片**", result)
        self.assertIn("**模型**", result)
        self.assertIn("测试识别结果", result)

        # 当前实现把图片以 base64 data URL 发给视觉模型
        payload = post.call_args.kwargs["json"]
        image_url = payload["messages"][0]["content"][1]["image_url"]["url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))

    def test_metadata_includes_path_and_model(self):
        with mock.patch("chat_client.tools.view_image.requests.post",
                        return_value=_FakeResponse("OK")):
            result = ViewImage(path=self.png)

        self.assertIn("<toolcall_status>done</toolcall_status>", result)
        self.assertIn("# 识图结果", result)
        self.assertIn("**图片**", result)
        self.assertIn("sample.png", result)
        self.assertIn("**模型**", result)
        self.assertIn("fake-vl", result)
        self.assertIn("**提示词**", result)

    def test_unsupported_extension_rejected(self):
        bad = os.path.join(self.tmp.name, "sample.txt")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("not an image")
        result = ViewImage(path=bad)
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("不支持的图片格式", result)

    def test_missing_file_rejected(self):
        result = ViewImage(path=os.path.join(self.tmp.name, "missing.png"))
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("文件不存在", result)

    def test_directory_rejected(self):
        result = ViewImage(path=self.tmp.name)
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("不是文件", result)

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
        self.assertIn("文件不存在", result)

    def test_image_extensions_cover_common_formats(self):
        # 当前实现支持的扩展名（已收敛，不含 .ico）
        self.assertEqual(
            IMAGE_EXTENSIONS,
            {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif"},
        )

    def test_safe_path_resolves_absolute(self):
        self.assertEqual(_safe_path(self.png), os.path.realpath(self.png))

    def test_safe_path_expands_user(self):
        expanded = _safe_path("~/x.png")
        self.assertEqual(expanded, os.path.realpath(os.path.expanduser("~/x.png")))

    def test_safe_path_relative_to_project_root(self):
        rel = os.path.relpath(self.png, BASE_DIR)
        self.assertEqual(_safe_path(rel), os.path.realpath(self.png))

    def test_corrupt_image_returns_error(self):
        # 损坏/非图片文件应在本地校验阶段被拦截（PIL verify），根本不会发往上游 API。
        with mock.patch("chat_client.tools.view_image.requests.post") as post:
            result = ViewImage(path=self._corrupt_png())
        self.assertIn("<toolcall_status>error</toolcall_status>", result)
        self.assertIn("读取图片失败", result)
        post.assert_not_called()

    def _corrupt_png(self):
        bad = os.path.join(self.tmp.name, "corrupt.png")
        with open(bad, "wb") as f:
            f.write(b"not a real png")
        return bad


class ViewImageSizeLimitTest(unittest.TestCase):
    def test_oversize_rejected(self):
        # 构造一个超过 MAX_IMAGE_SIZE 的假文件（不一定是合法图片，大小限制应先行拦截）
        with tempfile.TemporaryDirectory() as tmp:
            big = os.path.join(tmp, "big.png")
            with open(big, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_SIZE + 1))
            result = ViewImage(path=big)
            self.assertIn("<toolcall_status>error</toolcall_status>", result)
            self.assertIn("图片过大", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
