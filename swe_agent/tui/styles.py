"""TUI 样式：Claude Code 简约风格。"""

from rich.style import Style
from rich.theme import Theme

# 自定义主题（Claude Code 风格：低饱和、高对比）
CUSTOM_THEME = Theme({
    "user": Style(color="bright_cyan", bold=True),
    "ai": Style(color="bright_white"),
    "tool_ok": Style(color="green"),
    "tool_err": Style(color="red"),
    "thinking": Style(color="grey50", italic=True),
    "system": Style(color="grey50"),
    "header": Style(color="bright_white", bold=True),
    "dim": Style(color="grey50"),
    "success": Style(color="green"),
    "fail": Style(color="red"),
    "muted": Style(color="grey50"),
    "accent": Style(color="bright_cyan"),
    "prompt": Style(color="bright_cyan", bold=True),
})

# 消息类型对应的样式
MSG_STYLES = {
    "user": "user",
    "assistant": "ai",
    "tool_call": "tool_ok",
    "tool_result": "tool_ok",
    "tool_error": "tool_err",
    "thinking": "thinking",
    "system": "system",
}

# 状态颜色
STATUS_COLORS = {
    "idle": "white",
    "running": "cyan",
    "thinking": "yellow",
    "calling_tool": "blue",
    "success": "green",
    "fail": "red",
    "init": "yellow",
    "locate": "cyan",
    "patch": "blue",
    "test": "magenta",
}

# Claude Code 风格 CSS
APP_CSS = """
Screen {
    layout: vertical;
    background: $surface;
}

/* 状态栏：极简，一行，无背景色块 */
#status-bar {
    height: 1;
    dock: top;
    background: $surface;
    color: $text-muted;
    padding: 0 1;
    border-bottom: solid $primary-background-darken-2;
}

/* 消息列表：无内边距，紧凑 */
#message-list {
    height: 1fr;
    overflow-y: auto;
    padding: 0;
}

/* 输入区：简洁底线 */
#input-area {
    height: auto;
    max-height: 5;
    dock: bottom;
    background: $surface;
    border-top: tall $primary-background-darken-2;
    padding: 0 0;
}

#input-area Input {
    background: transparent;
    color: $text;
    padding: 0 1;
}

/* 命令菜单 */
#command-menu {
    display: none;
    dock: bottom;
    max-height: 12;
    height: auto;
    background: $surface;
    border: tall $primary-background-darken-2;
    margin: 0 0 3 0;
}

#command-menu.visible {
    display: block;
}

/* Collapsible 样式：无边框 */
Collapsible {
    margin: 0;
    border: none;
}

Collapsible Title {
    color: $text-muted;
    padding: 0 1;
}

Collapsible .collapse--contents {
    padding: 0 0 0 2;
}

/* Markdown 样式 */
Markdown {
    margin: 0;
    padding: 0 1;
}

Markdown .md-code {
    background: $surface-darken-1;
    padding: 0 1;
}

/* 消息间距 */
.user-message {
    margin: 1 0 0 0;
    padding: 0 1;
}

.ai-message {
    margin: 1 0 0 0;
    padding: 0 1;
}

.system-message {
    margin: 0;
    padding: 0 1;
    color: $text-muted;
}
"""
