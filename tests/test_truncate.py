# -*- coding: utf-8 -*-
"""工具输出智能截断测试（2026-08-13 边界处理）+ 第三层大输出落盘（2026-08-21）。"""
import sys
sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")
from swe_agent.tools.registry import _truncate_output, _process_output


class TestTruncateOutput:
    def test_short_output_untouched(self):
        assert _truncate_output("abc") == "abc"

    def test_success_output_truncated_head_tail(self):
        """成功输出（点点点）→ 头尾截断 + 省略标记。"""
        out = "." * 20000 + "\n20 passed in 0.1s"
        r = _truncate_output(out, 4000)
        assert len(r) <= 4200
        assert "省略" in r
        assert "20 passed" in r  # 尾部统计保留
        print("✅ 成功输出头尾截断")

    def test_failure_output_keeps_failures_section(self):
        """失败输出 → FAILURES 段完整保留（核心信号永不丢）。"""
        failures = ("=" * 10 + " FAILURES " + "=" * 10 + "\n"
                    "____ test_x ____\n"
                    "tests/test_x.py:5: in test_x\n"
                    "    assert 1 == 2\n"
                    "E   AssertionError: assert 1 == 2\n")
        out = "." * 20000 + "\n" + failures + "\n1 failed in 0.1s"
        r = _truncate_output(out, 4000)
        assert "FAILURES" in r, "FAILURES 段必须保留"
        assert "AssertionError: assert 1 == 2" in r, "失败原因必须保留"
        assert "1 failed" in r
        print("✅ 失败段保留")


class TestOutputTooLarge:
    """第三层（2026-08-21）：超大输出落盘 + output too large 提示，AI 用 view_file 自读。"""

    def test_small_untouched(self):
        assert _process_output("abc", "cmd", 4000, "/tmp") == "abc"

    def test_mid_truncated_with_marker(self):
        """截断阈值 ~ 落盘阈值之间 → 智能截断（带省略标记）。"""
        out = "x" * 9000 + "\nEND"
        r = _process_output(out, "cmd", 4000, "/tmp")
        assert "省略" in r
        assert "output too large" not in r

    def test_large_dumped_to_log(self, tmp_path):
        """超过落盘阈值 → 完整内容落盘 .graph/last_cmd.log + 提示带路径/大小/行数 + 尾部摘录。"""
        out = "line%d\n" * 3000
        out = out % tuple(range(3000))  # ~21KB
        r = _process_output(out, "cmd", 4000, str(tmp_path))
        assert "output too large" in r
        assert "last_cmd.log" in r
        assert "3000 行" in r
        log = tmp_path / ".graph" / "last_cmd.log"
        assert log.exists()
        assert log.read_text() == out  # 完整内容落盘，一字不丢
        assert "line2999" in r  # 尾部摘录保留

    def test_run_test_dumps_to_last_test_log(self, tmp_path):
        """run_test 大输出落盘到 last_test.log（与 FSM 失败落盘同名同义）。"""
        out = "y" * 25000
        r = _process_output(out, "test", 4000, str(tmp_path))
        assert "last_test.log" in r
        assert (tmp_path / ".graph" / "last_test.log").exists()

    def test_dump_threshold_configurable(self, tmp_path):
        """落盘阈值可配置（AgentConfig.output_dump_chars 透传）。"""
        out = "z" * 5000
        r = _process_output(out, "cmd", 4000, str(tmp_path), dump_chars=4000)
        assert "output too large" in r
        assert (tmp_path / ".graph" / "last_cmd.log").exists()

