"""监控主循环：调度层。

逻辑完全一样，只管调度。
"""

import time
from typing import Optional, Callable

from .config import WatchdogConfig
from .decision_engine import DecisionEngine, Action
from .interfaces import IProcessManager, IHeartbeatStore, ICheckpointStore


class WatchdogOrchestrator:
    """看门狗编排器。"""

    def __init__(
        self,
        config: WatchdogConfig,
        process_manager: IProcessManager,
        heartbeat_store: IHeartbeatStore,
        checkpoint_store: ICheckpointStore,
        on_restart: Optional[Callable[[str], None]] = None,
        on_terminate: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.engine = DecisionEngine(config)
        self.proc_mgr = process_manager
        self.hb_store = heartbeat_store
        self.checkpoint_store = checkpoint_store
        self.on_restart = on_restart
        self.on_terminate = on_terminate
        self.should_exit = False

    def run(self, max_iterations: int = -1) -> None:
        """运行监控循环。

        Args:
            max_iterations: 最大迭代次数，-1 表示无限循环
        """
        iteration = 0
        while not self.should_exit:
            if max_iterations > 0 and iteration >= max_iterations:
                break

            time.sleep(self.config.check_interval_sec)
            iteration += 1

            # 1. 获取状态
            alive = self.proc_mgr.is_alive()
            hb = self.hb_store.read()
            token_usage = hb.get("tokens", 0) if hb else 0

            # 2. 调用决策引擎
            action, reason = self.engine.assess(alive, hb, token_usage)

            # 3. 执行动作
            if action == Action.IGNORE:
                continue
            elif action == Action.BACKOFF:
                self._handle_backoff()
            elif action == Action.RESTART:
                self._restart_agent(reason)
            elif action == Action.TERMINATE:
                self._terminate_agent(reason)
                break

    def _handle_backoff(self) -> None:
        """处理退避等待。"""
        wait_sec = min(
            self.config.backoff_base_sec ** len(self.engine.restart_timestamps),
            self.config.max_backoff_sec,
        )
        time.sleep(wait_sec)

    def _restart_agent(self, reason: str) -> None:
        """重启 Agent。"""
        self.proc_mgr.terminate(graceful=True)
        self.engine.record_restart()
        self.checkpoint_store.save({"restart_reason": reason, "restart_time": time.time()})

        if self.on_restart:
            self.on_restart(reason)

        # 恢复检查点
        checkpoint = self.checkpoint_store.load()
        checkpoint_path = None
        if checkpoint and "path" in checkpoint:
            checkpoint_path = checkpoint["path"]

        self.proc_mgr.start(resume_checkpoint=checkpoint_path)

    def _terminate_agent(self, reason: str) -> None:
        """终止 Agent。"""
        self.proc_mgr.terminate(graceful=False)

        if self.on_terminate:
            self.on_terminate(reason)

        self.should_exit = True

        # 生成事后报告
        with open("post_mortem.log", "w") as f:
            f.write(f"Terminated due to: {reason}\n")
            f.write(f"Time: {time.time()}\n")
