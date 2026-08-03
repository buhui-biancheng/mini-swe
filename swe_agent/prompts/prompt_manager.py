"""PromptManager：提示词分级架构的确定性选择器（Phase 2 模块 D2）。

提示词分三层，FSM 作为"确定性提示词选择器"：
    第 1 层：base.md          永远在（身份 + 工作流 + 工具定义）
    第 2 层：locate/patch/success.md  进入对应状态时注入
    第 3 层：error_locating/rollback/degrade.md  遇到对应事件时注入

关键：重建，不是追加。system 不进 conversation 历史，
每轮调用 LLM 前从零拼，token 开销固定、不随轮数累积。
"""

import os
from typing import Optional


class PromptManager:
    """提示词加载 + 按状态/事件组装 system 消息。"""

    # 状态 → 提示词文件
    _STATE_PROMPTS = {
        "locate": "locate.md",
        "patch": "patch.md",
        "success": "success.md",
    }

    def __init__(self, prompts_dir: Optional[str] = None):
        self.prompts_dir = prompts_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__))
        )
        self._cache: dict[str, str] = {}

    def load(self, name: str) -> str:
        """加载提示词文件（带缓存）。"""
        if name not in self._cache:
            path = os.path.join(self.prompts_dir, name)
            with open(path, "r", encoding="utf-8") as f:
                self._cache[name] = f.read().rstrip()
        return self._cache[name]

    def build_system(
        self,
        *,
        state: str,
        mode: str,
        last_test_failed: bool = False,
        rollback_notice: bool = False,
    ) -> str:
        """按当前状态/事件组装 system 提示词。

        Args:
            state: 当前 FSM 状态
            mode: 当前模式（dp / greedy）
            last_test_failed: 上一轮测试是否失败（注入错误定位协议）
            rollback_notice: 是否刚发生回滚（注入回退认知上下文）
        """
        parts = [self.load("base.md")]

        # 第 2 层：状态提示词
        fname = self._STATE_PROMPTS.get(state)
        if fname:
            parts.append(self.load(fname))

        # 第 3 层：事件提示词
        if last_test_failed:
            parts.append(self.load("error_locating.md"))
        if mode == "greedy":
            parts.append(self.load("degrade.md"))
        if rollback_notice:
            parts.append(self.load("rollback.md"))

        return "\n\n".join(parts)
