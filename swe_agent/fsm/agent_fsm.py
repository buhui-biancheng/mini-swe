"""Agent FSM: 基于有限状态机的代码修复 Agent。

状态流转：
    INIT → LOCATE → PATCH → TEST → SUCCESS
                    ↓         ↓
                   FAIL      FAIL

功能：
1. 6 个状态：INIT, LOCATE, PATCH, TEST, SUCCESS, FAIL
2. 每个状态的 on_enter 回调触发 LLM 调用
3. Watchdog 防死循环（同状态同工具 ≥ 3 次触发回退）
4. Checkpoint 机制（PATCH 前备份代码快照）
"""

import os
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from transitions import Machine

from swe_agent.llm.client import LLMClient
from swe_agent.ast_view.skeleton import SkeletonTree
from swe_agent.tools.registry import ToolRegistry
from swe_agent.tools.schemas import TOOLS


# 状态定义
STATES = ["init", "locate", "patch", "test", "success", "fail"]

# 转换定义
TRANSITIONS = [
    {"trigger": "start", "source": "init", "dest": "locate"},
    {"trigger": "locate_done", "source": "locate", "dest": "patch"},
    {"trigger": "patch_done", "source": "patch", "dest": "test"},
    {"trigger": "test_pass", "source": "test", "dest": "success"},
    {"trigger": "test_fail", "source": "test", "dest": "locate"},
    {"trigger": "locate_fail", "source": "locate", "dest": "fail"},
    {"trigger": "patch_fail", "source": "patch", "dest": "fail"},
    {"trigger": "max_retries", "source": "test", "dest": "fail"},
]

# 系统提示词
SYSTEM_PROMPT = """\
你是一个专业的代码修复助手。你的任务是找到并修复 Python 代码中的 bug。

工作流程：
1. 使用 search_function 搜索相关函数
2. 使用 expand_function 查看函数的完整源码
3. 分析代码，找到 bug
4. 使用 edit_function 修复 bug（指定文件路径、起始行、结束行、新代码）
5. 使用 run_test 运行测试验证修复

重要规则：
- 每次只修复一个 bug
- edit_function 的 start_line 和 end_line 必须精确对应要替换的代码行
- 新代码必须是完整的、可运行的 Python 代码
- 修复后必须运行测试验证
- 文件路径使用骨架中显示的相对路径即可，系统会自动处理路径转换"""


@dataclass
class Watchdog:
    """Watchdog: 防死循环机制。

    记录每个状态的进入次数，同状态同工具 ≥ 3 次触发回退。
    """
    state_counts: dict[str, int] = field(default_factory=dict)
    tool_counts: dict[str, int] = field(default_factory=dict)
    max_same_state: int = 3
    max_same_tool: int = 3

    def record_state(self, state: str) -> bool:
        """记录状态进入，返回 True 表示触发防死循环。"""
        self.state_counts[state] = self.state_counts.get(state, 0) + 1
        if self.state_counts[state] >= self.max_same_state:
            return True
        return False

    def record_tool(self, tool_name: str) -> bool:
        """记录工具调用，返回 True 表示触发防死循环。"""
        self.tool_counts[tool_name] = self.tool_counts.get(tool_name, 0) + 1
        if self.tool_counts[tool_name] >= self.max_same_tool:
            return True
        return False

    def reset_state(self, state: str) -> None:
        """重置指定状态的计数。"""
        self.state_counts[state] = 0

    def reset_tool(self, tool_name: str) -> None:
        """重置指定工具的计数。"""
        self.tool_counts[tool_name] = 0

    def reset_all(self) -> None:
        """重置所有计数。"""
        self.state_counts.clear()
        self.tool_counts.clear()


@dataclass
class Checkpoint:
    """Checkpoint: 代码快照机制。

    在 PATCH 前备份代码，失败时恢复。
    """
    snapshots: dict[str, str] = field(default_factory=dict)

    def save(self, file_path: str) -> None:
        """保存文件快照。"""
        abs_path = os.path.abspath(file_path)
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                self.snapshots[abs_path] = f.read()
        except Exception:
            pass

    def restore(self, file_path: str) -> bool:
        """恢复文件快照。"""
        abs_path = os.path.abspath(file_path)
        if abs_path in self.snapshots:
            try:
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write(self.snapshots[abs_path])
                return True
            except Exception:
                return False
        return False

    def clear(self) -> None:
        """清除所有快照。"""
        self.snapshots.clear()


