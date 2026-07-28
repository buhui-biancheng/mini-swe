"""看门狗配置。"""

from dataclasses import dataclass


@dataclass
class WatchdogConfig:
    """看门狗配置。"""
    # 心跳超时
    heartbeat_timeout_sec: int = 30

    # 检查周期
    check_interval_sec: int = 5

    # 重启限制
    max_restarts_per_5min: int = 5

    # 退避策略
    backoff_base_sec: int = 2
    max_backoff_sec: int = 60

    # Token 预算
    budget_token_limit: int = 100000

    # 卡死检测
    stall_step_threshold: int = 3

    # 循环检测（滑动窗口）
    loop_window_size: int = 10
    loop_similarity_ratio: float = 0.6

    # 工具调用限制
    max_same_tool_calls: int = 8
    max_same_state_entries: int = 10

    # 无进展检测
    max_rounds_without_edit: int = 6
    min_edit_success_rate: float = 0.2
