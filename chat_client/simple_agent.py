import json
import uuid
from collections.abc import Generator
from typing import Any

import openai

from chat_client.api_retry import is_retryable_api_err
from chat_client.memory import MemoryManager
from chat_client.retrieval import RAGService

from .tools import registry


class SimpleAgent:
    def __init__(self, session_id: str, config: dict[str, Any] | None = None):
        self.session_id = session_id
        self.config = config or {}
        
        # 提取配置
        self.api_key = self.config.get("api_key")
        self.base_url = self.config.get("base_url")
        self.model_name = self.config.get("model_name")
        # 简单Agent通常不需要复杂的工具迭代，但为了兼容性保留基本配置读取，虽然不使用工具
        self.default_headers = self.config.get("default_headers", {})
        self.system_prompt = self.config.get("system_prompt", "")
        self.temperature = self.config.get("temperature", 1)
        self.top_p = self.config.get("top_p", 1)
        self.use_global_rag = self.config.get("use_global_rag", False)

        self.enabled_tools = self.config.get("tools", [])
        
        # Default tools for non-code mode
        default_non_code_tools = [
            "Skills"
        ]
        for tool in default_non_code_tools:
            if tool not in self.enabled_tools:
                self.enabled_tools.append(tool)
        
        # 显式设置滑动窗口大小为极大值，以实现"全量记忆"
        # 用户提到通常在10轮内，设置100足够覆盖全量
        self.memory = MemoryManager(
            session_id=session_id, 
            sessions_dir=self.config.get("sessions_dir", "sessions"),
            sliding_window_size=self.config.get("sliding_window_size", 100) 
        )

        # Global Knowledge Base RAG Service (Optional)
        self.global_rag = None
        if self.use_global_rag:
             self.global_rag = RAGService(
                session_dir=self.config.get("global_kb_dir", "global_knowledge"),
                openai_api_key=self.api_key,
                openai_base_url=self.base_url,
                embedding_api_key=self.config.get("embedding_api_key"),
                embedding_base_url=self.config.get("embedding_base_url"),
                embedding_model_name=self.config.get("embedding_model_name"),
                small_model_name=self.config.get("small_model_name"),
                rag_retrieval_count=self.config.get("rag_retrieval_count", 10),
                rag_final_count=self.config.get("rag_final_count", 5),
                default_headers=self.default_headers
            )
        
        self.client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            default_headers=self.default_headers,
            max_retries=self.config.get("max_retries", 5),
            # P0：不设 timeout 时 SDK 默认 600s，网络半开时请求挂死 10 分钟
            timeout=self.config.get("api_timeout", 120)
        )
        
    @staticmethod
    def _sanitize_messages_for_api(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """清理消息列表中非法的 tool_calls，防止 API 返回 400 错误。"""
        sanitized = []
        for msg in messages:
            msg = dict(msg)
            if "tool_calls" in msg:
                clean_tcs = []
                for tc in msg["tool_calls"]:
                    fn = (tc.get("function") or {})
                    args_str = (fn.get("arguments") or "").strip()
                    name = (fn.get("name") or "").strip()
                    if not name:
                        continue
                    if not args_str:
                        tc = dict(tc)
                        tc["function"] = dict(fn, arguments="{}")
                        clean_tcs.append(tc)
                        continue
                    try:
                        json.loads(args_str)
                        clean_tcs.append(tc)
                    except (json.JSONDecodeError, TypeError):
                        pass  # 丢弃非法 JSON 的 tool_call
                if clean_tcs:
                    msg["tool_calls"] = clean_tcs
                else:
                    del msg["tool_calls"]
            sanitized.append(msg)
        return sanitized

    def close(self):
        """
        关闭 Agent，释放资源。
        """
        if self.global_rag:
            self.global_rag.close()
        self.client.close()

    def chat(self, user_input: str | list[dict[str, Any]]) -> Generator[dict[str, Any], None, None]:
        """
        简单Agent聊天循环，支持流式响应。
        已添加工具调用支持。
        """
        try:
            # 生成 ID
            user_msg_id = str(uuid.uuid4())
            ai_msg_id = str(uuid.uuid4())

            yield {
                "type": "meta_info",
                "user_msg_id": user_msg_id,
                "ai_msg_id": ai_msg_id
            }
            
            # 提取纯文本用于检索
            user_text = user_input
            if isinstance(user_input, list):
                text_parts = []
                for item in user_input:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                user_text = "\n".join(text_parts)
            
            # 1. 更新记忆 (用户)
            self.memory.add_message("user", user_input, id=user_msg_id)
            
            # RAG 检索
            context_str = ""
            if self.global_rag:
                 global_docs = self.global_rag.search(user_text, scope="global",score=0.5)
                 if global_docs:
                     context_str += "[Global Knowledge Base]:\n" + "\n".join(global_docs) + "\n\n"

            # 2. 构建消息 (使用全量记忆，无RAG)
            messages = self._build_messages(context_str)
            
            self.enabled_tools.append("get_panel_info")
            tools = registry.get_openai_tools(enabled_ids=self.enabled_tools)
            
            # 循环限制，防止无限递归
            max_tool_iterations = self.config.get("max_tool_iterations", 10)
            iteration_count = 0
            full_response_content = "" # 最终累积响应
            full_reasoning_content = "" # 最终累积思考
            tool_call_chunks = {} 
            
            while iteration_count < max_tool_iterations:
                iteration_count += 1

                # ---- 带自动重试的流式请求：断流 / 429 / 5xx 后重新请求 ----
                import time as _time

                api_max_retry = max(0, int(self.config.get("api_max_retry", 3)))

                attempt = 0
                yielded_content = ""

                while True:
                    attempt += 1
                    # 工具调用累加器
                    tool_call_chunks = {}
                    current_response_content = ""
                    current_reasoning_content = ""
                    try:
                        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
                            messages = [dict(messages[0], cache_control={"type": "ephemeral"})] + messages[1:]
                        messages = self._sanitize_messages_for_api(messages)
                        response_stream = self.client.chat.completions.create(
                        model=self.model_name,
                        messages=messages,
                            stream=True,
                            stream_options={"include_usage": True},
                            temperature=self.temperature,
                            top_p=self.top_p,
                            tools=tools if tools else None
                        )

                        for chunk in response_stream:

                            # 结束判断
                            if not chunk.choices:
                                if chunk.usage:
                                    yield {
                                    "type": "stop",
                                    "usage": {
                                        "total_tokens": chunk.usage.total_tokens,
                                        "input_tokens": chunk.usage.prompt_tokens,
                                        "output_tokens": chunk.usage.completion_tokens
                                    },
                                    "message_id": chunk.id
                                }
                                continue

                            delta = chunk.choices[0].delta

                            # 处理推理内容
                            if getattr(delta, "reasoning_content", None):
                                current_reasoning_content += delta.reasoning_content
                                yield {
                                    "type": "reasoning",
                                    "response": delta.reasoning_content
                                }

                            # 处理正文内容
                            if delta.content:
                                current_response_content += delta.content
                                yielded_content += delta.content
                                yield {
                                    "type": "content",
                                    "response": delta.content
                                }

                            # 处理工具调用
                            if delta.tool_calls:
                                for tc in delta.tool_calls:
                                    index = tc.index
                                    if index not in tool_call_chunks:
                                        tool_call_chunks[index] = {
                                            "id": tc.id,
                                            "function": {"name": "", "arguments": ""}
                                        }

                                    if tc.id:
                                        tool_call_chunks[index]["id"] = tc.id
                                    if tc.function.name:
                                        tool_call_chunks[index]["function"]["name"] += tc.function.name
                                    if tc.function.arguments:
                                        tool_call_chunks[index]["function"]["arguments"] += tc.function.arguments

                        if attempt > 1:
                            current_response_content = yielded_content
                        break

                    # 注意：不能用 except _openai.OpenAIError —— httpx2 断流异常不是其子类，
                    # 必须捕获 Exception 后交给 _is_retryable_api_err 判定
                    except Exception as api_err:
                        if not is_retryable_api_err(api_err) or attempt > api_max_retry:
                            raise
                        wait_s = min(2 ** (attempt - 1), 10)
                        _time.sleep(wait_s)
                        if yielded_content and not tool_call_chunks:
                            # 移除上一次重试附加的续写上下文，避免堆叠
                            if messages and str(messages[-1].get("content", "")).startswith("[system-note]"):
                                del messages[-2:]
                            messages.append({"role": "assistant", "content": yielded_content})
                            messages.append({"role": "system", "content":
                                "[system-note] 上一次响应因网络异常被截断。请从上次中断处无缝继续输出剩余内容，不要重复任何已输出的文字。"})
                        else:
                            yielded_content = ""

                # 续写成功后移除临时的续写上下文（不污染后续轮次）
                if attempt > 1 and messages and str(messages[-1].get("content", "")).startswith("[system-note]"):
                    del messages[-2:]

                # 如果这一轮有内容，累加到最终响应
                if current_response_content:
                    full_response_content = current_response_content
                if current_reasoning_content:
                    full_reasoning_content = current_reasoning_content

                # 如果没有工具调用，结束循环
                if not tool_call_chunks:
                    break

                # 处理工具调用逻辑
                assistant_msg_kwargs = {"tool_calls": []}
                for idx in sorted(tool_call_chunks.keys()):
                    tc = tool_call_chunks[idx]
                    assistant_msg_kwargs["tool_calls"].append({
                        "id": tc["id"],
                        "type": "function",
                        "function": tc["function"]
                    })
                
                # 保存助手工具调用消息
                if current_reasoning_content:
                    assistant_msg_kwargs["reasoning_content"] = current_reasoning_content
                
                self.memory.add_message("assistant", current_response_content, id=ai_msg_id, **assistant_msg_kwargs)
                
                messages.append({
                    "role": "assistant",
                    "content": current_response_content,
                    "tool_calls": assistant_msg_kwargs["tool_calls"]
                })

                # --- 循环调用保护检测 ---
                is_duplicate_call = False
                if iteration_count > 1 and messages:
                    last_assistant_msg = None
                    for i in range(len(messages) - 2, -1, -1):
                        if messages[i]["role"] == "assistant":
                            last_assistant_msg = messages[i]
                            break
                    
                    if last_assistant_msg and "tool_calls" in last_assistant_msg:
                        current_calls_dump = json.dumps([{k: v for k, v in tc.items() if k != 'id'} for tc in assistant_msg_kwargs["tool_calls"]], sort_keys=True)
                        prev_calls_dump = json.dumps([{k: v for k, v in tc.items() if k != 'id'} for tc in last_assistant_msg["tool_calls"]], sort_keys=True)
                        
                        if current_calls_dump == prev_calls_dump:
                            is_duplicate_call = True

                if is_duplicate_call:
                    for tc in assistant_msg_kwargs["tool_calls"]:
                        func_name = tc["function"]["name"]
                        call_id = tc["id"]
                        
                        err_msg = "System Monitor: Detected repeated tool execution with identical arguments. Please do not run the same tool again. Analyze the PREVIOUS results and provide your final answer immediately."
                        
                        yield {
                            "type": "tool_result",
                            "tool": func_name,
                            "result": err_msg,
                            "id": call_id
                        }
                        
                        self.memory.add_message("tool", err_msg, tool_call_id=call_id, id=ai_msg_id)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": err_msg
                        })
                    continue
                # ------------------------

                # 执行工具
                for tc in assistant_msg_kwargs["tool_calls"]:
                    func_name = tc["function"]["name"]
                    args_str = tc["function"]["arguments"]
                    call_id = tc["id"]
                    
                    yield {
                        "type": "tool_call",
                        "tool": func_name,
                        "args": args_str,
                        "id": call_id
                    }
                    try:
                        args = json.loads(args_str)
                        func = registry.get_tool_func(func_name)
                        if func:
                            result = func(**args)
                            result_str = json.dumps(result, ensure_ascii=False)

                        else:
                            result_str = f"Error: Tool {func_name} not found."
                    except Exception as e:
                        result_str = f"Error executing tool: {e}"
                    
                    yield {
                        "type": "tool_result",
                        "tool": func_name,
                        "result": result_str[:1000] + '···',
                        "id": call_id
                    }
                    
                    self.memory.add_message("tool", result_str, tool_call_id=call_id, id=ai_msg_id)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": result_str
                    })

            # 4. 最终记忆更新 (助手响应)
            if not tool_call_chunks and full_response_content:
                kwargs = {}
                if full_reasoning_content:
                    kwargs["reasoning_content"] = full_reasoning_content
                
                self.memory.add_message("assistant", full_response_content, id=ai_msg_id, **kwargs)
                 
        except openai.AuthenticationError:
            yield {"type": "error", "data": "API密钥错误或无效，请检查密钥是否正确", "error_code": 401}
        except openai.RateLimitError as e:
            yield {"type": "error", "data": f"接口调用频率超限，请稍后再试或提升配额:{e}", "error_code": 429}
        except openai.APIConnectionError as e:
            yield {"type": "error", "data": f"无法连接到API服务器（{self.base_url}），请检查网络或地址是否正确:{e}", "error_code": getattr(e, 'status_code', 502)}
        except openai.APIError as e:
            yield {"type": "error", "data": f"API返回错误：{e!s}", "error_code": getattr(e, 'status_code', 500)}
        except Exception as e:
            yield {"type": "error", "data": f"调用AI接口时发生未知错误：{e!s}"}

    def _build_messages(self, context_str: str = "") -> list[dict[str, Any]]:
        """构建包含系统指令和全量历史的 Prompt。"""
        
        if context_str:
            self.system_prompt += f"\n\n[History Context]:\n{context_str}"

        messages = [{"role": "system", "content": self.system_prompt}]
        
        # 使用 get_sliding_window 获取记忆
        # 由于初始化时 sliding_window_size 设置很大，这里实际上获取的是全量或近乎全量的记忆
        window = self.memory.get_sliding_window()
        
        for msg in window:
            m = {
                "role": msg["role"],
                "content": msg["content"]
            }
            # 虽然SimpleAgent不产生工具调用，但如果历史记录里有（比如之前是普通Agent产生的），
            # 这里兼容一下，防止报错或信息丢失，但对于SimpleAgent新产生的对话不会有这些。
            if "tool_calls" in msg:
                m["tool_calls"] = msg["tool_calls"]
            if "tool_call_id" in msg:
                m["tool_call_id"] = msg["tool_call_id"]
            messages.append(m)

        return self._sanitize_messages_for_api(messages)