class AgentFSM:
    """Agent FSM: 基于有限状态机的代码修复 Agent。

    用法：
        fsm = AgentFSM(bug_file="path/to/bug.py", test_command="pytest")
        success = fsm.run()
    """

    def __init__(
        self,
        bug_file: str,
        test_command: str,
        max_retries: int = 2,
        python_version: str = "3.11",
        packages: list[str] | None = None,
    ):
        """初始化 Agent FSM。

        Args:
            bug_file: 有 bug 的 Python 文件路径
            test_command: 测试命令
            max_retries: 最大重试次数
            python_version: Docker 容器的 Python 版本
            packages: 需要预装的包列表
        """
        self.bug_file = os.path.abspath(bug_file)
        self.test_command = test_command
        self.max_retries = max_retries
        self.python_version = python_version
        self.packages = packages or ["pytest"]
        self.code_dir = os.path.dirname(self.bug_file)

        # 核心组件
        self.client = LLMClient()
        self.skeleton_tree = SkeletonTree(self.code_dir)
        self.skeleton_tree.scan()
        self.skeleton_text = self.skeleton_tree.generate_skeleton()

        self.registry = ToolRegistry(
            skeleton_text=self.skeleton_text,
            code_dir=self.code_dir,
            python_version=python_version,
            packages=self.packages,
        )

        # 防死循环和快照
        self.watchdog = Watchdog()
        self.checkpoint = Checkpoint()

        # 状态机
        self.machine = Machine(
            model=self,
            states=STATES,
            transitions=TRANSITIONS,
            initial="init",
        )

        # 绑定状态回调
        self.machine.on_enter_init(self._on_enter_init)
        self.machine.on_enter_locate(self._on_enter_locate)
        self.machine.on_enter_patch(self._on_enter_patch)
        self.machine.on_enter_test(self._on_enter_test)
        self.machine.on_enter_success(self._on_enter_success)
        self.machine.on_enter_fail(self._on_enter_fail)

        # 运行时状态
        self.messages: list[dict[str, Any]] = []
        self.attempt = 0
        self.tool_call_count = 0

    def _on_enter_init(self) -> None:
        """INIT 状态：初始化对话。"""
        print(f"\n{'='*50}")
        print(f"[INIT] 读取文件: {self.bug_file}")
        print(f"[INIT] 项目骨架:\n{self.skeleton_text}")
        print(f"[INIT] 测试命令: {self.test_command}")
        print(f"{'='*50}\n")

        # 将测试命令中的绝对路径转换为相对路径
        display_command = self.test_command
        if self.code_dir in display_command:
            display_command = display_command.replace(self.code_dir + "/", "")
            display_command = display_command.replace(self.code_dir, "")

        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"请修复以下文件中的 bug：\n\n"
                    f"文件：{self.bug_file}\n\n"
                    f"骨架：\n{self.skeleton_text}\n\n"
                    f"修复完成后运行测试：{display_command}"
                ),
            },
        ]

        self.watchdog.reset_all()
        self.checkpoint.clear()
        self.tool_call_count = 0

        # 自动流转到 LOCATE
        self.start()

    def _on_enter_locate(self) -> None:
        """LOCATE 状态：调用 LLM 定位 bug。"""
        # Watchdog 检查
        if self.watchdog.record_state("locate"):
            print("[WATCHDOG] locate 状态重复过多，触发失败")
            self.locate_fail()
            return

        print(f"\n--- 第 {self.attempt + 1} 次尝试 (LOCATE) ---")
        print("[LLM] 发送请求到 DeepSeek...")

        def tool_executor(tool_name: str, arguments: dict) -> str:
            print(f"  [TOOL] 调用 {tool_name}({json.dumps(arguments, ensure_ascii=False)})")
            result = self.registry.execute(tool_name, arguments)

            # Watchdog 检查工具调用
            if self.watchdog.record_tool(tool_name):
                print(f"  [WATCHDOG] 工具 {tool_name} 调用过多")
                return json.dumps({"error": f"工具 {tool_name} 调用次数超限"})

            result_data = json.loads(result)
            if "error" in result_data:
                print(f"  [TOOL] 错误: {result_data['error']}")
            else:
                print(f"  [TOOL] 成功")

            self.tool_call_count += 1
            return result

        # 调用 LLM
        final_response, conversation = self.client.chat_with_tools(
            messages=self.messages,
            tools=TOOLS,
            tool_executor=tool_executor,
            max_rounds=10,
        )

        self.messages = conversation

        if final_response:
            print(f"\n[LLM] 回复: {final_response[:200]}")

        # 流转到 PATCH
        self.locate_done()

    def _on_enter_patch(self) -> None:
        """PATCH 状态：保存快照，准备测试。"""
        # 保存代码快照
        self.checkpoint.save(self.bug_file)
        print(f"[PATCH] 已保存代码快照")

        # 流转到 TEST
        self.patch_done()

    def _on_enter_test(self) -> None:
        """TEST 状态：运行测试验证修复。"""
        # Watchdog 检查
        if self.watchdog.record_state("test"):
            print("[WATCHDOG] test 状态重复过多，触发失败")
            self.max_retries()
            return

        # 将测试命令转换为容器内路径
        import re
        container_command = self.test_command
        if self.code_dir in container_command:
            container_command = container_command.replace(self.code_dir, "/workspace")
        # 去掉目录前缀
        container_command = re.sub(r'(?:^|\s)(?:examples|tests|eval)/', ' ', container_command)
        container_command = ' '.join(container_command.split())

        print(f"\n[TEST] 运行测试: {container_command}")

        from swe_agent.sandbox.docker_runner import run_in_docker
        test_result = run_in_docker(self.code_dir, container_command)

        print(f"[TEST] exit_code: {test_result.exit_code}")
        if test_result.stdout:
            print(f"[TEST] stdout: {test_result.stdout[:500]}")
        if test_result.stderr:
            print(f"[TEST] stderr: {test_result.stderr[:500]}")

        if test_result.exit_code == 0:
            self.test_pass()
        else:
            # 恢复快照
            if self.checkpoint.restore(self.bug_file):
                print("[TEST] 已恢复代码快照")

            self.attempt += 1
            if self.attempt >= self.max_retries + 1:
                self.max_retries()
            else:
                self.test_fail()

    def _on_enter_success(self) -> None:
        """SUCCESS 状态：修复成功。"""
        print(f"\n{'='*50}")
        print(f"[SUCCESS] 修复成功！共尝试 {self.attempt + 1} 次")
        print(f"{'='*50}")

    def _on_enter_fail(self) -> None:
        """FAIL 状态：修复失败。"""
        print(f"\n{'='*50}")
        print(f"[FAIL] 修复失败，已用尽 {self.max_retries + 1} 次尝试")
        print(f"{'='*50}")

    def run(self) -> bool:
        """运行 Agent 修复 bug。

        Returns:
            True 表示修复成功，False 表示失败
        """
        self._on_enter_init()
        return self.state == "success"


def run_fsm_agent(
    bug_file: str,
    test_command: str,
    max_retries: int = 2,
    python_version: str = "3.11",
    packages: list[str] | None = None,
) -> bool:
    """运行 FSM Agent 修复 bug（便捷函数）。

    Args:
        bug_file: 有 bug 的 Python 文件路径
        test_command: 测试命令
        max_retries: 最大重试次数
        python_version: Docker 容器的 Python 版本
        packages: 需要预装的包列表

    Returns:
        True 表示修复成功，False 表示失败
    """
    fsm = AgentFSM(
        bug_file=bug_file,
        test_command=test_command,
        max_retries=max_retries,
        python_version=python_version,
        packages=packages,
    )
    return fsm.run()
