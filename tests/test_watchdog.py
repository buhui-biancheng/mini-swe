"""DecisionEngine 单元测试。"""

import time
import pytest
from swe_agent.watchdog import DecisionEngine, WatchdogConfig, Action


class TestDecisionEngine:
    """DecisionEngine 测试类。"""

    def test_healthy_state(self):
        """测试正常状态。"""
        config = WatchdogConfig()
        engine = DecisionEngine(config)

        heartbeat = {"timestamp": time.time(), "step": 1, "action_hash": "abc"}
        action, reason = engine.assess(True, heartbeat, 0)

        assert action == Action.IGNORE
        assert reason == "healthy"

    def test_process_crashed(self):
        """测试进程崩溃。"""
        config = WatchdogConfig()
        engine = DecisionEngine(config)

        action, reason = engine.assess(False, None, 0)

        assert action == Action.RESTART
        assert reason == "process_crashed"

    def test_heartbeat_timeout(self):
        """测试心跳超时。"""
        config = WatchdogConfig(heartbeat_timeout_sec=10)
        engine = DecisionEngine(config)

        heartbeat = {"timestamp": time.time() - 20, "step": 1}
        action, reason = engine.assess(True, heartbeat, 0)

        assert action == Action.RESTART
        assert reason == "heartbeat_timeout"

    def test_step_stalled(self):
        """测试步数卡死。"""
        config = WatchdogConfig(stall_step_threshold=3)
        engine = DecisionEngine(config)

        # 先初始化 last_step
        heartbeat = {"timestamp": time.time(), "step": 1}
        engine.assess(True, heartbeat, 0)

        # 模拟步数不变（连续 3 次相同）
        for _ in range(3):
            heartbeat = {"timestamp": time.time(), "step": 1}
            action, reason = engine.assess(True, heartbeat, 0)

        assert action == Action.RESTART
        assert reason == "step_stalled"

    def test_budget_exceeded(self):
        """测试预算超限。"""
        config = WatchdogConfig(budget_token_limit=1000)
        engine = DecisionEngine(config)

        heartbeat = {"timestamp": time.time(), "step": 1}
        action, reason = engine.assess(True, heartbeat, 2000)

        assert action == Action.TERMINATE
        assert reason == "budget_exceeded"

    def test_tool_call_repetition(self):
        """测试工具调用重复检测。"""
        engine = DecisionEngine(WatchdogConfig())

        # 相同工具+相同参数连续 3 次
        for _ in range(2):
            assert engine.record_tool_call("search_function", {"name": "add"}) is False
        assert engine.record_tool_call("search_function", {"name": "add"}) is True

    def test_tool_call_different_args(self):
        """测试不同参数不触发。"""
        engine = DecisionEngine(WatchdogConfig())

        for name in ["add", "mul", "div", "sub", "pow"]:
            assert engine.record_tool_call("search_function", {"name": name}) is False

    def test_state_entry_repetition(self):
        """测试状态进入重复检测。

        2026-08-13 修复：同一状态连续 6 次才触发（原 5 次会误杀正常回环）。
        """
        engine = DecisionEngine(WatchdogConfig())

        for _ in range(5):
            assert engine.record_state_entry("locate") is False
        assert engine.record_state_entry("locate") is True

    def test_edit_tracking(self):
        """测试编辑追踪。"""
        engine = DecisionEngine(WatchdogConfig())

        engine.record_edit("/tmp/test.py", True)
        engine.record_edit("/tmp/test.py", False)

        assert engine.edit_count == 2
        assert engine.successful_edits == 1
        assert engine.rounds_without_edit == 1

    def test_check_stuck_no_edit(self):
        """测试无编辑卡住检测。"""
        config = WatchdogConfig(max_rounds_without_edit=3)
        engine = DecisionEngine(config)

        for _ in range(3):
            engine.record_edit("/tmp/test.py", False)

        stuck, reason = engine.check_stuck()
        assert stuck is True
        assert reason == "no_edit_rounds"

    def test_check_stuck_low_success_rate(self):
        """测试低成功率卡住检测。"""
        config = WatchdogConfig(min_edit_success_rate=0.5)
        engine = DecisionEngine(config)

        # 5 次编辑，只有 1 次成功（20% < 50%）
        engine.record_edit("/tmp/test.py", True)
        for _ in range(4):
            engine.record_edit("/tmp/test.py", False)

        stuck, reason = engine.check_stuck()
        assert stuck is True
        assert reason == "low_edit_success_rate"

    def test_reset(self):
        """测试重置。"""
        engine = DecisionEngine(WatchdogConfig())

        engine.record_tool_call("search_function", {"name": "add"})
        engine.record_state_entry("locate")
        engine.record_edit("/tmp/test.py", True)

        engine.reset()

        assert len(engine.tool_call_history) == 0
        assert len(engine.state_entry_history) == 0
        assert engine.edit_count == 0

    def test_restart_throttling(self):
        """测试重启限流。"""
        config = WatchdogConfig(max_restarts_per_5min=2)
        engine = DecisionEngine(config)

        # 记录 2 次重启
        engine.record_restart()
        engine.record_restart()

        # 进程正常，但被限流
        heartbeat = {"timestamp": time.time(), "step": 1}
        action, reason = engine.assess(True, heartbeat, 0)

        assert action == Action.BACKOFF
        assert reason == "restart_throttled"
