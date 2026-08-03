"""提示词分级架构单元测试（Phase 2 模块 D2）。"""

from swe_agent.prompts import PromptManager


class TestPromptManager:
    def setup_method(self):
        self.pm = PromptManager()

    def test_base_always_present(self):
        sys = self.pm.build_system(state="locate", mode="dp")
        assert "你是一个专业的代码修复助手" in sys

    def test_locate_state_injects_locate_protocol(self):
        sys = self.pm.build_system(state="locate", mode="dp")
        assert "定位策略" in sys
        assert "探索模式" not in sys  # 非 greedy 不注入

    def test_patch_state_injects_patch_strategy(self):
        sys = self.pm.build_system(state="patch", mode="dp")
        assert "修复策略" in sys

    def test_test_fail_injects_error_locating(self):
        sys = self.pm.build_system(state="locate", mode="dp", last_test_failed=True)
        assert "错误定位协议" in sys
        assert "log_start_line" in sys

    def test_greedy_mode_injects_degrade(self):
        sys = self.pm.build_system(state="locate", mode="greedy")
        assert "探索模式" in sys

    def test_rollback_notice_injects_rollback(self):
        sys = self.pm.build_system(state="locate", mode="dp", rollback_notice=True)
        assert "修改路径已回退" in sys

    def test_success_state_injects_success(self):
        sys = self.pm.build_system(state="success", mode="dp")
        assert "修复成功" in sys

    def test_token_overhead_is_bounded(self):
        """system 每轮从零拼，只含当前相关的块（重建而非追加）。"""
        minimal = self.pm.build_system(state="locate", mode="dp")
        full = self.pm.build_system(
            state="locate", mode="greedy", last_test_failed=True, rollback_notice=True
        )
        assert len(minimal) < len(full)
        # 各块互不重复加载（缓存）
        assert len(self.pm._cache) == len(set(self.pm._cache))
