import os
import json
import time
from typing import Any, Optional, Callable
from dataclasses import dataclass
from dotenv import load_dotenv
from openai import OpenAI
from openai import (
    APITimeoutError,
    RateLimitError,
    APIConnectionError,
    APIStatusError,
)

load_dotenv()


class AgentAPIError(Exception):
    """LLM API 持续失败（重试用尽后抛出，FSM 接住走取消事件）。"""

    def __init__(self, message: str = "LLM API 持续失败", retries: int = 0):
        super().__init__(message)
        self.retries = retries


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]
    reasoning_content: Optional[str] = None


@dataclass
class LLMClient:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"

    def __post_init__(self):
        if not self.api_key:
            self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not self.api_key:
            raise ValueError("未设置 DEEPSEEK_API_KEY，请在环境变量或 .env 文件中配置")

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking: bool = False,
        reasoning_effort: str = "high",
        stream: bool = False,
        on_token: Optional[Callable[[str], None]] = None,
        on_reasoning_token: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """调用 LLM 进行对话。

        Args:
            messages: 对话历史
            tools: 工具定义列表（Function Calling 格式）
            temperature: 温度参数
            max_tokens: 最大输出 token 数
            thinking: 是否启用深度思考模式
            reasoning_effort: 推理强度（low/medium/high）
            stream: 是否启用流式输出
            on_token: 流式输出回调函数

        Returns:
            LLMResponse 包含 content、tool_calls、usage
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }

        if thinking:
            kwargs["extra_body"] = {
                "thinking": {"type": "enabled"},
            }
            # reasoning_effort 是顶层参数
            kwargs["reasoning_effort"] = reasoning_effort

        if tools:
            kwargs["tools"] = tools

        # 流式输出处理
        if stream:
            return self._handle_stream(kwargs, on_token, on_reasoning_token)

        response = self._create_with_retry(kwargs)

        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # DeepSeek thinking 模式返回 reasoning_content，需要传回
        reasoning_content = getattr(message, "reasoning_content", None)

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=usage,
            reasoning_content=reasoning_content,
        )

    def _create_with_retry(self, kwargs: dict[str, Any], max_retries: int = 4):
        """包裹 API 调用，指数退避重试。

        - 429 / 超时 / 连接错误 → 重试（退避 2^n 秒）
        - 5xx → 重试；4xx（参数错误等）→ 不重试直接抛出
        - 持续失败 → 抛 AgentAPIError（FSM 接住走取消事件）
        """
        last_exc: Optional[Exception] = None
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(**kwargs)
            except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                last_exc = e
                wait = 2 ** attempt
                print(f"  [RETRY] LLM API 错误，{wait}秒后重试 ({attempt + 1}/{max_retries}): {type(e).__name__}")
                time.sleep(wait)
            except APIStatusError as e:
                if e.status_code >= 500:
                    last_exc = e
                    wait = 2 ** attempt
                    print(f"  [RETRY] LLM API 5xx，{wait}秒后重试 ({attempt + 1}/{max_retries}): {e.status_code}")
                    time.sleep(wait)
                else:
                    raise  # 4xx 不重试
            except Exception as e:
                # 网络层未知错误（如 DNS/连接重置），按可重试处理
                last_exc = e
                wait = 2 ** attempt
                print(f"  [RETRY] LLM API 未知错误，{wait}秒后重试 ({attempt + 1}/{max_retries}): {type(e).__name__}")
                time.sleep(wait)

        raise AgentAPIError(
            f"LLM API 持续失败: {type(last_exc).__name__}: {last_exc}",
            retries=max_retries,
        ) from last_exc

    def _handle_stream(
        self,
        kwargs: dict[str, Any],
        on_token: Optional[Callable[[str], None]] = None,
        on_reasoning_token: Optional[Callable[[str], None]] = None,
    ) -> LLMResponse:
        """处理流式输出。"""
        content = ""
        tool_calls_data = []
        usage = {}
        reasoning_content = ""

        response = self._create_with_retry(kwargs)

        for chunk in response:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # 处理内容
            if delta.content:
                content += delta.content
                if on_token:
                    on_token(delta.content)

            # 处理 reasoning_content（思考链）
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                reasoning_content += delta.reasoning_content
                if on_reasoning_token:
                    on_reasoning_token(delta.reasoning_content)
                elif on_token:
                    on_token(delta.reasoning_content)

            # 处理工具调用
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    while len(tool_calls_data) <= idx:
                        tool_calls_data.append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                    if tc_delta.id:
                        tool_calls_data[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tool_calls_data[idx]["function"]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tool_calls_data[idx]["function"]["arguments"] += tc_delta.function.arguments

            # 处理 usage
            if chunk.usage:
                usage = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "total_tokens": chunk.usage.total_tokens,
                }

        return LLMResponse(
            content=content,
            tool_calls=tool_calls_data,
            usage=usage,
            reasoning_content=reasoning_content if reasoning_content else None,
        )

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_executor: callable,
        max_rounds: int = 5,
        max_retries: int = 3,
    ) -> tuple[str, list[dict[str, Any]]]:
        """带工具调用的多轮对话。

        Args:
            messages: 初始对话历史
            tools: 工具定义列表
            tool_executor: 工具执行函数，接收 (tool_name, arguments) 返回结果字符串
            max_rounds: 最大工具调用轮数
            max_retries: JSON 解析失败时最大重试次数

        Returns:
            (最终回复文本, 完整对话历史)
        """
        conversation = list(messages)

        for _ in range(max_rounds):
            response = self.chat(conversation, tools=tools)

            if not response.tool_calls:
                return response.content or "", conversation

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if response.content:
                assistant_msg["content"] = response.content
            if response.reasoning_content:
                assistant_msg["reasoning_content"] = response.reasoning_content
            assistant_msg["tool_calls"] = response.tool_calls
            conversation.append(assistant_msg)

            for tc in response.tool_calls:
                func_name = tc["function"]["name"]

                # 指数退避重试 JSON 解析
                args = {}
                json_parse_error = None
                for retry in range(max_retries):
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        json_parse_error = None
                        break
                    except json.JSONDecodeError as e:
                        json_parse_error = e
                        if retry < max_retries - 1:
                            wait_time = 2 ** retry  # 指数退避：1s, 2s, 4s
                            print(f"  [RETRY] JSON 解析失败，{wait_time}秒后重试... ({retry + 1}/{max_retries})")
                            time.sleep(wait_time)

                if json_parse_error:
                    print(f"  [ERROR] JSON 解析最终失败: {json_parse_error}")
                    # 尝试从文本中提取 JSON
                    raw = tc["function"]["arguments"]
                    if "{" in raw and "}" in raw:
                        try:
                            start = raw.index("{")
                            end = raw.rindex("}") + 1
                            args = json.loads(raw[start:end])
                        except (json.JSONDecodeError, ValueError):
                            args = {}

                result = tool_executor(func_name, args)

                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

        return "", conversation
