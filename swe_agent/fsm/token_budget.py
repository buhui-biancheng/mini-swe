"""TokenBudget：跨多轮对话汇总 LLM token 用量（Phase 2 模块 E3）。

认知保持保留历史会加剧 token 消耗，需要在多轮对话中追踪总用量：
    - 每次 LLM 调用后 add(usage)
    - exceeded() 超限 → FSM 熔断（终止）或降级（Greedy 清空上下文省 token）
与 Watchdog 的 budget_token_limit 合并到 AgentConfig 统一管理。
"""

from typing import Optional


class TokenBudgetExceeded(Exception):
    """Token 预算超限（LLM 调用中途抛出，FSM 接住降级/熔断）。"""


class TokenBudget:
    """Token 预算管理。"""

    def __init__(self, limit: Optional[int] = 100000):
        self.limit = limit  # None = 无上限（评测用）
        self.total = 0

    def add(self, usage: Optional[dict]) -> int:
        """累加一次 LLM 调用的 usage，返回累计总量。"""
        if not usage:
            return self.total
        total = usage.get("total_tokens")
        if total is None:
            total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        self.total += int(total or 0)
        return self.total

    def exceeded(self) -> bool:
        """是否超限（limit=None 永不超限）。"""
        if self.limit is None:
            return False
        return self.total > self.limit

    def remaining(self) -> int:
        """剩余额度（limit=None 返回 -1 表示无限制）。"""
        if self.limit is None:
            return -1
        return max(0, self.limit - self.total)

    def reset(self) -> None:
        """归零（Greedy 降级/新任务时）。"""
        self.total = 0
