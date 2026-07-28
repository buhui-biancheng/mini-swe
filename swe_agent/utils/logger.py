"""结构化日志模块：使用 structlog 实现 JSON 格式日志。"""

import logging
import sys
from typing import Any
import structlog


def setup_logger(
    level: str = "INFO",
    json_format: bool = True,
) -> structlog.stdlib.BoundLogger:
    """配置结构化日志。

    Args:
        level: 日志级别（DEBUG/INFO/WARNING/ERROR）
        json_format: 是否使用 JSON 格式（False 则使用彩色控制台格式）

    Returns:
        配置好的 structlog logger
    """
    # 配置 structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 配置标准库 logging
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    return structlog.get_logger()


class AgentLogger:
    """Agent 专用日志器，封装常用的日志方法。"""

    def __init__(self, name: str = "swe-agent"):
        self.logger = structlog.get_logger(name)

    def init(self, bug_file: str, test_command: str, **kwargs: Any) -> None:
        """记录 Agent 初始化。"""
        self.logger.info(
            "agent_init",
            bug_file=bug_file,
            test_command=test_command,
            **kwargs,
        )

    def state_enter(self, state: str, attempt: int) -> None:
        """记录状态进入。"""
        self.logger.info(
            "state_enter",
            state=state,
            attempt=attempt,
        )

    def tool_call(self, tool_name: str, arguments: dict, success: bool) -> None:
        """记录工具调用。"""
        self.logger.info(
            "tool_call",
            tool_name=tool_name,
            arguments=arguments,
            success=success,
        )

    def llm_request(self, model: str, tokens: dict) -> None:
        """记录 LLM 请求。"""
        self.logger.info(
            "llm_request",
            model=model,
            prompt_tokens=tokens.get("prompt_tokens", 0),
            completion_tokens=tokens.get("completion_tokens", 0),
        )

    def test_result(self, exit_code: int, stdout: str, stderr: str) -> None:
        """记录测试结果。"""
        self.logger.info(
            "test_result",
            exit_code=exit_code,
            stdout=stdout[:500] if stdout else "",
            stderr=stderr[:500] if stderr else "",
        )

    def success(self, attempts: int) -> None:
        """记录修复成功。"""
        self.logger.info(
            "agent_success",
            attempts=attempts,
        )

    def fail(self, attempts: int) -> None:
        """记录修复失败。"""
        self.logger.info(
            "agent_fail",
            attempts=attempts,
        )

    def watchdog_trigger(self, state: str, reason: str) -> None:
        """记录 Watchdog 触发。"""
        self.logger.warning(
            "watchdog_trigger",
            state=state,
            reason=reason,
        )

    def checkpoint_save(self, file_path: str) -> None:
        """记录快照保存。"""
        self.logger.debug(
            "checkpoint_save",
            file_path=file_path,
        )

    def error(self, message: str, **kwargs: Any) -> None:
        """记录错误。"""
        self.logger.error(
            "error",
            message=message,
            **kwargs,
        )
