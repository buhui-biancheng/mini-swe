"""工具调用折叠组件。"""

import json
from textual.widgets import Static, Collapsible
from textual.containers import Vertical


class ToolCallTitle(Static):
    """工具调用标题行：✓/✗ tool_name(args)  duration。"""

    DEFAULT_CSS = """
    ToolCallTitle {
        padding: 0 1;
    }
    .tool-success { color: $success; }
    .tool-error { color: $error; }
    """

    def __init__(
        self,
        tool_name: str,
        args_summary: str,
        success: bool = True,
        duration: float = 0.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._tool_name = tool_name
        self._args_summary = args_summary
        self._success = success
        self._duration = duration

    def render(self) -> str:
        icon = "[green]✓[/green]" if self._success else "[red]✗[/red]"
        dur = f"  {self._duration:.1f}s" if self._duration else ""
        style = "green" if self._success else "red"
        return f"{icon} [{style}]{self._tool_name}[/{style}]({self._args_summary}){dur}"


class ToolCallDetail(Static):
    """工具调用详情（参数 + 结果）。"""

    DEFAULT_CSS = """
    ToolCallDetail {
        padding: 0 1;
        margin: 0 0 0 3;
        max-height: 15;
        overflow-y: auto;
    }
    """

    def __init__(self, arguments: dict, result: str = "", **kwargs):
        super().__init__(**kwargs)
        self._arguments = arguments
        self._result = result

    def render(self) -> str:
        parts = []
        if self._arguments:
            args_json = json.dumps(self._arguments, ensure_ascii=False, indent=2)
            parts.append(f"[dim]参数:[/dim]\n{args_json}")
        if self._result:
            # 截断过长的结果
            display = self._result[:500] + "..." if len(self._result) > 500 else self._result
            parts.append(f"[dim]结果:[/dim]\n{display}")
        return "\n\n".join(parts)


class ToolCallWidget(Collapsible):
    """工具调用折叠组件。"""

    DEFAULT_CSS = """
    ToolCallWidget {
        margin: 0 0;
        border: none;
    }
    ToolCallWidget > .collapse--title {
        padding: 0 1;
    }
    ToolCallWidget > .collapse--contents {
        padding: 0;
    }
    """

    def __init__(
        self,
        tool_name: str,
        arguments: dict | None = None,
        result: str = "",
        success: bool = True,
        duration: float = 0.0,
        collapsed: bool = True,
        **kwargs,
    ):
        arguments = arguments or {}
        args_summary = self._make_args_summary(arguments)

        self._title_widget = ToolCallTitle(tool_name, args_summary, success, duration)
        self._detail_widget = ToolCallDetail(arguments, result)

        super().__init__(
            self._detail_widget,
            title=self._title_widget.render(),
            collapsed=collapsed,
            **kwargs,
        )

    def update_result(self, result: str, success: bool) -> None:
        self._detail_widget._result = result
        self._detail_widget.refresh()
        self._title_widget._success = success
        self._title_widget.refresh()
        # 更新折叠标题
        self.title = self._title_widget.render()

    @staticmethod
    def _make_args_summary(arguments: dict) -> str:
        """生成参数摘要（单行，截断）。"""
        if not arguments:
            return ""
        parts = []
        for k, v in arguments.items():
            val_str = str(v)
            if len(val_str) > 30:
                val_str = val_str[:30] + "..."
            parts.append(f"{k}={val_str}")
        return ", ".join(parts)
