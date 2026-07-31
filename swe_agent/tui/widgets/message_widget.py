"""消息渲染组件：根据消息类型分发到不同渲染方式。"""

from textual.widgets import Static, Markdown
from textual.containers import Vertical

from ..models import Message
from .thinking_widget import ThinkingWidget
from .tool_call_widget import ToolCallWidget


class UserMessage(Static):
    """用户消息。"""

    DEFAULT_CSS = """
    UserMessage {
        margin: 1 0 0 0;
        padding: 0 1;
    }
    """

    def __init__(self, content: str, **kwargs):
        super().__init__(**kwargs)
        self._content = content

    def render(self) -> str:
        return f"[bold bright_cyan]>[/bold bright_cyan]  {self._content}"


class AIMessage(Static):
    """AI 消息（纯文本）。"""

    DEFAULT_CSS = """
    AIMessage {
        margin: 1 0 0 0;
        padding: 0 1;
    }
    """

    def __init__(self, content: str, **kwargs):
        super().__init__(**kwargs)
        self._content = content

    def render(self) -> str:
        return self._content


class AIMarkdownMessage(Markdown):
    """AI 消息：Markdown 渲染（代码高亮）。"""

    DEFAULT_CSS = """
    AIMarkdownMessage {
        margin: 1 0 0 0;
        padding: 0 1;
    }
    """

    def __init__(self, content: str, **kwargs):
        super().__init__(content, **kwargs)


class SystemMessage(Static):
    """系统消息。"""

    DEFAULT_CSS = """
    SystemMessage {
        margin: 0;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, content: str, **kwargs):
        super().__init__(**kwargs)
        self._content = content

    def render(self) -> str:
        return f"[dim]{self._content}[/dim]"


class DiffMessage(Static):
    """代码差异：红/绿行着色。"""

    DEFAULT_CSS = """
    DiffMessage {
        margin: 1 0 0 2;
        padding: 0 1;
    }
    """

    def __init__(self, content: str, **kwargs):
        super().__init__(**kwargs)
        self._content = content

    def render(self) -> str:
        lines = self._content.split("\n")
        result = []
        for line in lines:
            if line.startswith("-"):
                result.append(f"[red]{line}[/red]")
            elif line.startswith("+"):
                result.append(f"[green]{line}[/green]")
            elif line.startswith("@@"):
                result.append(f"[cyan]{line}[/cyan]")
            else:
                result.append(line)
        return "\n".join(result)


class ToolResultMessage(Static):
    """工具结果：缩进显示。"""

    DEFAULT_CSS = """
    ToolResultMessage {
        margin: 0 0 0 4;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, content: str, is_error: bool = False, **kwargs):
        super().__init__(**kwargs)
        self._content = content
        self._is_error = is_error

    def render(self) -> str:
        display = self._content[:300] + "..." if len(self._content) > 300 else self._content
        if self._is_error:
            return f"[red]{display}[/red]"
        return f"[dim]{display}[/dim]"


class LoadingMessage(Static):
    """加载中消息。"""

    DEFAULT_CSS = """
    LoadingMessage {
        margin: 1 0;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, content: str = "思考中...", **kwargs):
        super().__init__(**kwargs)
        self._content = content

    def render(self) -> str:
        return f"[dim italic]⟳ {self._content}[/dim italic]"


def create_message_widget(msg: Message, on_toggle_thinking=None):
    """根据消息类型创建对应的 Widget。

    Args:
        msg: 消息数据
        on_toggle_thinking: 思考链折叠/展开回调

    Returns:
        对应的 Widget 实例
    """
    if msg.msg_type == "user":
        return UserMessage(msg.content)

    elif msg.msg_type == "assistant":
        # 检测是否包含代码块，有则用 Markdown 渲染
        if "```" in msg.content or "**" in msg.content or "- " in msg.content:
            return AIMarkdownMessage(msg.content)
        return AIMessage(msg.content)

    elif msg.msg_type == "thinking":
        return ThinkingWidget(
            content=msg.content,
            collapsed=msg.is_collapsed,
        )

    elif msg.msg_type == "tool_call":
        return ToolCallWidget(
            tool_name=msg.tool_name or "unknown",
            arguments=msg.tool_args or {},
            result="",
            success=not msg.is_error,
            duration=msg.duration or 0.0,
            collapsed=True,
        )

    elif msg.msg_type == "tool_result":
        return ToolResultMessage(msg.content, is_error=msg.is_error)

    elif msg.msg_type == "diff":
        return DiffMessage(msg.content)

    elif msg.msg_type == "system":
        return SystemMessage(msg.content)

    elif msg.msg_type == "loading":
        return LoadingMessage(msg.content)

    else:
        return Static(msg.content)
