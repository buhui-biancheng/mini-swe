"""TUI 单元测试。"""

import pytest
from swe_agent.tui.app import ChatApp
from swe_agent.tui.models import Message, AppState
from swe_agent.tui.styles import STATUS_COLORS
from swe_agent.tui.widgets import StatusBar, MessageList
from swe_agent.tui.widgets.message_widget import (
    UserMessage,
    AIMessage,
    SystemMessage,
    DiffMessage,
    ToolResultMessage,
    LoadingMessage,
    create_message_widget,
)
from swe_agent.tui.widgets.thinking_widget import ThinkingWidget
from swe_agent.tui.widgets.tool_call_widget import ToolCallWidget


class TestMessage:
    """Message 模型测试。"""

    def test_user_message(self):
        msg = Message(content="hello", msg_type="user")
        assert msg.content == "hello"
        assert msg.msg_type == "user"

    def test_tool_call_message(self):
        msg = Message(
            content="search_function",
            msg_type="tool_call",
            tool_name="search_function",
            tool_args={"name": "add"},
            duration=0.2,
        )
        assert msg.tool_name == "search_function"
        assert msg.tool_args == {"name": "add"}
        assert msg.duration == 0.2

    def test_thinking_message(self):
        msg = Message(content="思考内容", msg_type="thinking", is_collapsed=True)
        assert msg.is_collapsed is True


class TestAppState:
    """AppState 模型测试。"""

    def test_default_state(self):
        state = AppState()
        assert state.status == "idle"
        assert state.step == 0
        assert state.token_usage == 0

    def test_custom_state(self):
        state = AppState(status="running", step=5, token_usage=1000)
        assert state.status == "running"
        assert state.step == 5
        assert state.token_usage == 1000


class TestStatusBar:
    """StatusBar 组件测试。"""

    def test_render(self):
        bar = StatusBar()
        output = bar.render()
        assert "IDLE" in output
        assert "/10" in output

    def test_update_state(self):
        bar = StatusBar()
        state = AppState(status="running", step=3, token_usage=500)
        bar.update_state(state)
        assert bar.state.status == "running"
        assert bar.state.step == 3


class TestMessageWidgets:
    """消息 Widget 测试。"""

    def test_user_message_render(self):
        widget = UserMessage("hello")
        output = widget.render()
        assert ">" in output
        assert "hello" in output

    def test_ai_message_render(self):
        widget = AIMessage("你好")
        output = widget.render()
        assert "你好" in output

    def test_system_message_render(self):
        widget = SystemMessage("系统消息")
        output = widget.render()
        assert "系统消息" in output

    def test_diff_message_render(self):
        widget = DiffMessage("- old\n+ new")
        output = widget.render()
        assert "- old" in output
        assert "+ new" in output

    def test_tool_result_success(self):
        widget = ToolResultMessage("成功", is_error=False)
        output = widget.render()
        assert "成功" in output

    def test_tool_result_error(self):
        widget = ToolResultMessage("失败", is_error=True)
        output = widget.render()
        assert "失败" in output

    def test_loading_message(self):
        widget = LoadingMessage("思考中...")
        output = widget.render()
        assert "思考中..." in output


class TestCreateMessageWidget:
    """create_message_widget 分发测试。"""

    def test_user_type(self):
        msg = Message(content="hi", msg_type="user")
        widget = create_message_widget(msg)
        assert isinstance(widget, UserMessage)

    def test_assistant_type_plain(self):
        msg = Message(content="你好", msg_type="assistant")
        widget = create_message_widget(msg)
        assert isinstance(widget, AIMessage)

    def test_assistant_type_markdown(self):
        msg = Message(content="这是 **加粗** 和 `代码`", msg_type="assistant")
        widget = create_message_widget(msg)
        # 包含 Markdown 语法时应该用 Markdown 渲染
        from textual.widgets import Markdown
        assert isinstance(widget, Markdown)

    def test_thinking_type(self):
        msg = Message(content="思考内容", msg_type="thinking", is_collapsed=True)
        widget = create_message_widget(msg)
        assert isinstance(widget, ThinkingWidget)

    def test_tool_call_type(self):
        msg = Message(
            content="search_function",
            msg_type="tool_call",
            tool_name="search_function",
            tool_args={"name": "add"},
        )
        widget = create_message_widget(msg)
        assert isinstance(widget, ToolCallWidget)

    def test_system_type(self):
        msg = Message(content="系统消息", msg_type="system")
        widget = create_message_widget(msg)
        assert isinstance(widget, SystemMessage)

    def test_diff_type(self):
        msg = Message(content="- old\n+ new", msg_type="diff")
        widget = create_message_widget(msg)
        assert isinstance(widget, DiffMessage)


class TestToolCallWidget:
    """ToolCallWidget 测试。"""

    def test_args_summary(self):
        summary = ToolCallWidget._make_args_summary({"name": "add", "file": "test.py"})
        assert "name=add" in summary
        assert "file=test.py" in summary

    def test_args_summary_empty(self):
        summary = ToolCallWidget._make_args_summary({})
        assert summary == ""

    def test_args_summary_truncate(self):
        long_val = "x" * 50
        summary = ToolCallWidget._make_args_summary({"key": long_val})
        assert "..." in summary


class TestChatApp:
    """ChatApp 测试。"""

    def test_init(self):
        app = ChatApp()
        assert app.state.status == "idle"
        assert app.state.step == 0

    def test_add_user_message(self):
        app = ChatApp()
        app.add_user_message("hello")

    def test_add_ai_message(self):
        app = ChatApp()
        app.add_ai_message("你好")

    def test_set_status(self):
        app = ChatApp()
        app.set_status("running", step=3, token_usage=500)
        assert app.state.status == "running"
        assert app.state.step == 3
        assert app.state.token_usage == 500


class TestStyles:
    """样式测试。"""

    def test_status_colors_exist(self):
        assert "idle" in STATUS_COLORS
        assert "success" in STATUS_COLORS
        assert "fail" in STATUS_COLORS
