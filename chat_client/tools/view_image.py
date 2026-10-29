"""
@input: base64, io, json, logging, mimetypes, os, requests
@output: ViewImage 工具 — 识图工具，调用多模态API识别图片内容
@position: Tools layer — 图片/多模态，支持视觉理解任务

设计说明：
- 模型需要理解图片内容时（截图分析、OCR、图像描述等），调用本工具。
- 内部调用标准OpenAI兼容API（/v1/chat/completions），发送图片+提示词。
- 支持任何支持多模态的模型（gpt-4-vision, claude-3, qwen-vl等）。
- 自动从config.json读取API配置，无需手动传入。
- 单张图片大小限制10MB。
"""

import base64
import json
import logging
import mimetypes
import os

import requests

from . import PROJECT_ROOT, register_tool
from .base import _xml_response

logger = logging.getLogger(__name__)

# 单张图片大小上限：10MB
MAX_IMAGE_SIZE = 10 * 1024 * 1024

# 支持的图片扩展名
IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif",
}


def _load_api_config():
    """从config.json读取主API配置
    config.json 中的实际 key 名：api_base_url / api_key / default_model
    兼容旧名 base_url / model_name
    """
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        api_config = config.get("agent", {})
        # 取 key 的优先级：子节 > 顶层；支持新旧两种 key 名
        base_url = (api_config.get("base_url") or config.get("base_url")
                    or api_config.get("api_base_url") or config.get("api_base_url", ""))
        api_key = api_config.get("api_key") or config.get("api_key", "")
        model = (api_config.get("model_name") or config.get("model_name")
                 or api_config.get("default_model") or config.get("default_model", "auto"))
        return base_url, api_key, model
    except Exception as e:
        logger.debug("读取API配置失败: %s", e)
        return "", "", "auto"


def _load_mm_config():
    """从config.json读取多模态API配置（fallback用）
    格式：{"mm_api": {"api_key": "...", "base_url": "...", "model_name": "..."}}
    兼容旧格式：{"multimodal_api": {...}}
    """
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        mm = config.get("mm_api") or config.get("multimodal_api") or {}
        if not isinstance(mm, dict):
            return "", "", ""
        base_url = (mm.get("base_url") or mm.get("api_base_url") or "").strip()
        api_key = (mm.get("api_key") or "").strip()
        model = (mm.get("model_name") or mm.get("model") or "").strip()
        return base_url, api_key, model
    except Exception as e:
        logger.debug("读取多模态API配置失败: %s", e)
        return "", "", ""


def _safe_path(path: str) -> str:
    """解析图片路径：绝对路径直接用，相对路径按项目根目录解析"""
    if not path:
        return ""
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p):
        p = os.path.join(PROJECT_ROOT, p)
    return os.path.realpath(os.path.abspath(p))


def _read_image_as_base64(image_path: str) -> tuple[str, str]:
    """读取图片并转为base64 data URL，返回(mime_type, data_url)"""
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return mime, data_url


