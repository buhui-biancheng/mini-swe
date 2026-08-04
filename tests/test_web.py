"""Web 控制台单元测试：任务解析、marker 事件翻译、trace → turns。"""

import pytest

from swe_agent.web.payload import _tool_status, build_turns
from swe_agent.web.runner import FixRunner


class TestParseTask:
    def test_simple(self):
        from swe_agent.web.server import parse_task
        bug, cmd = parse_task("examples/bug.py pytest")
        assert bug == "examples/bug.py"
        assert cmd == "pytest"

    def test_multiword_command(self):
        from swe_agent.web.server import parse_task
        bug, cmd = parse_task("examples/bug.py python -m pytest test_x.py")
        assert bug == "examples/bug.py"
        assert cmd == "python -m pytest test_x.py"

    def test_fix_prefix(self):
        from swe_agent.web.server import parse_task
        bug, cmd = parse_task("fix a/b.py pytest -x")
        assert bug == "a/b.py"
        assert cmd == "pytest -x"

    def test_default_command(self):
        from swe_agent.web.server import parse_task
        bug, cmd = parse_task("examples/bug.py")
        assert bug == "examples/bug.py"
        assert cmd == "pytest"


class TestMarkerParsing:
    """FixRunner._handle_line 把 stdout 标记翻译成 waku 事件（graph=None，不建图）。"""

    def _runner(self):
        events = []
        return events, FixRunner(
            project_root=".", bug_file="examples/bug.py",
            test_command="pytest", on_event=lambda k, e: events.append((k, e)),
            graph=None,
        )

    def test_state_transition(self):
        events, r = self._runner()
        r._handle_line("  [STATE] locate (第 1 次尝试)")
        kinds = [k for k, _ in events]
        assert "text" in kinds          # 供 dock 流式显示
        assert "state" in kinds         # 供 trace/Ops 记录
        text = [e["delta"] for k, e in events if k == "text"][0]
        assert "定位" in text           # 中文标签
        state_ev = [e for k, e in events if k == "state"][0]
        assert state_ev["state"] == "locate"
        assert state_ev["attempt"] == 1

    def test_success_synthesizes_state_event(self):
        """FSM 不打印 success/fail 的 [STATE]，由 [SUCCESS] 补发供状态图点亮终点。"""
        events, r = self._runner()
        r._handle_line("[SUCCESS] 修复成功！共尝试 1 次")
        state_ev = [e for k, e in events if k == "state"][0]
        assert state_ev["state"] == "success"
        assert state_ev["attempt"] == 1

    def test_fail_synthesizes_state_event(self):
        events, r = self._runner()
        r._handle_line("[FAIL] 修复失败，已用尽 3 次尝试")
        state_ev = [e for k, e in events if k == "state"][0]
        assert state_ev["state"] == "fail"
        assert state_ev["attempt"] == 3

    def test_tool_call_counts_iteration_and_emits_tool_on_result(self):
        events, r = self._runner()
        r._handle_line('  [TOOL] 调用 view_file({"file_path": "examples/bug.py"})')
        assert r._iterations == 1
        assert r._pending_tool is not None
        r._handle_line("  [TOOL] 成功")
        kinds = [k for k, _ in events]
        assert "tool" in kinds
        tool_ev = [e for k, e in events if k == "tool"][0]
        assert tool_ev["tool"] == "view_file"
        assert tool_ev["output"] == "成功"

    def test_tool_error(self):
        events, r = self._runner()
        r._handle_line('  [TOOL] 调用 run_command({"command": "ls"})')
        r._handle_line("  [TOOL] 错误: command failed")
        tool_ev = [e for k, e in events if k == "tool"][0]
        assert "错误" in tool_ev["output"]

    def test_test_exit_code_becomes_tool_event(self):
        events, r = self._runner()
        r._handle_line("[TEST] exit_code: 1")
        tool_ev = [e for k, e in events if k == "tool"][0]
        assert tool_ev["tool"] == "run_test"
        assert tool_ev["output"] == "exit_code: 1"

    def test_success_sets_final_reply(self):
        events, r = self._runner()
        r._handle_line("[SUCCESS] 修复成功！共尝试 1 次")
        assert "成功" in r._final_reply
        text = [e["delta"] for k, e in events if k == "text"][0]
        assert "SUCCESS" in text


class TestBuildTurns:
    def _ev(self, type_, ts, **kw):
        return {"type": type_, "ts": ts, **kw}

    def test_groups_turn_with_tools(self):
        events = [
            self._ev("turn_start", "2026-08-04T10:00:00", user_message="fix a.py"),
            self._ev("tool", "2026-08-04T10:00:02", tool="view_file", output="ok"),
            self._ev("tool", "2026-08-04T10:00:04", tool="run_test", output="exit_code: 0"),
            self._ev("turn_end", "2026-08-04T10:00:06", reply="修复成功", iterations=2),
        ]
        turns = build_turns(events)
        assert len(turns) == 1
        t = turns[0]
        assert t["user_message"] == "fix a.py"
        assert [x["tool"] for x in t["tools"]] == ["view_file", "run_test"]
        assert t["reply"] == "修复成功"
        assert t["iterations"] == 2
        assert t["latency_ms"] == 6000          # 10:00:00 → 10:00:06
        # 工具状态从输出推导
        assert t["tools"][1]["status"] == "ok"

    def test_unfinished_turn_marked(self):
        events = [
            self._ev("turn_start", "2026-08-04T10:00:00", user_message="fix a.py"),
        ]
        turns = build_turns(events)
        assert len(turns) == 1
        assert turns[0]["unfinished"] is True


class TestToolStatus:
    def test_error_detection(self):
        assert _tool_status("错误: x") == "error"
        assert _tool_status("Error: boom") == "error"
        assert _tool_status("exit_code: 1") == "error"

    def test_ok(self):
        assert _tool_status("成功") == "ok"
        assert _tool_status("exit_code: 0") == "ok"
        assert _tool_status("") == "ok"
