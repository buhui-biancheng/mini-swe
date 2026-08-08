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
        # 成本明细（2026-08-08 评测加）：缓存命中的输入远便宜于未命中
        self.prompt_total = 0       # 输入 token（含缓存命中）
        self.completion_total = 0   # 输出 token
        self.cached_total = 0       # 缓存命中 token（DeepSeek prefix caching）

    def add(self, usage: Optional[dict]) -> int:
        """累加一次 LLM 调用的 usage，返回累计总量。"""
        if not usage:
            return self.total
        total = usage.get("total_tokens")
        if total is None:
            total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        self.total += int(total or 0)
        # 明细：DeepSeek usage.prompt_tokens_details.cached_tokens
        pt = int(usage.get("prompt_tokens", 0) or 0)
        ct = int(usage.get("completion_tokens", 0) or 0)
        cached = 0
        # DeepSeek 官方字段（2026-08-08 对照文档）：prompt_cache_hit_tokens 顶层字段
        cached = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        if cached == 0:
            details = usage.get("prompt_tokens_details") or {}
            # openai 兼容层：Pydantic 对象或 dict
            if hasattr(details, "cached_tokens"):
                cached = int(details.cached_tokens or 0)
            elif isinstance(details, dict):
                cached = int(details.get("cached_tokens", 0) or 0)
        self.prompt_total += pt
        self.completion_total += ct
        self.cached_total += cached
        return self.total

    def estimate_cost(self, price_in=1.0, price_cached=0.02, price_out=2.0) -> float:
        """估算成本（元）。默认 DeepSeek 定价：输入未命中 1 元/百万、命中 0.02、输出 2。

        注意：价格参数需按官方文档/实测校准（缓存命中价可能随模型变化）；
        cached 异常（API 未返回或大于 prompt）时按 0 处理，避免负数。
        """
        uncached = max(0, self.prompt_total - self.cached_total)
        p = uncached / 1e6 * price_in
        c = self.cached_total / 1e6 * price_cached
        o = self.completion_total / 1e6 * price_out
        return round(p + c + o, 4)

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
