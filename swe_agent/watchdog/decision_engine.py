"""纯决策引擎：无外部依赖，纯逻辑。

这个模块可以原封不动地复制到 TypeScript 项目中。
"""

import time
from enum import Enum
from typing import Optional
from collections import deque

from .config import WatchdogConfig


class Action(Enum):
    """决策动作。"""
    IGNORE = "ignore"          # 一切正常
    RESTART = "restart"        # 需要重启
    TERMINATE = "terminate"    # 彻底终止
    BACKOFF = "backoff"        # 等待退避后再试


class HealthStatus(Enum):
    """健康状态。"""
    HEALTHY = "healthy"
    STALLED = "stalled"        # 假死
    DEAD = "dead"              # 进程已退出
    LOOPING = "looping"        # 检测到死循环
    BUDGET_EXCEEDED = "budget_exceeded"


class DecisionEngine:
    """纯决策引擎：根据输入状态返回决策。

    未来 TS 完全照抄这个逻辑，一字不改。
    """

    def __init__(self, config: WatchdogConfig):
        self.config = config

        # 循环检测（滑动窗口）
        self.action_history: deque[str] = deque(maxlen=config.loop_window_size)

        # 卡死检测
        self.last_step = -1
        self.stall_counter = 0

        # 重启限流
        self.restart_timestamps: deque[float] = deque(maxlen=5)

        # 工具调用追踪
        self.tool_call_history: deque[str] = deque(maxlen=20)

        # 状态进入追踪
        self.state_entry_history: deque[str] = deque(maxlen=20)

        # 编辑追踪
        self.edit_count = 0
        self.successful_edits = 0
        self.rounds_without_edit = 0

    def assess(
        self,
        process_alive: bool,
        heartbeat: Optional[dict],
        current_token_usage: int = 0,
    ) -> tuple[Action, str]:
        """纯函数：根据输入状态返回 (动作, 原因)。

        Args:
            process_alive: 进程是否存活
            heartbeat: 心跳数据（可选）
            current_token_usage: 当前 token 使用量

        Returns:
            (动作, 原因)
        """
        # --- L1: 进程已死 ---
        if not process_alive:
            return Action.RESTART, "process_crashed"

        # --- L2: 无心跳数据 ---
        if heartbeat is None:
            return Action.IGNORE, "waiting_for_first_heartbeat"

        now = time.time()

        # --- L3: 心跳超时 ---
        if now - heartbeat.get("timestamp", 0) > self.config.heartbeat_timeout_sec:
            return Action.RESTART, "heartbeat_timeout"

        # --- L4: 步数卡死检测 ---
        current_step = heartbeat.get("step", 0)
        if current_step == self.last_step:
            self.stall_counter += 1
        else:
            self.stall_counter = 0
            self.last_step = current_step

        if self.stall_counter >= self.config.stall_step_threshold:
            self.stall_counter = 0
            return Action.RESTART, "step_stalled"

        # --- L5: 循环检测（滑动窗口重复率） ---
        action_hash = heartbeat.get("action_hash", "")
        if action_hash:
            self.action_history.append(action_hash)
            if len(self.action_history) == self.config.loop_window_size:
                unique_count = len(set(self.action_history))
                repeat_ratio = 1 - (unique_count / self.config.loop_window_size)
                if repeat_ratio >= self.config.loop_similarity_ratio:
                    self.action_history.clear()
                    return Action.RESTART, "detected_loop"

        # --- L6: 预算熔断 ---
        if current_token_usage > self.config.budget_token_limit:
            return Action.TERMINATE, "budget_exceeded"

        # --- L7: 限流检查 ---
        if self._is_throttled():
            return Action.BACKOFF, "restart_throttled"

        return Action.IGNORE, "healthy"

    def _is_throttled(self) -> bool:
        """计算 5 分钟内重启次数是否超限。"""
        now = time.time()
        while self.restart_timestamps and now - self.restart_timestamps[0] > 300:
            self.restart_timestamps.popleft()
        return len(self.restart_timestamps) >= self.config.max_restarts_per_5min

    def record_restart(self):
        """记录一次重启。"""
        self.restart_timestamps.append(time.time())

    def record_tool_call(self, tool_name: str, arguments: dict) -> bool:
        """记录工具调用，返回 True 表示检测到重复调用。

        检测逻辑：
        1. 最近 3 次调用完全相同（工具+参数都一样）→ 触发
        2. 最近 5 次调用中，完全相同的调用出现 3 次以上 → 触发
        不同参数的同一工具调用不算重复。
        """
        # 2026-08-13：run_test 重复是正常流程（改完再测）——watchdog 不拦；
        # submit 是交卷信号——也不拦
        if tool_name in ("run_test", "submit"):
            return False
        import hashlib
        args_hash = hashlib.md5(
            f"{tool_name}:{sorted(arguments.items())}".encode()
        ).hexdigest()[:8]
        key = f"{tool_name}:{args_hash}"

        self.tool_call_history.append(key)

        # 检测：最近 3 次调用完全相同（工具+参数）
        if len(self.tool_call_history) >= 3:
            last3 = list(self.tool_call_history)[-3:]
            if len(set(last3)) == 1:
                return True

        # 检测：最近 10 次调用中，完全相同的调用出现 4 次以上
        if len(self.tool_call_history) >= 5:
            last_n = list(self.tool_call_history)[-10:]
            from collections import Counter
            counts = Counter(last_n)
            most_common_count = counts.most_common(1)[0][1]
            if most_common_count >= 4:
                return True

        return False

    def record_state_entry(self, state: str) -> bool:
        # 2026-08-13：test→patch→test 是正常流（官方模式）——状态检测不拦 test
        if state == "test":
            return False
        """记录状态进入，返回 True 表示检测到异常。"""
        self.state_entry_history.append(state)

        # 检测：最近 5 次进入同一状态
        if len(self.state_entry_history) >= 5:
            last5 = list(self.state_entry_history)[-5:]
            if len(set(last5)) == 1 and last5[0] == state:
                return True

        return False

    def record_edit(self, file_path: str, success: bool) -> None:
        """记录编辑操作。"""
        self.edit_count += 1
        if success:
            self.successful_edits += 1
            self.rounds_without_edit = 0
        else:
            self.rounds_without_edit += 1

    def check_stuck(self) -> tuple[bool, str]:
        """检查是否卡住了。

        Returns:
            (是否卡住, 原因)
        """
        # 条件 1：连续多轮没有编辑
        if self.rounds_without_edit >= self.config.max_rounds_without_edit:
            return True, "no_edit_rounds"

        # 条件 2：编辑次数过多但成功率太低
        if self.edit_count >= 5:
            success_rate = self.successful_edits / self.edit_count
            if success_rate < self.config.min_edit_success_rate:
                return True, "low_edit_success_rate"

        return False, "ok"

    def reset(self):
        """重置所有状态。"""
        self.action_history.clear()
        self.tool_call_history.clear()
        self.state_entry_history.clear()
        self.last_step = -1
        self.stall_counter = 0
        self.edit_count = 0
        self.successful_edits = 0
        self.rounds_without_edit = 0
