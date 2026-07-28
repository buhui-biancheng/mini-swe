"""TUI 单元测试。"""

import pytest
from swe_agent.tui.app import ChatApp, MessageWidget, StatusBar, MessageList
from swe_agent.tui.models import Message, AppState
from swe_agent.tui.styles import STATUS_COLORS


class TestMessage:
    """Message 模型测试。"""

    def test_user_message(self):
        """测试用户消息。"""
        msg = Message(content="hello", msg_type="user")
        assert msg.content == "hello"
        assert msg.msg_type == "user"

    def test_tool_call_message(self):
        """测试工具调用消息。"""
        msg = Message(
            content="search_function",
            msg_type="tool_call",
            tool_name="search_function",
            duration=0.2,
        )
        assert msg.tool_name == "search_function"
        assert msg.duration == 0.2

    def test_thinking_message(self):
        """测试思考链消息。"""
        msg = Message(content="思考内容", msg_type="thinking", is_collapsed=True)
        assert msg.is_collapsed is True


class TestAppState:
    """AppState 模型测试。"""

    def test_default_state(self):
        """测试默认状态。"""
        state = AppState()
        assert state.status == "idle"
        assert state.step == 0
        assert state.token_usage == 0

    def test_custom_state(self):
        """测试自定义状态。"""
        state = AppState(status="running", step=5, token_usage=1000)
        assert state.status == "running"
        assert state.step == 5
        assert state.token_usage == 1000


class TestStatusBar:
    """StatusBar 组件测试。"""

    def test_render(self):
        """测试渲染。"""
        bar = StatusBar()
        output = bar.render()
        assert "IDLE" in output
        assert "0/10" in output

    def test_update_state(self):
        """测试状态更新。"""
        bar = StatusBar()
        state = AppState(status="running", step=3, token_usage=500)
        bar.update_state(state)
        assert bar.state.status == "running"
        assert bar.state.step == 3


class TestMessageWidget:
    """MessageWidget 组件测试。"""

    def test_user_message_render(self):
        """测试用户消息渲染。"""
        msg = Message(content="hello", msg_type="user")
        widget = MessageWidget(msg)
        output = widget.render()
        assert "[You]" in output
        assert "hello" in output

    def test_ai_message_render(self):
        """测试 AI 消息渲染。"""
        msg = Message(content="你好", msg_type="assistant")
        widget = MessageWidget(msg)
        output = widget.render()
        assert "[AI]" in output
        assert "你好" in output

    def test_tool_call_ok_render(self):
        """测试工具调用成功渲染。"""
        msg = Message(
            content="search_function",
            msg_type="tool_call",
            tool_name="search_function",
            duration=0.2,
        )
        widget = MessageWidget(msg)
        output = widget.render()
        assert "[OK]" in output
        assert "search_function" in output
        assert "0.2s" in output

    def test_tool_call_error_render(self):
        """测试工具调用失败渲染。"""
        msg = Message(
            content="run_test",
            msg_type="tool_call",
            tool_name="run_test",
            is_error=True,
        )
        widget = MessageWidget(msg)
        output = widget.render()
        assert "[ERR]" in output

    def test_thinking_collapsed_render(self):
        """测试思考链折叠渲染。"""
        msg = Message(content="思考内容", msg_type="thinking", is_collapsed=True)
        widget = MessageWidget(msg)
        output = widget.render()
        assert "[+]" in output

    def test_thinking_expanded_render(self):
        """测试思考链展开渲染。"""
        msg = Message(content="思考内容", msg_type="thinking", is_collapsed=False)
        widget = MessageWidget(msg)
        output = widget.render()
        assert "[-]" in output
        assert "思考内容" in output

    def test_diff_render(self):
        """测试差异渲染。"""
        msg = Message(content="- old\n+ new", msg_type="diff")
        widget = MessageWidget(msg)
        output = widget.render()
        assert "- old" in output
        assert "+ new" in output


class TestChatApp:
    """ChatApp 测试。"""

    def test_init(self):
        """测试初始化。"""
        app = ChatApp()
        assert app.state.status == "idle"
        assert app.state.step == 0

    def test_add_user_message(self):
        """测试添加用户消息（未 mount 时不崩溃）。"""
        app = ChatApp()
        # 未 mount 时 message_list 是 None，方法应该安全处理
        app.add_user_message("hello")
        # 不崩溃就是成功

    def test_add_ai_message(self):
        """测试添加 AI 消息（未 mount 时不崩溃）。"""
        app = ChatApp()
        app.add_ai_message("你好")
        # 不崩溃就是成功

    def test_set_status(self):
        """测试设置状态。"""
        app = ChatApp()
        app.set_status("running", step=3, token_usage=500)
        assert app.state.status == "running"
        assert app.state.step == 3
        assert app.state.token_usage == 500


class TestStyles:
    """样式测试。"""

    def test_status_colors_exist(self):
        """测试状态颜色定义。"""
        assert "init" in STATUS_COLORS
        assert "locate" in STATUS_COLORS
        assert "patch" in STATUS_COLORS
        assert "test" in STATUS_COLORS
        assert "success" in STATUS_COLORS
        assert "fail" in STATUS_COLORS
