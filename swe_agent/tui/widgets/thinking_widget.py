"""思考链折叠组件。"""

from textual.widgets import Static, Collapsible
from textual.containers import Vertical


class ThinkingContent(Static):
    """思考链内容（dim 样式）。"""

    DEFAULT_CSS = """
    ThinkingContent {
        color: $text-muted;
        padding: 0 1;
        margin: 0 0 0 2;
        max-height: 20;
        overflow-y: auto;
    }
    """

    def __init__(self, content: str, **kwargs):
        super().__init__(**kwargs)
        self._content = content

    def render(self) -> str:
        return self._content

    def update_content(self, content: str) -> None:
        self._content = content
        self.refresh(layout=True)


class ThinkingWidget(Collapsible):
    """思考链折叠组件。"""

    DEFAULT_CSS = """
    ThinkingWidget {
        margin: 0;
        border: none;
    }
    ThinkingWidget > .collapse--title {
        color: $text-muted;
        padding: 0 1;
    }
    ThinkingWidget > .collapse--contents {
        padding: 0;
    }
    """

    def __init__(self, content: str = "", collapsed: bool = True, **kwargs):
        self._content_widget = ThinkingContent(content)
        super().__init__(
            self._content_widget,
            title="💭 思考链",
            collapsed=collapsed,
            **kwargs,
        )

    def update_content(self, content: str) -> None:
        self._content_widget.update_content(content)
