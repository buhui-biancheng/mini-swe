"""统一中断处理：取消/超时/致命错误共用同一套回滚逻辑（Phase 2 模块 E）。

取消是事件，不是状态。不需要 CANCELLING 状态：
    1. 执行回滚（恢复快照 + 清理）
    2. 强制进入终止状态（不经过状态转移图）
    3. 记录退出原因（取消/超时/错误/预算超限）
"""


class CancelReason:
    """退出原因（不同中断类型只在记录原因时区分）。"""
    USER = "user_cancelled"
    TIMEOUT = "timeout"
    API_ERROR = "api_error"
    BUDGET_EXCEEDED = "token_budget_exceeded"
    FATAL = "fatal_error"
    NO_PROGRESS = "no_progress"


def handle_cancel(fsm, reason: str = CancelReason.USER) -> None:
    """统一中断处理。

    Args:
        fsm: AgentFSM 实例（鸭子类型：需 checkpoint / logger / machine）
        reason: 退出原因（CancelReason 常量）
    """
    # 1. 回滚：恢复所有被编辑文件的初始快照
    try:
        checkpoint = getattr(fsm, "checkpoint", None)
        if checkpoint is not None and getattr(checkpoint, "snapshots", None):
            restored = 0
            for path in list(checkpoint.snapshots):
                if checkpoint.restore(path):
                    restored += 1
            fsm.logger.snapshot_restored(f"<{restored} files>")
    except Exception as e:  # 取消清理不能抛穿
        fsm.logger.error(f"取消回滚异常: {e}", reason=reason)

    # 2. 记录退出原因
    fsm._cancel_reason = reason
    fsm.logger.error(f"任务中断: {reason}", reason=reason)

    # 3. 强制进入终止状态（不经过状态转移图）
    try:
        fsm.machine.set_state("fail")
    except Exception:
        pass
