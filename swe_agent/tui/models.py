"""TUI 数据模型。"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


@dataclass
class Message:
    """消息模型。"""
    content: str
    msg_type: str  # user, assistant, tool_call, tool_result, thinking, system
    timestamp: datetime = field(default_factory=datetime.now)
    tool_name: Optional[str] = None
    is_error: bool = False
    is_collapsed: bool = False  # 思考链是否折叠
    duration: Optional[float] = None  # 工具调用耗时（秒）


@dataclass
class AppState:
    """应用状态。"""
    status: str = "idle"  # idle, running, thinking, calling_tool, success, fail
    step: int = 0
    max_steps: int = 10
    token_usage: int = 0
    elapsed_time: float = 0.0
    messages: list[Message] = field(default_factory=list)
    is_running: bool = False
