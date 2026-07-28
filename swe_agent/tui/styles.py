"""TUI 样式定义：颜色、主题等。"""

from rich.style import Style
from rich.theme import Theme

# 自定义主题
CUSTOM_THEME = Theme({
    "user": Style(color="cyan", bold=True),
    "ai": Style(color="green"),
    "tool_ok": Style(color="green"),
    "tool_err": Style(color="red"),
    "thinking": Style(color="grey50"),
    "status": Style(color="yellow"),
    "header": Style(color="bright_blue", bold=True),
    "border": Style(color="blue"),
    "input": Style(color="white"),
})

# 消息类型对应的样式
MSG_STYLES = {
    "user": "user",
    "assistant": "ai",
    "tool_call": "tool_ok",
    "tool_result": "tool_ok",
    "tool_error": "tool_err",
    "thinking": "thinking",
    "system": "status",
}

# 状态颜色
STATUS_COLORS = {
    "init": "yellow",
    "locate": "cyan",
    "patch": "blue",
    "test": "magenta",
    "success": "green",
    "fail": "red",
}
