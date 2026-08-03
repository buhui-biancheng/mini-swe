"""TokenBudget 单元测试（Phase 2 模块 E3）。"""

from swe_agent.fsm.token_budget import TokenBudget


class TestTokenBudget:
    def test_add_usage(self):
        b = TokenBudget(limit=100)
        assert b.add({"total_tokens": 60}) == 60
        assert b.total == 60

    def test_add_without_total(self):
        b = TokenBudget(limit=100)
        b.add({"prompt_tokens": 10, "completion_tokens": 20})
        assert b.total == 30

    def test_exceeded(self):
        b = TokenBudget(limit=100)
        assert not b.exceeded()
        b.add({"total_tokens": 60})
        b.add({"total_tokens": 50})
        assert b.exceeded()

    def test_remaining(self):
        b = TokenBudget(limit=100)
        b.add({"total_tokens": 30})
        assert b.remaining() == 70
        b.add({"total_tokens": 100})
        assert b.remaining() == 0

    def test_reset(self):
        b = TokenBudget(limit=100)
        b.add({"total_tokens": 200})
        b.reset()
        assert b.total == 0
        assert not b.exceeded()

    def test_add_empty_usage(self):
        b = TokenBudget(limit=100)
        assert b.add(None) == 0
        assert b.add({}) == 0
