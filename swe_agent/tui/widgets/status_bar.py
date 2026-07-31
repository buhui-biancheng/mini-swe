"""分区状态栏组件。"""

from textual.widgets import Static
from textual.reactive import reactive

from ..models import AppState


class StatusBar(Static):
    """分区状态栏：状态 | 步骤 | Token | 耗时 | 模型。"""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: top;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
        border-bottom: solid $primary-background-darken-2;
    }
    """

    state: reactive[AppState] = reactive(AppState(), layout=False)

    # 状态颜色映射
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

    def update_state(self, state: AppState) -> None:
        self.state = state
        self.refresh()

    def render(self) -> str:
        s = self.state
        color = self.STATUS_COLORS.get(s.status, "white")
        status_text = s.status.upper()

        parts = [
            f"[{color}]{status_text}[/{color}]",
            f"[dim]{s.step}/{s.max_steps}[/dim]",
            f"[dim]{s.token_usage:,} tok[/dim]",
            f"[dim]{s.elapsed_time:.1f}s[/dim]",
        ]
        return " · ".join(parts)
