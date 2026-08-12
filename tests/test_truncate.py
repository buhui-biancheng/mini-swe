# -*- coding: utf-8 -*-
"""工具输出智能截断测试（2026-08-13 边界处理）。"""
import sys
sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")
from swe_agent.tools.registry import _truncate_output


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
