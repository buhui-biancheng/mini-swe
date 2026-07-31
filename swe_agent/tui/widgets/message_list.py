"""消息列表容器组件。"""

from textual.widgets import Static
from textual.containers import ScrollableContainer

from ..models import Message
from .message_widget import create_message_widget, LoadingMessage


class MessageList(ScrollableContainer):
    """消息列表：支持滚动、自动到底部。"""

    DEFAULT_CSS = """
    MessageList {
        height: 1fr;
        overflow-y: auto;
        padding: 0;
    }
    """

    def __init__(self):
        super().__init__()
        self.loading_widget: LoadingMessage | None = None

    def add_message(self, msg: Message, on_toggle_thinking=None) -> Static:
        """添加一条消息到列表。"""
        widget = create_message_widget(msg, on_toggle_thinking=on_toggle_thinking)
        self.mount(widget)
        # 延迟滚动，确保布局完成
        self.call_after_refresh(lambda: self.scroll_end(animate=False))
        return widget

    def show_loading(self, text: str = "思考中...") -> None:
        """显示加载指示器。"""
        self.hide_loading()
        self.loading_widget = LoadingMessage(text)
        self.mount(self.loading_widget)
        self.call_after_refresh(lambda: self.scroll_end(animate=False))

    def hide_loading(self) -> None:
        """隐藏加载指示器。"""
        if self.loading_widget:
            self.loading_widget.remove()
            self.loading_widget = None

    def clear_messages(self) -> None:
        """清空所有消息。"""
        self.remove_children()
        self.loading_widget = None
