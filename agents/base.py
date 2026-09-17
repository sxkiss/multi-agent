"""
@input: openai, typing
@output: BaseAgent — 系统信息收集、Prompt 构建、OpenAI 流式调用
@position: Agents base — 预置 Agent 基类（面板兼容模式）
@auto-doc: Update header and folder INDEX.md when this file changes
"""
from openai import OpenAI
from openai import APIError, APIConnectionError, AuthenticationError, RateLimitError
from typing import List, Dict, Optional, Any

class BaseAgent:
    """
    Base Agent class
    封装了系统信息收集、信息构建和 OpenAI API 调用流程。
    """
    
    def __init__(
            self,
            api_key: str,
            base_url: str = "https://api.openai.com/v1",
            headers: Optional[Dict[str, str]] = None,
            model: str = "gpt-3.5-turbo",
            temperature: float = 0.7,
            system_role: str = "",
            agent_args: Dict[str, Any] = None
    ):
        """
        Initialize the agent
        :param api_key: API key
        :param base_url: API base URL
        :param model: Model name
        :param temperature: Temperature
        :param system_role: System role prompt
        :param agent_args: Arguments for the agent from configuration
        """
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.system_role = system_role
        self.agent_args = agent_args or {}
        
        # Initialize OpenAI client
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=headers
        )
    
    def build_message(
            self,
            tool_results: List[Dict[str, str]],
            user_question: str
    ) -> List[Dict[str, str]]:
        """
        构建消息列表，包含角色、工具结果和提示词
        :param tool_results: 工具运行结果列表
        :param user_question: 提示词
        :return: 消息列表
        """
        system_content = self.system_role
        system_content += "\n\n前置工具运行结果：\n" if tool_results else ""
        
        for idx, tool_res in enumerate(tool_results, 1):
            system_content += (
                f"{idx}. 工具名称：{tool_res['tool']}\n"
                f"   运行状态：{tool_res['status']}\n"
                f"   输出内容：{tool_res['data']}\n\n"
            )
        
        messages = [
            {"role": "system", "content": "系统信息：请基于以下前置工具运行结果，结合用户的提问，生成专业且易懂的回答。核心规则：1. 所有分析均为AI建议，请勿直接操作，先让用户分析可不可行后操作，不要有太强的指导性；2. 保持中性建议，不要太过激进（避免使用立刻、马上、立即、紧急、超级等重感情词语）。"+system_content.strip()},
            {"role": "user", "content": user_question}
        ]
        return messages
    
    def close(self):
        """
        关闭客户端连接
        """
        if self.client:
            self.client.close()
    
    def call_ai_api(self, messages: List[Dict[str, str]]):
        """
        调用OPENAI接口，返回流式生成器 流式chunk实时yield，无阻塞
        """
        try:
            if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
                messages = [dict(messages[0], cache_control={"type": "ephemeral"})] + messages[1:]
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                stream=True,
                stream_options={"include_usage": True},
                # extra_body={"thinking": {"type": "disabled"}}
            )
            for chunk in response:
                result ={
                    "type": "",
                    "response": "",
                }
                
                # if chunk.usage:
                #     result["usage"] = {
                #         "total_tokens": chunk.usage.total_tokens,
                #         "input_tokens": chunk.usage.prompt_tokens,
                #         "output_tokens": chunk.usage.completion_tokens
                #     }
                
                if chunk.choices and getattr(chunk.choices[0].delta, "reasoning_content", None):
                    result["type"] = "reasoning"
                    result["response"] = chunk.choices[0].delta.reasoning_content

                if chunk.choices and chunk.choices[0].delta.content:
                    result["type"] = "content"
                    result["response"] = chunk.choices[0].delta.content
                
                if not chunk.choices:
                    result["type"] = "stop"
                    result["usage"] = {
                        "total_tokens": chunk.usage.total_tokens,
                        "input_tokens": chunk.usage.prompt_tokens,
                        "output_tokens": chunk.usage.completion_tokens
                    }
                
                yield result
        except AuthenticationError:
            yield {"type": "error", "data": "API密钥错误或无效，请检查密钥是否正确"}
        except RateLimitError as e:
            yield {"type": "error", "data": "接口调用频率超限，请稍后再试或提升配额:{}".format(e)}
        except APIConnectionError:
            yield {"type": "error", "data": f"无法连接到API服务器（{self.base_url}），请检查网络或地址是否正确"}
        except APIError as e:
            yield {"type": "error", "data": f"API返回错误：{str(e)}"}
        except Exception as e:
            yield {"type": "error", "data": f"调用AI接口时发生未知错误：{str(e)}"}
    
    def run(self, user_question: str, custom_tool_results: List[Dict] = None) -> Any:
        """
        Run the agent with streaming response
        """
        if custom_tool_results is None:
            custom_tool_results = []
            
        messages = self.build_message(custom_tool_results, user_question)
        
        return self.call_ai_api(messages)