"""
RAG 判断客户端：用于执行一次性 ChatCompletion 任务
如 RAG 检索判断、标题生成、对话压缩等
不支持流式响应，直接返回完整响应
"""

import json
import logging
import time
from typing import Any

import openai

from chat_client.api_retry import is_retryable_api_err

logger = logging.getLogger(__name__)


class RAGJudgmentClient:
    """RAG 判断专用一次性客户端，基于 SingleAgent 重构而来"""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str | None = None,
        default_headers: dict[str, str] | None = None,
        temperature: float = 0.7,
        top_p: float = 1.0,
        max_retries: int = 5,
        retry_base_wait: float = 1.0,
        retry_max_wait: float = 10.0,
        timeout: float = 120
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model_name = model_name
        if not self.model_name:
            raise ValueError("缺少 model_name 配置（应从 config.json 或调用方传入）")
        self.default_headers = default_headers or {}
        self.temperature = temperature
        self.top_p = top_p
        self.max_retries = max_retries
        self.retry_base_wait = retry_base_wait
        self.retry_max_wait = retry_max_wait
        self.timeout = timeout

        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers,
            max_retries=self.max_retries,
            timeout=self.timeout
        )

    def close(self):
        """关闭客户端连接"""
        self.client.close()

    def chat(
        self,
        prompt: str | None = None,
        input_text: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        json_response: bool = False,
        json_schema: dict[str, Any] | None = None,
        temperature: float | None = None,
        model: str | None = None,
        **kwargs
    ) -> dict[str, Any]:
        """执行一次性 ChatCompletion"""
        try:
            # 构建消息列表
            if messages is not None:
                if prompt is not None:
                    request_messages = [{"role": "system", "content": prompt}] + messages
                else:
                    request_messages = messages
                if input_text is not None:
                    request_messages.append({"role": "user", "content": input_text})
            elif prompt is not None and input_text is not None:
                request_messages = [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": input_text}
                ]
            elif prompt is not None:
                request_messages = [{"role": "system", "content": prompt}]
            else:
                return {
                    "success": False,
                    "error": "必须提供 prompt + input_text 或 messages 参数"
                }

            # 构建请求参数
            params = {
                "model": model or self.model_name,
                "messages": request_messages,
                "temperature": temperature if temperature is not None else self.temperature,
                "top_p": self.top_p,
                **kwargs
            }

            # 处理 JSON 响应
            if json_response or json_schema:
                if json_schema:
                    params["response_format"] = {"type": "json_schema", "json_schema": json_schema}
                else:
                    params["response_format"] = {"type": "json_object"}

            # 调用 API（非流式），对瞬时错误自动重试
            api_max_retry = max(0, int(self.max_retries))
            response = None
            _attempt = 0
            while True:
                _attempt += 1
                try:
                    _msgs = params.get("messages")
                    if _msgs and isinstance(_msgs[0], dict) and _msgs[0].get("role") == "system":
                        params = {**params, "messages": [dict(_msgs[0], cache_control={"type": "ephemeral"})] + _msgs[1:]}
                    response = self.client.chat.completions.create(**params)
                    break
                except Exception as api_err:
                    if not is_retryable_api_err(api_err) or _attempt > api_max_retry:
                        return {
                            "success": False,
                            "error": f"接口调用失败（重试 {api_max_retry} 次仍失败）: {api_err!s}",
                            "error_code": getattr(api_err, "status_code", 503),
                        }
                    wait_s = min(self.retry_base_wait * (2 ** (_attempt - 1)), self.retry_max_wait)
                    logger.warning(
                        "[RAG-Judgment] 接口异常 %s，%ss 后第 %d/%d 次重试: %s",
                        type(api_err).__name__, wait_s, _attempt, api_max_retry, str(api_err)[:200]
                    )
                    time.sleep(wait_s)

            # 提取响应内容
            if not response.choices:
                return {"success": False, "error": "API 返回空响应"}

            content = response.choices[0].message.content
            usage = None
            if response.usage:
                usage = {
                    "total_tokens": response.usage.total_tokens,
                    "input_tokens": response.usage.prompt_tokens,
                    "output_tokens": response.usage.completion_tokens
                }

            # 处理响应
            if json_response or json_schema:
                try:
                    data = json.loads(content)
                    return {"success": True, "data": data, "response": content, "usage": usage}
                except json.JSONDecodeError as e:
                    return {"success": False, "error": f"JSON 解析失败: {e!s}", "response": content, "usage": usage}
            else:
                return {"success": True, "response": content, "usage": usage}

        except openai.AuthenticationError:
            return {"success": False, "error": "API 密钥错误或无效", "error_code": 401}
        except openai.RateLimitError:
            return {"success": False, "error": "接口调用频率超限，请稍后重试", "error_code": 429}
        except openai.APIConnectionError as e:
            return {"success": False, "error": f"无法连接到 API 服务器: {e!s}", "error_code": getattr(e, 'status_code', 502)}
        except openai.APIError as e:
            return {"success": False, "error": f"API 返回错误: {e!s}", "error_code": getattr(e, 'status_code', 500)}
        except Exception as e:
            return {"success": False, "error": f"未知错误: {e!s}"}

    def should_use_rag(self, user_input: str, threshold: float = 0.5) -> dict[str, Any]:
        """判断是否需要 RAG 检索"""
        prompt = """你是一个 RAG 检索判断助手。根据用户输入，判断是否需要从知识库中检索相关信息来回答。

判断标准：
- 如果问题涉及特定领域知识、技术文档、产品信息等，应该检索
- 如果是简单的闲聊或通用知识，不需要检索

请以 JSON 格式返回：
{
    "use_rag": true/false,
    "confidence": 0.0-1.0,
    "reason": "判断理由"
}"""

        result = self.chat(prompt=prompt, input_text=user_input, json_response=True, temperature=0.1)
        if result["success"]:
            return {
                "use_rag": result["data"].get("use_rag", False),
                "confidence": result["data"].get("confidence", 0.0),
                "reason": result["data"].get("reason", "")
            }
        else:
            return {"use_rag": False, "confidence": 0.0, "reason": result.get("error", "判断失败")}