@register_tool(category="媒体", name_cn="识图", risk_level="low")
def ViewImage(
    path: str,
    prompt: str = "请描述这张图片的内容",
    model: str = "",
) -> str:
    """
    识图工具：读取本地图片并调用多模态API进行内容理解。

    Use this tool when you need to understand or analyze an image:
    - 截图分析、OCR文字提取
    - 图像描述、内容理解
    - 图表/公式识别
    - 图像问答（"这张图里有什么？"）

    Args:
        path: 图片文件路径（绝对路径或相对于项目根目录）
        prompt: 识图提示词，默认为"请描述这张图片的内容"
        model: 指定使用的视觉模型（可选，默认用config.json中的model_name）

    Returns:
        API返回的识别结果文本

    Examples:
        ViewImage(path="screenshots/error.png", prompt="这个错误是什么？")
        ViewImage(path="design/mockup.jpg", prompt="描述这个UI设计")
        ViewImage(path="photo.png", prompt="提取图中的所有文字（OCR）")
    """
    real = _safe_path(path)

    # 路径检查
    if not real or not os.path.exists(real):
        return _xml_response("error", f"文件不存在: {path}")
    if not os.path.isfile(real):
        return _xml_response("error", f"不是文件: {path}")

    ext = os.path.splitext(real)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return _xml_response(
            "error",
            f"不支持的图片格式: {ext}。支持: {', '.join(sorted(IMAGE_EXTENSIONS))}",
        )

    # 敏感路径检查
    SENSITIVE_PATHS = {"/etc/shadow", "/etc/sudoers", "/etc/gshadow", "/root/.ssh", "/etc/ssh"}
    for sp in SENSITIVE_PATHS:
        if real == sp or real.startswith(sp + os.sep):
            return _xml_response("error", f"禁止访问: 敏感文件 ({path})")

    # 文件大小检查
    try:
        file_size = os.path.getsize(real)
        if file_size > MAX_IMAGE_SIZE:
            return _xml_response(
                "error",
                f"图片过大: {file_size / 1024 / 1024:.1f}MB，超过10MB限制",
            )
    except OSError as e:
        return _xml_response("error", f"读取文件失败: {e}")

    # 读取图片
    try:
        mime, data_url = _read_image_as_base64(real)
    except Exception as e:
        return _xml_response("error", f"读取图片失败: {e}")

    # 获取API配置
    base_url, api_key, default_model = _load_api_config()
    if not base_url or not api_key:
        return _xml_response(
            "error",
            "未配置API（config.json缺少base_url/api_key）。请先在设置中配置网关地址和密钥。",
        )

    # 确定模型
    use_model = model or default_model or "auto"

    # ── 端点列表：主API → 多模态fallback ──────────────────────────
    endpoints = [(base_url, api_key, use_model)]
    mm_url, mm_key, mm_md = _load_mm_config()
    if mm_url and mm_key:
        endpoints.append((mm_url, mm_key, mm_md or use_model))
    if not endpoints:
        return _xml_response("error", "未配置任何API，请先在设置中配置网关地址和密钥。")

    MAX_RETRIES = 2       # 最多重试2次（含首次共3次）
    TIMEOUT = 30          # 单次超时30秒

    def _call_api(url, key, mdl):
        """执行一次API调用，返回(成功bool, 结果文本str, 错误消息str)"""
        # 自动检测 base_url 是否已含 /v1，避免拼成 /v1/v1/chat/completions
        base = url.rstrip('/')
        chat_path = "/v1/chat/completions" if not base.endswith("/v1") else "/chat/completions"
        api_url = base + chat_path
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": mdl,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 2000,
        }
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            result = resp.json()
            choices = result.get("choices", [])
            if not choices:
                return False, "", "API返回空结果"
            message = choices[0].get("message", {})
            text = message.get("content", "").strip()
            if not text:
                return False, "", "API返回空文本"
            return True, text, ""
        except requests.exceptions.Timeout:
            return False, "", "timeout"
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            err_text = e.response.text[:300] if e.response.text else ""
            return False, "", f"http_error_{status}"
        except Exception:
            return False, "", "network_error"

    # ── 逐端点尝试，每端点最多重试MAX_RETRIES次 ───────────────────
    last_err = ""
    last_model = use_model
    for i, (url, key, mdl) in enumerate(endpoints):
        src_label = "主API" if i == 0 else "多模态fallback"
        logger.info("识图调用%s (endpoint %d/%d): model=%s, path=%s", src_label, i + 1, len(endpoints), mdl, real)
        for attempt in range(1 + MAX_RETRIES):
            ok, text, err = _call_api(url, key, mdl)
            if ok:
                last_model = mdl
                body = (
                    f"# 识图结果\n\n"
                    f"**图片**: `{path}`\n"
                    f"**模型**: `{last_model}`\n"
                    f"**提示词**: `{prompt}`\n\n"
                    f"---\n\n"
                    f"{text}\n"
                )
                return _xml_response("done", body)
            last_err = err
            logger.debug("识图尝试失败 (endpoint %d attempt %d): %s", i + 1, attempt + 1, err)
        # 当前端点所有重试耗尽，进入下一端点（如有）
        if i < len(endpoints) - 1:
            logger.info("端点%d全部失败，切换至端点%d", i + 1, i + 2)

    # 所有端点、所有重试均失败
    if last_err == "timeout":
        return _xml_response("error", f"识图失败：API请求超时（> {TIMEOUT}s），所有端点重试均失败。")
    if last_err and last_err.startswith("http_error"):
        return _xml_response("error", f"识图失败：API HTTP 错误（{last_err}），所有端点重试均失败。")
    return _xml_response("error", f"识图失败：{last_err or '未知错误'}，所有端点重试均失败。")
