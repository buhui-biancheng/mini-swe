import os
import json
from typing import Any, Optional
from dataclasses import dataclass, field
from openai import OpenAI


@dataclass
class LLMResponse:
    content: Optional[str]
    tool_calls: list[dict[str, Any]]
    usage: dict[str, int]


@dataclass
class LLMClient:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"

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
    ) -> LLMResponse:
        """调用 LLM 进行对话。

        Args:
            messages: 对话历史
            tools: 工具定义列表（Function Calling 格式）
            temperature: 温度参数
            max_tokens: 最大输出 token 数

        Returns:
            LLMResponse 包含 content、tool_calls、usage
        """
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        if tools:
            kwargs["tools"] = tools

        response = self.client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if message.tool_calls:
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
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

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            usage=usage,
        )

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_executor: callable,
        max_rounds: int = 5,
    ) -> tuple[str, list[dict[str, Any]]]:
        """带工具调用的多轮对话。

        Args:
            messages: 初始对话历史
            tools: 工具定义列表
            tool_executor: 工具执行函数，接收 (tool_name, arguments) 返回结果字符串
            max_rounds: 最大工具调用轮数

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
            assistant_msg["tool_calls"] = response.tool_calls
            conversation.append(assistant_msg)

            for tc in response.tool_calls:
                func_name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                result = tool_executor(func_name, args)

                conversation.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

        return "", conversation
