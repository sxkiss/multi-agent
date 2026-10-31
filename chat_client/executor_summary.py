"""
executor_summary module
Provides summarize_with_executor function for group brief skill.

实现：调用 BT 本地网关（config.json 的 api_base_url / api_key / default_model）
做一次 chat completion，产出结构化中文群聊摘要。失败时返回 ok=False。
"""
import json
import logging
import os
from typing import Any

import requests

logger = logging.getLogger(__name__)

CONFIG_PATH = os.environ.get("BT_CONFIG_PATH", "/home/sxkiss/bt/config.json")


def _load_llm_conf() -> dict[str, str]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    base = str(cfg.get("api_base_url") or "http://127.0.0.1:3333/v1").rstrip("/")
    key = str(cfg.get("api_key") or "")
    model = str(cfg.get("default_model") or "auto")
    return {"base": base, "key": key, "model": model}


def build_summary_prompt(messages: list[str], group_id: str, request: str) -> str:
    joined = "\n".join(messages)
    return (
        "你是微信群聊总结助手。请只依据下面的聊天记录输出简洁中文总结，不要编造。\n"
        "输出格式（Markdown）：\n"
        "1. 总览：一句话概括这段时间群里主要在聊什么\n"
        "2. 核心话题：最多 4 条，每条一句话\n"
        "3. 结论/分歧：最多 2 条\n"
        "4. 待跟进：没有就写「无」\n"
        "要求：总字数控制在 500 字内；忽略机器人播报、系统提示和无关表情。\n\n"
        f"请求：{request}\n"
        f"聊天记录（{len(messages)} 条）：\n{joined}"
    )


def summarize_with_executor(messages: list[str], group_id: str, request: str, timeout: int = 180) -> dict[str, Any]:
    """调用本地模型网关生成群聊摘要。"""
    if not messages:
        return {"ok": False, "text": "没有可总结的群消息", "meta": {"messageCount": 0}}

    conf = _load_llm_conf()
    url = conf["base"] + "/chat/completions"
    payload = {
        "model": conf["model"],
        "messages": [
            {"role": "system", "content": "你是严谨的中文群聊总结助手，只基于给定记录作答。"},
            {"role": "user", "content": build_summary_prompt(messages, group_id, request)},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }
    headers = {"Content-Type": "application/json"}
    if conf["key"]:
        headers["Authorization"] = "Bearer " + conf["key"]

    last_err = ""
    for attempt in range(2):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=min(int(timeout), 120))
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                continue
            data = resp.json()
            text = ""
            choices = data.get("choices") or []
            if choices:
                text = ((choices[0].get("message") or {}).get("content") or "").strip()
            if not text:
                last_err = "模型返回空内容"
                continue
            return {
                "ok": True,
                "text": text,
                "meta": {
                    "messageCount": len(messages),
                    "mode": request,
                    "model": data.get("model") or conf["model"],
                },
            }
        except Exception as e:
            last_err = str(e)
            logger.warning("summarize_with_executor 第 %d 次失败: %s", attempt + 1, e)

    return {"ok": False, "text": f"群总结生成失败：{last_err}", "meta": {"messageCount": len(messages)}}
