"""命令补全菜单：输入 / 时弹出，支持筛选和选择。"""

from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option
from textual.message import Message
from textual.reactive import reactive


# 命令定义
COMMANDS = [
    ("/help", "显示帮助信息"),
    ("/status", "显示当前状态"),
    ("/clear", "清空消息"),
    ("/reset", "重置对话历史"),
    ("/quit", "退出"),
    ("ls", "浏览当前目录"),
    ("cd", "切换目录"),
    ("cat", "查看文件内容"),
    ("fix", "修复 bug 文件"),
]


class CommandPalette(OptionList):
    """命令补全菜单。"""

    DEFAULT_CSS = """
    CommandPalette {
        display: none;
        dock: bottom;
        max-height: 12;
        height: auto;
        background: $surface;
        border: tall $primary-background-darken-2;
        margin: 0 0 3 0;
    }
    CommandPalette.visible {
        display: block;
    }
    """

    class Selected(Message):
        """命令选中消息。"""
        def __init__(self, command: str) -> None:
            super().__init__()
            self.command = command

    def __init__(self, **kwargs):
        options = [
            Option(f"[bold]{cmd}[/bold]  [dim]{desc}[/dim]", id=cmd)
            for cmd, desc in COMMANDS
        ]
        super().__init__(*options, **kwargs)

    def filter_commands(self, text: str) -> None:
        """根据输入筛选命令。"""
        if not text:
            # 显示所有
            options = [
                Option(f"[bold]{cmd}[/bold]  [dim]{desc}[/dim]", id=cmd)
                for cmd, desc in COMMANDS
            ]
        else:
            text_lower = text.lower()
            options = [
                Option(f"[bold]{cmd}[/bold]  [dim]{desc}[/dim]", id=cmd)
                for cmd, desc in COMMANDS
                if text_lower in cmd.lower() or text_lower in desc.lower()
            ]

        self.clear_options()
        if options:
            self.add_options(options)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        """选中命令。"""
        if event.option_id:
            self.post_message(self.Selected(event.option_id))
            self.remove_class("visible")

    def show(self) -> None:
        self.add_class("visible")
        self.filter_commands("")
        if self.option_count > 0:
            self.highlighted = 0

    def hide(self) -> None:
        self.remove_class("visible")

    def is_visible(self) -> bool:
        return "visible" in self.classes
