"""Agent FSM: 基于有限状态机的代码修复 Agent（Phase 2 FSM 增强）。

状态流转：
    INIT → LOCATE → PATCH → CHECK → TEST → SUCCESS
                    │       │      │
                    │       └─check_fail→ PATCH（重生成修复）
                    │              └── 连续失败 ≥ 3 → ROLLBACK
                    │
                    └─test_fail（影响面 < 阈值）→ LOCATE（新起点）
                      test 影响面 ≥ 阈值 → ROLLBACK

    ROLLBACK ◄────────── 代价熔断
      ├── rollback_count < 3 → LOCATE（换条路）
      ├── DP 反复失效 → 降级 Greedy → LOCATE
      └── Greedy 仍失败 → FAIL

Phase 2 新增：
    - 模块 A：每轮从 Traceback 新起点独立规划（FSM 重置机制）
    - 模块 B：动态权限围栏（软约束：警告 + 影响面代价惩罚）
    - 模块 C：ROLLBACK 代价熔断（影响面 ≥ 阈值 → 回滚初始快照）
    - 模块 D：日志解析器（TEST 出口 → grouped_errors + FAILURES 截断 + 落盘）
    - 模块 D2：提示词分级架构（PromptManager 按状态/事件注入）
    - 模块 D3：工具集简化（view_file 三模式，expand 合并，6 → 5）
    - 模块 E：取消回滚事件（CANCEL/TIMEOUT/ERROR 统一处理）
    - 模块 E2：Traceback 解析三条规则
    - 模块 E3：TokenBudget 预算管理（超限 → 降级/熔断）
    - 模块 F：双模降级（连续 ROLLBACK > 3 → Greedy，成功回写图权重）
"""

import os
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from transitions import Machine

from swe_agent.llm.client import LLMClient, AgentAPIError
from swe_agent.graph import (
    GraphManager,
    AgentConfig,
    SyntaxFirewall,
    PermissionFence,
    parse_test_log,
    save_full_log,
    compute_edited_impact,
)
from swe_agent.snapshot import SnapshotManager
from swe_agent.tools.registry import ToolRegistry
from swe_agent.tools.schemas import TOOLS
from swe_agent.utils.logger import AgentLogger
from swe_agent.watchdog import DecisionEngine, WatchdogConfig, Action
from swe_agent.prompts import PromptManager
from swe_agent.fsm.cancel_handler import handle_cancel, CancelReason
from swe_agent.fsm.token_budget import TokenBudget, TokenBudgetExceeded


# 状态定义
STATES = ["init", "locate", "patch", "check", "test", "rollback", "success", "fail"]

# 转换定义
TRANSITIONS = [
    {"trigger": "start", "source": "init", "dest": "locate"},
    {"trigger": "locate_done", "source": "locate", "dest": "patch"},
    {"trigger": "patch_done", "source": "patch", "dest": "check"},
    {"trigger": "check_pass", "source": "check", "dest": "test"},
    {"trigger": "check_fail", "source": "check", "dest": "patch"},
    {"trigger": "check_exhausted", "source": "check", "dest": "rollback"},  # 连续语法失败
    {"trigger": "patch_syntax_error", "source": "patch", "dest": "locate"},  # 兼容旧触发器
    {"trigger": "test_pass", "source": "test", "dest": "success"},
    {"trigger": "test_fail", "source": "test", "dest": "locate"},
    {"trigger": "rollback", "source": "test", "dest": "rollback"},      # 代价熔断
    {"trigger": "rollback_retry", "source": "rollback", "dest": "locate"},
    {"trigger": "degrade", "source": "rollback", "dest": "locate"},     # DP → Greedy
    {"trigger": "rollback_fail", "source": "rollback", "dest": "fail"},
    {"trigger": "locate_fail", "source": "locate", "dest": "fail"},
    {"trigger": "patch_fail", "source": "patch", "dest": "fail"},
    {"trigger": "retries_exhausted", "source": "test", "dest": "fail"},
]


@dataclass
class Watchdog:
    """Watchdog: 基于 DecisionEngine 的智能防死循环机制。

    使用纯决策引擎进行检测：
    1. 重复调用检测（最近 3 次相同工具+参数）
    2. 状态进入检测（最近 5 次进入同一状态）
    3. 无进展检测（连续多轮没有编辑）
    4. 低效编辑检测（编辑成功率过低）
    """
    engine: DecisionEngine = field(default_factory=lambda: DecisionEngine(WatchdogConfig()))

    def record_tool(self, tool_name: str, arguments: dict) -> bool:
        """记录工具调用，返回 True 表示触发防死循环。"""
        return self.engine.record_tool_call(tool_name, arguments)

    def record_state(self, state: str) -> bool:
        """记录状态进入，返回 True 表示触发防死循环。"""
        return self.engine.record_state_entry(state)

    def record_edit(self, file_path: str, success: bool) -> None:
        """记录编辑操作。"""
        self.engine.record_edit(file_path, success)

    def check_stuck(self) -> tuple[bool, str]:
        """检查是否卡住了。"""
        return self.engine.check_stuck()

    def reset_all(self) -> None:
        """重置所有计数。"""
        self.engine.reset()


@dataclass
class Checkpoint:
    """Checkpoint: 代码快照机制（Phase 2 作为 mock 快照，Phase 4 换 SnapshotManager）。

    首次编辑前保存初始态（save_initial），ROLLBACK 恢复初始态。
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

    def save_initial(self, file_path: str) -> None:
        """首次编辑前记录初始态（仅当该文件尚未快照时保存）。

        保证 ROLLBACK 恢复的是"编辑前"的初始状态，而非编辑后的状态。
        """
        abs_path = os.path.abspath(file_path)
        if abs_path not in self.snapshots:
            self.save(file_path)

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
        code_dir: Optional[str] = None,
        max_retries: int = 2,
        python_version: str = "3.11",
        packages: list[str] | None = None,
        mode: str = "auto",
        no_degrade: bool = False,
        sandbox: bool = False,
        config=None,
        graph_level: int = 2,
        early_stop: bool = False,
        early_stop_patience: int = 2,
    ):
        """初始化 Agent FSM。

        Args:
            bug_file: 有 bug 的 Python 文件路径
            test_command: 测试命令
            max_retries: 每条规划路径的最大重试次数（rollback 后重置）
            python_version: Docker 容器的 Python 版本
            packages: 需要预装的包列表
            mode: 运行模式（dp/greedy/auto）。dp=图索引引导，greedy=无图探索
        """
        self.bug_file = os.path.abspath(bug_file)
        self.test_command = test_command
        self.max_retries = max_retries
        self.python_version = python_version
        self.packages = packages or ["pytest"]
        # code_dir：显式传入（SWE-bench 多文件 repo = 项目根）或默认 bug 文件所在目录
        # 2026-08-08：修"bug 在子目录 + 测试在根目录"场景（容器必须挂载整个项目）
        self.code_dir = os.path.abspath(code_dir) if code_dir else os.path.dirname(self.bug_file)
        # Phase 6 两层沙盒：真实代码只读，Agent 在 COW 副本工作
        self.sandbox = sandbox
        self._l1 = None
        # 评测消融（2026-08-08 用户定稿）：graph_level 显微镜粗准/细准
        #   0 = 纯贪心（无任何图信息）；1 = 只有细准（L1 函数级，无文件级先验）
        #   2 = 完整（粗准 L-1/L0/骨架 + 细准 L1/影响面标注）
        self.graph_level = graph_level
        # 收益早停（2026-08-08）：每次尝试后记录失败用例数，连续无进展则提前停
        self.early_stop = early_stop
        self.early_stop_patience = max(1, early_stop_patience)
        self.attempt_trajectory = []  # [{attempt, fail_count, token, cost}] 尝试轨迹
        if sandbox:
            from swe_agent.sandbox.l1_sandbox import L1Sandbox
            real_dir = self.code_dir
            self._real_dir = real_dir
            self._l1 = L1Sandbox(real_dir)
            task_id = f"{os.path.basename(real_dir)}_{os.getpid()}"
            ws = self._l1.create(task_id=task_id)
            self.code_dir = ws  # 一切操作指向副本
            # bug_file 映射到副本（图/快照/编辑全部基于副本）
            self.bug_file = self._l1.map_to_workspace(self.bug_file)
        self.mode = mode if mode in ("dp", "greedy", "auto") else "auto"
        self.effective_mode = "greedy" if mode == "greedy" else "dp"
        self.no_degrade = no_degrade  # 评测消融：禁止 DP→Greedy 降级（2026-08-05）

        # 核心组件（图索引）
        self.agent_config = config or AgentConfig()  # 可注入（评测/Web 调整思考强度）
        self.client = LLMClient()
        self.logger = AgentLogger()
        self.logger.init(bug_file=self.bug_file, test_command=test_command, mode=self.mode)

        self.graph_manager = GraphManager(self.code_dir, config=self.agent_config)
        self.graph_index = self.graph_manager.build()
        # 评测消融：骨架 = 粗准（文件级），graph_level >= 2 才有
        self.skeleton_text = (self.graph_index.generate_skeleton_text()
                              if self.graph_level >= 2 else "")

        # Phase 2：权限围栏 + 提示词分级 + TokenBudget
        self.fence = PermissionFence(self.graph_index, self.agent_config)
        self.prompt_manager = PromptManager()
        self.token_budget = TokenBudget(self.agent_config.token_budget)

        self.registry = ToolRegistry(
            skeleton_text=self.skeleton_text,
            code_dir=self.code_dir,
            python_version=python_version,
            packages=self.packages,
            graph_index=self.graph_index,
            fence=self.fence,
            graph_manager=self.graph_manager,
            sandbox=self.sandbox,
        )

        # 防死循环、快照、语法防火墙
        self.watchdog = Watchdog()
        self.checkpoint = Checkpoint()
        self.snapshot_mgr = SnapshotManager(
            self.code_dir, task_id=os.path.basename(self.code_dir))
        self._initial_weights: dict = {}
        self._last_silent_errors: list = []
        self._cognition_history: list = []  # Phase 4 机制二：修改历史（认知保持）
        # 语法检查版本 = 目标容器 Python 版本（评测 py3.8 时拦截 3.9+ 语法）
        _fv = None
        if self.python_version:
            try:
                _parts = [int(x) for x in str(self.python_version).split(".")[:2]]
                if len(_parts) == 2:
                    _fv = tuple(_parts)
            except ValueError:
                _fv = None
        self.syntax_firewall = SyntaxFirewall(feature_version=_fv)
        self._syntax_errors: list = []

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
        self.machine.on_enter_check(self._on_enter_check)
        self.machine.on_enter_test(self._on_enter_test)
        self.machine.on_enter_rollback(self._on_enter_rollback)
        self.machine.on_enter_success(self._on_enter_success)
        self.machine.on_enter_fail(self._on_enter_fail)

        # 运行时状态
        self.messages: list[dict[str, Any]] = []
        self.attempt = 0
        self.step = 0
        self.rollback_count = 0
        self._check_fail_count = 0
        self._last_test_failed = False
        self._rollback_notice = False
        self._cancel_reason = ""
        self._initial_task_msg = ""
        self._plain_task_msg = ""
        self.tool_call_count = 0
        self._prev_state: Optional[str] = None  # 记录上一状态，用于 transition 日志
        self._edited_ranges: list[tuple] = []   # 记录本次编辑的 (file, start, end)
        self._signature_notes: list = []        # CHECK 检测到的签名变更调用方提醒

    # ========== 日志与上下文 ==========

    def _log_state_enter(self, state: str, attempt: int) -> None:
        """记录状态进入 + 状态转换（transition 事件）。

        统一 print [STATE] 标记，供 TUI 子进程解析状态流转。
        """
        print(f"  [STATE] {state} (第 {attempt + 1} 次尝试)")
        self.logger.state_enter(state, attempt)
        if self._prev_state is not None and self._prev_state != state:
            self.logger.transition(
                trigger="", source=self._prev_state, dest=state,
            )
        self._prev_state = state

    def _build_messages(self) -> list[dict[str, Any]]:
        """重建 system 提示词（不 append 进 conversation，每轮从零拼）。

        system 只含当前相关的提示词块，token 开销固定、不随轮数累积。

        稳定头部约束（缺陷8，2026-08-05）：
            DeepSeek cache-hit 比 miss 便宜 120x，而缓存按前缀命中。
            头部 = system（base.md 永远第一）+ 初始任务消息（_initial_task_msg，
            含 L-1/L0/L1 图上下文，只在 INIT 注入一次）——这两块在任务期间
            **必须保持不变**，每轮只变中部（轮次对话），才能每轮 cache-hit。
            禁止：把图上下文改成每轮注入 / 调整 base.md 顺序 / 头部拼动态内容。
            例外：降级时 _plain_task_msg 重置是有意的换策略（次数 ≤ rollback_limit，
            符合"少而深不渐进"）。
        """
        system = self.prompt_manager.build_system(
            state=self.state,
            mode=self.effective_mode,
            last_test_failed=self._last_test_failed,
            rollback_notice=self._rollback_notice,
        )
        self._rollback_notice = False  # 一次性注入
        return [{"role": "system", "content": system}] + self.messages

    def _graph_context_text(self) -> str:
        if self.graph_level == 0:
            return ""
        """DP 模式：生成图索引上下文文本（L-1 文件级先验 + L0 摘要 + 报错节点邻接）。"""
        parts = []

        # L-1 文件级全局先验（在 L0 之前，最优先注入；数据全部现成，聚合视图）
        # 粗准（大地图）：graph_level >= 2 才有
        if self.graph_level >= 2:
            parts.append(self._file_level_prior_text())

        # L0 摘要（粗准：图级概览）
        summary = self.graph_index.get_summary()
        lines = [
            f"节点数: {summary['node_count']}, 边数: {summary['edge_count']}, "
            f"文件数: {summary['file_count']}",
            "高入度节点（核心枢纽）:",
        ]
        for item in summary["top_in_degree"][:10]:
            lines.append(
                f"  - {item['node']} (in_degree={item['in_degree']})"
            )
        if self.graph_level >= 2:
            parts.append("【图索引 L0 摘要】\n" + "\n".join(lines))

        # 报错文件相关节点 L1 邻接 → 极简格式（位置化读取，省 token）
        # 细准（小地图）：graph_level >= 1 就有（单独细准也能用）
        if self.graph_level >= 1:
            self._append_l1_neighborhood(parts)
        return "\n\n".join(parts)

    def _append_l1_neighborhood(self, parts) -> None:
        """L1 邻域极简格式（细准：函数级邻居/边/影响面）。"""
        bug_base = os.path.basename(self.bug_file)
        seed_ids = [
            n.node_id for n in self.graph_index.graph.nodes.values()
            if n.node_type.value == "function" and os.path.basename(n.file) == bug_base
        ]
        if seed_ids:
            # 邻域：seed 函数 + 1 跳邻居 + 邻域内边
            seen = set(seed_ids)
            for sid in seed_ids:
                neighbors = self.graph_index.get_neighbors(sid, hops=1)
                for depth, items in neighbors.get("neighbors", {}).items():
                    for it in items:
                        if it.get("node"):
                            seen.add(it["node"])
            g = self.graph_index.graph
            nbr_nodes = sorted(
                (g.nodes[nid] for nid in seen if nid in g.nodes),
                key=lambda x: (x.file, x.lineno),
            )
            nbr_edges = [e for e in g.edges if e.source in seen and e.target in seen]
            body = [
                "# 图索引极简格式：行首 NODE:/EDGE: 区分节点边，字段按 | 分隔固定列序",
                "# NODE: id | file | function | lineno | in_degree | dynamic_weight | is_reflection",
                "# EDGE: from | to | edge_type",
            ]
            for n in nbr_nodes:
                body.append(
                    f"NODE: {n.node_id} | {n.file} | {n.name} | {n.lineno} | "
                    f"{n.in_degree} | {n.dynamic_weight} | {str(n.is_reflection).lower()}"
                )
            for e in nbr_edges:
                body.append(f"EDGE: {e.source} | {e.target} | {e.edge_type.value}")
            for sid in seed_ids:
                body.append(f"# 影响面 {sid} = {self.graph_index.compute_impact(sid)}")
            parts.append("【图索引邻域（极简格式）】\n" + "\n".join(body))

        return "\n\n".join(parts)

    def _file_level_prior_text(self) -> str:
        if self.graph_level < 2:
            return ""
        """L-1 文件级先验：委托 GraphIndex（2026-08-05 重构，diagnose 共用）。"""
        return self.graph_index.file_level_prior_text()

    def _fence_warnings(self) -> list[str]:
        """本次编辑涉及的围栏警告（软约束）。"""
        if self.fence is None:
            return []
        warnings = []
        for fp, _, _ in self._edited_ranges:
            warnings.extend(self.fence.check_edit(fp).warnings)
        return list(dict.fromkeys(warnings))

    def _resolve_file(self, file_path: str) -> str:
        """把工具传入的文件路径解析为绝对路径（兼容相对/绝对）。

        LLM 可能传相对路径（如 "bug.py"），而 cwd 不一定等于 code_dir，
        直接 abspath 会解析到错误位置。统一先按 code_dir 兜底。
        """
        abs_path = os.path.abspath(file_path)
        if os.path.exists(abs_path):
            return abs_path
        joined = os.path.join(self.code_dir, file_path)
        if os.path.exists(joined):
            return os.path.abspath(joined)
        return abs_path

    def _display_command(self) -> str:
        """测试命令的展示形态（去掉宿主机绝对路径前缀）。"""
        display_command = self.test_command
        if self.code_dir in display_command:
            display_command = display_command.replace(self.code_dir + "/", "")
            display_command = display_command.replace(self.code_dir, "")
        return display_command

    # ========== 工具回合 ==========

    def _run_llm_turn(self) -> str:
        """调用 LLM 进行工具调用回合（LOCATE 与 PATCH 重修复共用）。"""
        def tool_executor(tool_name: str, arguments: dict) -> str:
            # 编辑前先记录初始快照（保证 ROLLBACK 能恢复编辑前状态）
            if tool_name == "edit_function":
                fp = self._resolve_file(arguments.get("file_path", ""))
                self.checkpoint.save_initial(fp)

            print(f"  [TOOL] 调用 {tool_name}({json.dumps(arguments, ensure_ascii=False)})")
            result = self.registry.execute(tool_name, arguments)
            result_data = json.loads(result)

            # Watchdog 检查工具调用（传入参数做重复检测）
            if self.watchdog.record_tool(tool_name, arguments):
                print(f"  [WATCHDOG] 检测到重复调用: {tool_name}")
                self.logger.watchdog_trigger("locate", f"重复调用: {tool_name}")
                return json.dumps({"error": f"检测到重复调用: {tool_name}"})

            # 追踪 edit 操作（记录实际编辑范围，成功时给对应函数加权）
            if tool_name == "edit_function":
                is_success = "error" not in result_data
                file_path = arguments.get("file_path", "")
                self.watchdog.record_edit(file_path, is_success)
                if is_success:
                    self._edited_ranges.append((
                        self._resolve_file(file_path),
                        int(arguments.get("start_line", 1)),
                        int(arguments.get("end_line", 1)),
                    ))
                    # Phase 4 机制二：认知保持（记录修改历史，回退时注入）
                    self._cognition_history.append(
                        f"{os.path.basename(file_path)}:{arguments.get('start_line', 1)}"
                        f"-{arguments.get('end_line', 1)} 编辑")

            # 追踪无进展
            stuck, reason = self.watchdog.check_stuck()
            if stuck:
                print(f"  [WATCHDOG] 检测到卡住: {reason}")
                self.logger.watchdog_trigger("locate", reason)
                return json.dumps({"error": f"检测到卡住: {reason}"})

            self.logger.tool_call(tool_name, arguments, success=("error" not in result_data))

            if "error" in result_data:
                print(f"  [TOOL] 错误: {result_data['error']}")
            else:
                print(f"  [TOOL] 成功")

            self.tool_call_count += 1
            return result

        messages = self._build_messages()
        try:
            final_response, conversation = self.client.chat_with_tools(
                messages=messages,
                tools=TOOLS,
                tool_executor=tool_executor,
                max_rounds=10,
                usage_callback=self._on_usage,
                thinking=self.agent_config.thinking_enabled,
                reasoning_effort=self.agent_config.reasoning_effort,
            )
        except AgentAPIError as e:
            print(f"[ERROR] LLM API 持续失败: {e}")
            self.cancel(reason=CancelReason.API_ERROR)
            return ""
        except TokenBudgetExceeded:
            # 调用中途超限：DP → 降级 Greedy（清空上下文省 token）；Greedy → 熔断
            if self.effective_mode == "dp":
                print("[BUDGET] 降级到 Greedy 模式（清空上下文省 token）")
                self._switch_mode("greedy", reason="token_budget_exceeded")
                self.messages = [{"role": "user", "content": self._plain_task_msg}]
                self._rollback_notice = True
            else:
                print("[BUDGET] Greedy 模式 token 预算也超限，熔断")
                self.cancel(reason=CancelReason.BUDGET_EXCEEDED)
            return ""

        # system 不进 conversation 历史（模块 D2：重建而非追加）
        self.messages = [m for m in conversation if m.get("role") != "system"]
        return final_response

    def _on_usage(self, usage: dict) -> None:
        """每次 LLM 调用后汇总 token 用量（模块 E3）。

        超限时抛 TokenBudgetExceeded，中断当前调用，由 _run_llm_turn 接住
        降级（DP→Greedy 清空上下文）或熔断（Greedy→cancel）。
        """
        if usage:
            self.token_budget.add(usage)
            if self.token_budget.exceeded():
                print(f"[BUDGET] token 预算超限（累计 {self.token_budget.total}"
                      f"/{self.token_budget.limit}），中断当前调用")
                raise TokenBudgetExceeded()

    # ========== 状态回调 ==========

    def _on_enter_init(self) -> None:
        """INIT 状态：初始化对话。"""
        print(f"\n{'='*50}")
        print(f"[INIT] 读取文件: {self.bug_file}")
        print(f"[INIT] 模式: {self.mode}")
        print(f"[INIT] 项目骨架:\n{self.skeleton_text}")
        print(f"[INIT] 测试命令: {self.test_command}")
        print(f"{'='*50}\n")

        # 纯任务消息（Greedy 降级时使用，不带图上下文）
        self._plain_task_msg = (
            f"请修复以下文件中的 bug：\n\n"
            f"文件：{self.bug_file}\n\n"
            f"骨架：\n{self.skeleton_text}\n\n"
            f"修复完成后运行测试：{self._display_command()}"
        )

        # DP 模式：注入图索引上下文 + 围栏软约束
        user_content = self._plain_task_msg
        if self.effective_mode in ("dp", "auto"):
            user_content += f"\n\n{self._graph_context_text()}"
            fence_text = self.fence.fence_text()
            if fence_text:
                user_content += f"\n\n{fence_text}"

        self._initial_task_msg = user_content
        self.messages = [{"role": "user", "content": user_content}]
        self._last_test_failed = False
        self._rollback_notice = False
        self._cancel_reason = ""
        self.attempt = 0
        self.step = 0
        self.rollback_count = 0
        self._check_fail_count = 0
        self._edited_ranges = []

        self.watchdog.reset_all()
        self.checkpoint.clear()
        # Phase 4 机制四：任务开始时记录初始权重（失败/取消时回退到它）
        self._initial_weights = self.graph_manager.get_weights_snapshot()
        self.tool_call_count = 0
        self._syntax_errors = []
        self._log_state_enter("init", self.attempt)

        # 自动流转到 LOCATE
        self.start()

    def _on_enter_locate(self) -> None:
        """LOCATE 状态：调用 LLM 定位 bug（每轮从新起点独立规划）。"""
        # Watchdog 检查
        if self.watchdog.record_state("locate"):
            print("[WATCHDOG] locate 状态重复过多，触发失败")
            self.logger.watchdog_trigger("locate", "状态重复过多")
            self.locate_fail()
            return

        # TokenBudget 超限检查：DP → 降级 Greedy；Greedy → 熔断
        if self.token_budget.exceeded():
            if self.effective_mode == "dp":
                print("[BUDGET] token 预算超限，降级到 Greedy 模式")
                self._switch_mode("greedy", reason="token_budget_exceeded")
                self.messages = [{"role": "user", "content": self._plain_task_msg}]
                self._rollback_notice = True
            else:
                print("[BUDGET] Greedy 模式 token 预算也超限，熔断")
                self.cancel(reason=CancelReason.BUDGET_EXCEEDED)
                return

        print(f"\n--- 第 {self.attempt + 1} 次尝试 (LOCATE) ---")
        print("[LLM] 发送请求到 DeepSeek...")
        self._log_state_enter("locate", self.attempt)

        final_response = self._run_llm_turn()

        if self.state in ("fail", "success"):
            return  # 取消/熔断已强制终止，不再触发转换

        if final_response:
            print(f"\n[LLM] 回复: {final_response[:200]}")

        # 流转到 PATCH
        self.locate_done()

    def _on_enter_patch(self) -> None:
        """PATCH 状态：语法检查失败时反馈并重生成修复，否则保存快照进 CHECK。"""
        self._log_state_enter("patch", self.attempt)

        # CHECK 驳回后重进 PATCH：反馈语法错误，重新生成修复（不重新定位）
        if self._syntax_errors:
            for err in self._syntax_errors:
                msg = (f"⚠️ 上一轮修改引入了语法错误，请修正后重新提交：\n"
                       f"文件 {os.path.basename(self.bug_file)} 第 {err.line} 行: {err.msg}")
                print(f"  [SYNTAX] 注入反馈给 LLM: {msg}")
                self.messages.append({"role": "user", "content": msg})
            self._syntax_errors = []

            final_response = self._run_llm_turn()
            if self.state in ("fail", "success"):
                return  # 取消/熔断已强制终止
            if final_response:
                print(f"\n[LLM] 回复: {final_response[:200]}")
            self.patch_done()  # → CHECK
            return

        # 兜底保存初始快照（LLM 未走工具直接改文件时）
        self.checkpoint.save_initial(self.bug_file)
        print(f"[PATCH] 已保存代码快照")
        self.logger.snapshot_saved(self.bug_file)

        # 流转到 CHECK
        self.patch_done()

    def _on_enter_check(self) -> None:
        """CHECK 状态：语法防火墙 + 围栏软约束 + 影响面（并行，任一硬失败 → 回 PATCH）。"""
        self._log_state_enter("check", self.attempt)

        # 1. 语法防火墙：ast.parse 毫秒级拦截，不进 Docker
        files_to_check = [self.bug_file]
        for fp, _, _ in self._edited_ranges:
            resolved = self._resolve_file(fp)
            if resolved != self.bug_file:
                files_to_check.append(resolved)
        syntax_errors = []
        for fp in files_to_check:
            r = self.syntax_firewall.check_file(fp)
            if not r.ok:
                syntax_errors.extend(r.errors)

        if syntax_errors:
            print(f"[CHECK] 语法错误被拦截: {len(syntax_errors)} 处")
            self.logger.rollback_triggered("syntax_error", self.bug_file)
            self._syntax_errors = syntax_errors
            self._check_fail_count += 1
            if self._check_fail_count >= self.agent_config.check_fail_limit:
                print(f"[CHECK] 连续语法失败 {self._check_fail_count} 次，触发 ROLLBACK")
                self.check_exhausted()  # → rollback
            else:
                self.check_fail()       # → patch（重新生成修复）
            return

        self._check_fail_count = 0

        # 2. 围栏软约束（警告，不拦截）
        fence_warnings = self._fence_warnings()
        if fence_warnings:
            print(f"[CHECK] 围栏警告: {'; '.join(fence_warnings)}")

        # 3. 影响面分析 + 签名变更调用方适配检查（P2）
        self._signature_notes = []
        if self._edited_ranges:
            impact = compute_edited_impact(
                self.graph_index, self._edited_ranges, self.agent_config, self.fence
            )
            if impact["nodes"]:
                print(f"[CHECK] 本次编辑影响面: {impact['total']} "
                      f"(涉及 {impact['nodes']})")
                self._check_signature_changes(impact["nodes"])

        self.check_pass()  # → test

    def _check_signature_changes(self, node_ids: list) -> None:
        """签名变更调用方适配检查（Phase 2 P2 任务）。

        编辑范围触及函数定义行（def 行）→ 视为签名变更 → 列出调用方提醒。
        软约束：不拦截，记录到 _signature_notes，测试失败时随上下文注入。
        """
        for node_id in node_ids:
            node = self.graph_index.get_node(node_id)
            if node is None:
                continue
            # 编辑范围是否触及该节点的定义行
            touched_def = any(
                start <= node.lineno <= end
                for _, start, end in self._edited_ranges
            )
            if not touched_def:
                continue
            callers = self.graph_index.get_callers(node_id)
            if not callers:
                continue
            caller_names = [c.node_id for c in callers]
            note = (
                f"你修改了 {node_id} 的函数签名（第 {node.lineno} 行），"
                f"以下调用方可能需要适配: {'; '.join(caller_names)}"
            )
            self._signature_notes.append(note)
            print(f"  [CHECK] 签名变更提醒: {note[:150]}...")

    def _on_enter_test(self) -> None:
        """TEST 状态：运行测试验证修复。

        TEST 出口（模块 D 日志解析器，非独立状态）：
            绿色 → SUCCESS + 动态权重 +1
            红色 → grouped_errors + 完整日志落盘 → 影响面熔断判定
        """
        # Watchdog 检查
        if self.watchdog.record_state("test"):
            print("[WATCHDOG] test 状态重复过多，触发失败")
            self.logger.watchdog_trigger("test", "状态重复过多")
            self.retries_exhausted()
            return

        # 将测试命令转换为容器内路径
        container_command = self.test_command
        if self.code_dir in container_command:
            container_command = container_command.replace(self.code_dir, "/workspace")
        # 保留测试命令原样（2026-08-08：不再剥 tests/ 前缀——SWE-bench 测试路径必须完整）
        container_command = ' '.join(container_command.split())

        print(f"\n[TEST] 运行测试: {container_command}")
        self._log_state_enter("test", self.attempt)

        from swe_agent.sandbox.docker_runner import run_in_docker
        # 2026-08-08：传 python_version/packages（评测环境注入），network=False 保持沙盒断网
        test_result = run_in_docker(
            self.code_dir, container_command,
            python_version=self.python_version, packages=self.packages)

        print(f"[TEST] exit_code: {test_result.exit_code}")
        if test_result.stdout:
            print(f"[TEST] stdout: {test_result.stdout[:500]}")
        if test_result.stderr:
            print(f"[TEST] stderr: {test_result.stderr[:500]}")
        self.logger.test_result(test_result.exit_code, test_result.stdout, test_result.stderr)

        raw_log = (test_result.stdout or "") + "\n" + (test_result.stderr or "")

        if test_result.exit_code == 0:
            self._last_test_failed = False
            # Phase 4 机制三增强：异常信号检查（静默报错检测）
            # exit 0 但日志含异常模式 → 记录可疑信号（不强制回退，避免误伤）
            silent = sorted(set(re.findall(
                r"(Traceback|NameError|TypeError|ValueError|AssertionError|KeyError|IndexError|Exception|Error)",
                raw_log)))
            if silent:
                print(f"[TEST] ⚠️ 静默报错信号（exit 0 但日志含）: {silent}")
                self._last_silent_errors = silent
            # 修复成功：给实际编辑的函数动态权重 +1（图索引）
            targets = self._success_weight_targets()
            for node_id in targets:
                self.graph_manager.update_dynamic_weight(node_id)
                self.logger.jit_update(node=node_id, accepted=True, reason="success")
            self.test_pass()
        else:
            self._last_test_failed = True
            self.step += 1

            # 日志解析器：红色 → 完整日志落盘 + 结构化错误路径 + FAILURES 段截断
            full_log_path = save_full_log(raw_log, self.graph_manager.graph_dir)
            parsed = parse_test_log(
                raw_log,
                test_result.exit_code,
                code_dir=self.code_dir,
                failures_segment_limit=self.agent_config.failures_segment_limit,
            )
            parsed.full_log_path = full_log_path
            self._append_failure_context(parsed)

            # 影响面代价熔断（模块 C）：≥ 阈值 → ROLLBACK
            impact = compute_edited_impact(
                self.graph_index, self._edited_ranges, self.agent_config, self.fence
            )
            if self.step > self.agent_config.max_steps:
                print(f"[TEST] 步数超限（{self.step}/{self.agent_config.max_steps}），失败")
                self.retries_exhausted()
                return
            if impact["total"] >= self.agent_config.impact_threshold:
                print(f"[TEST] 影响面 {impact['total']} ≥ 阈值 "
                      f"{self.agent_config.impact_threshold}，触发 ROLLBACK")
                self.logger.rollback_triggered("impact", self.bug_file)
                self.rollback()  # test → rollback
                return

            # 收益早停（2026-08-08）：失败用例数连续无进展 → 提前停
            self.attempt_trajectory.append({
                "attempt": self.attempt,
                "fail_count": len(parsed.grouped_errors),
                "token": self.token_budget.total,
                "cost": self.token_budget.estimate_cost(),
            })
            if self.early_stop and len(self.attempt_trajectory) > self.early_stop_patience:
                recent = self.attempt_trajectory[-self.early_stop_patience:]
                best = min(x["fail_count"] for x in self.attempt_trajectory[:-self.early_stop_patience])
                if all(x["fail_count"] >= best for x in recent):
                    print(f"[EARLY-STOP] 连续 {self.early_stop_patience} 次无进展"
                          f"（失败数 {[x['fail_count'] for x in recent]} ≥ 历史最佳 {best}），提前停止")
                    self.retries_exhausted()
                    return

            # 影响面 < 阈值 → LOCATE（新起点 = 结构化错误路径，继续修不走原路）
            self.attempt += 1
            if self.attempt > self.max_retries:
                print(f"[TEST] 超过最大重试次数（{self.max_retries}），失败")
                self.retries_exhausted()
            else:
                self.test_fail()  # test → locate

    def _append_failure_context(self, parsed) -> None:
        """把结构化错误路径 + 图影响面排序 + 新起点图上下文注入对话。

        模块 D 升级：LOCATE 收到的不是"去日志找错误"，而是已按图影响面
        排序的错误列表（影响面最大的那条优先定位）。
        模块 A 升级：对影响面最大的错误节点注入 L1 邻接（图引导跨文件追踪）。
        """
        _head = ("【测试失败】测试未通过，以下是结构化错误信息（已按图影响面排序）："
                 if self.graph_level >= 1
                 else "【测试失败】测试未通过，以下是结构化错误信息：")
        lines = [_head]
        enriched = []
        for e in parsed.grouped_errors:
            top_node, impact = self._error_impact_representative(e)
            enriched.append((e, top_node, impact))
        # 影响面降序（匹配不到图节点的放最后）；图先验消融：无图时不排序（保持原始错误顺序）
        if self.graph_level >= 1:
            enriched.sort(key=lambda x: x[2] if x[2] is not None else -1.0, reverse=True)

        if enriched:
            for i, (e, node, impact) in enumerate(enriched):
                loc = f"{e.file}:{e.lineno}" if e.file else "(无法解析位置)"
                if self.graph_level >= 1 and impact is not None:
                    impact_str = f"影响面={impact:.4f}"
                    mark = " ← 影响面最大，优先定位" if i == 0 else ""
                    lines.append(f"- {e.error_type} @ {loc} [{impact_str}]{mark}")
                else:
                    lines.append(f"- {e.error_type} @ {loc}")
                if e.callsite:
                    lines.append(f"  调用链: {' → '.join(e.callsite)}")
                lines.append(
                    f"  日志位置: 第 {e.log_start_line}-{e.log_end_line} 行 "
                    f"(.graph/last_test.log，用 view_file 查看)"
                )
        else:
            lines.append("- 未能解析出结构化错误，请用 view_file 查看 .graph/last_test.log")

        # 签名变更提醒（A3：CHECK 阶段记录，测试失败时带出）
        if self._signature_notes:
            lines.append("\n【签名变更提醒】")
            lines.extend(f"  - {n}" for n in self._signature_notes)

        lines.append("请优先定位影响面最大的那条错误路径。")
        msg = "\n".join(lines)
        print(f"  [LOG] 注入失败上下文: {msg[:200]}...")
        self.messages.append({"role": "user", "content": msg})

        # 模块 A：对影响面最大的错误节点注入 L1 邻接（跨文件追踪引导）
        if enriched and enriched[0][1] is not None:
            top_node = enriched[0][1]
            ctx = self._node_context_text(top_node.node_id)
            if ctx:
                print(f"  [LOG] 注入新起点图上下文: {top_node.node_id}")
                self.messages.append({"role": "user", "content": ctx})

    def _error_impact_representative(self, e) -> tuple:
        """解析一条错误的影响面代表节点：错误位置 + 调用链上影响面最大的节点。

        错误主位置通常在测试文件（断言失败处），影响面≈0；
        调用链上的生产节点（如 cart.total → pricing.compute_price）才是
        影响面代表——这正是"错误路径影响面"的语义。
        """
        candidates = []
        if e.file:
            node = self.graph_index.resolve_location(e.file, e.lineno)
            if node is not None:
                candidates.append(node)
        for cs in e.callsite:
            parts = cs.split(":")
            if len(parts) == 2:
                node = self.graph_index.resolve_location(parts[0], int(parts[1]))
                if node is not None:
                    candidates.append(node)
        if not candidates:
            return None, None
        # 去重后取影响面最大的节点
        best_node = None
        best_impact = -1.0
        seen = set()
        for n in candidates:
            if n.node_id in seen:
                continue
            seen.add(n.node_id)
            impact = self.graph_index.compute_impact(n.node_id)
            if impact > best_impact:
                best_impact = impact
                best_node = n
        return best_node, best_impact

    def _node_context_text(self, node_id: str) -> str:
        """单个节点的图上下文（L1 邻接 + 影响面），供 Traceback 新起点注入。"""
        lines = [
            f"【新起点图上下文】{node_id} 影响面={self.graph_index.compute_impact(node_id):.4f}"
        ]
        neighbors = self.graph_index.get_neighbors(node_id, hops=1)
        for depth, items in neighbors.get("neighbors", {}).items():
            for it in items:
                lines.append(f"  [{it['direction']} {it['edge_type']}] {it['node']}")
        return "\n".join(lines)

    def _on_enter_rollback(self) -> None:
        """ROLLBACK 状态：代价熔断，恢复初始快照 + 重置规划路径。"""
        self._log_state_enter("rollback", self.attempt)

        # 恢复初始快照（所有被编辑文件）
        restored = 0
        for path in list(self.checkpoint.snapshots):
            if self.checkpoint.restore(path):
                restored += 1
        print(f"[ROLLBACK] 已恢复 {restored} 个文件的初始快照")
        self.logger.snapshot_restored(f"<{restored} files>")
        self.logger.rollback_triggered("rollback", self.bug_file)

        # Phase 4 机制四：权重回退到初始快照（失败路径不污染图）
        try:
            if self._initial_weights:
                self.graph_manager.restore_weights_snapshot(self._initial_weights)
                print("[ROLLBACK] 权重已回退到初始快照")
        except Exception as e:
            print(f"[ROLLBACK] 权重回退失败: {e}")

        # 缺陷3：回滚 = 失败一次，记录 fail_count（放在权重回退之后：
        # fail 是历史事实不该被回退，success 回退但 fail 保留）
        try:
            for fp, _, _ in self._edited_ranges:
                base = os.path.basename(fp)
                for n in self.graph_index.graph.nodes.values():
                    if n.node_type.value == "function" and os.path.basename(n.file) == base:
                        self.graph_manager.record_failure(n.node_id)
                        break
        except Exception:
            pass

        # 清理规划路径（不沿用旧方案）
        self._edited_ranges = []
        self.rollback_count += 1
        self.attempt = 0  # 每条路径独立规划
        self.watchdog.reset_all()

        if self.rollback_count < self.agent_config.rollback_limit:
            print(f"[ROLLBACK] 重新规划（{self.rollback_count}/{self.agent_config.rollback_limit}）")
            hist = "、".join(self._cognition_history[-5:]) or "（无）"
            self.messages.append({"role": "user", "content": (
                "上一轮修改路径已回退，当前文件状态已恢复初始快照。\n"
                f"[历史认知] 你之前完成的修改: {hist}\n"
                "请基于当前状态重新规划，尝试不同的修复策略。"
                "上一轮的规划路径已清理，请勿沿用。"
            )})
            self._rollback_notice = True
            self.rollback_retry()  # → locate
        elif self.effective_mode == "dp":
            print("[ROLLBACK] DP 反复失效，降级到 Greedy 模式")
            self._switch_mode("greedy", reason="rollback_limit_exceeded")
            # Greedy 清空上下文（含图上下文），不让 DP 模式的路径惯性干扰
            self.messages = [{"role": "user", "content": self._plain_task_msg}]
            self._rollback_notice = True
            self.degrade()  # → locate
        else:
            print("[ROLLBACK] Greedy 模式也无法解决，交给人类")
            self.rollback_fail()  # → fail

    def _switch_mode(self, new_mode: str, reason: str) -> None:
        """DP ↔ Greedy 模式切换（记录 mode_switched 日志）。

        评测消融（2026-08-05）：no_degrade=True 时禁止降级，
        保证消融对比"唯一变量是图信息"（dp-无图 vs dp 都固定不降级）。
        """
        if self.no_degrade and new_mode == "greedy" and reason != "token_budget_exceeded":
            # 不降级：当作失败处理（回滚计数到上限即失败）
            print(f"[MODE] no_degrade 禁止降级（{reason}），按失败处理")
            self.rollback_fail()
            return
        old = self.effective_mode
        self.effective_mode = new_mode
        self.logger.mode_switched(old, new_mode, reason)
        print(f"[MODE] {old} → {new_mode} ({reason})")

    def _success_weight_targets(self) -> list[str]:
        """解析成功修复实际涉及的函数节点（用于动态权重 +1）。

        优先：根据 edit_function 编辑的行范围，匹配图中包含该范围的文件内函数。
        兜底：bug 文件中入度最高的函数。
        """
        targets = []
        for file_path, start, end in self._edited_ranges:
            base = os.path.basename(file_path)
            for n in self.graph_index.graph.nodes.values():
                if n.node_type.value != "function":
                    continue
                if os.path.basename(n.file) != base:
                    continue
                # 编辑行范围与函数体有交集
                if n.lineno <= end and start <= n.end_lineno:
                    targets.append(n.node_id)
        if targets:
            return list(dict.fromkeys(targets))

        # 兜底：无编辑记录（异常路径），记 bug 文件核心函数
        bug_base = os.path.basename(self.bug_file)
        candidates = []
        for n in self.graph_index.graph.nodes.values():
            if n.node_type.value != "function":
                continue
            if os.path.basename(n.file) == bug_base:
                candidates.append(n)
        if not candidates:
            return []
        return [max(candidates, key=lambda n: n.in_degree).node_id]

    def _on_enter_success(self) -> None:
        # Phase 4：任务成功，清理快照（权重已持久化保留）
        self.snapshot_mgr.clear()
        """SUCCESS 状态：修复成功。"""
        # Greedy 修复成功 → 回写图权重已做，切回 DP（模块 F）
        if self.effective_mode == "greedy":
            self._switch_mode("dp", reason="greedy_success")
        self.rollback_count = 0
        self._check_fail_count = 0

        print(f"\n{'='*50}")
        print(f"[SUCCESS] 修复成功！共尝试 {self.attempt + 1} 次")
        print(f"{'='*50}")
        self.logger.success(self.attempt + 1)

    def _on_enter_fail(self) -> None:
        # Phase 4 机制四：任务失败，权重回退到初始快照
        try:
            if self._initial_weights:
                self.graph_manager.restore_weights_snapshot(self._initial_weights)
                print("[FAIL] 权重已回退到初始快照")
        except Exception:
            pass
        """FAIL 状态：修复失败。"""
        reason = self._cancel_reason or f"已用尽 {self.max_retries + 1} 次尝试"
        print(f"\n{'='*50}")
        print(f"[FAIL] 修复失败，{reason}")
        print(f"{'='*50}")
        self.logger.fail(self.attempt + 1)

    # ========== 外部控制 ==========

    def cancel(self, reason: str = CancelReason.USER) -> None:
        """外部取消：统一中断处理（回滚 + 强制终止 + 记录原因）。"""
        handle_cancel(self, reason)

    def run(self) -> bool:
        """运行 Agent 修复 bug。

        Returns:
            True 表示修复成功，False 表示失败
        """
        self._on_enter_init()
        success = self.state == "success"
        # Phase 6：任务结束清理工作副本（成功/失败都不留副本）
        if self.sandbox and self._l1 is not None:
            self._l1.cleanup()
        return success


def run_fsm_agent(
    bug_file: str,
    test_command: str,
    max_retries: int = 2,
    python_version: str = "3.11",
    packages: list[str] | None = None,
    mode: str = "auto",
) -> bool:
    """运行 FSM Agent 修复 bug（便捷函数）。

    Args:
        bug_file: 有 bug 的 Python 文件路径
        test_command: 测试命令
        max_retries: 最大重试次数
        python_version: Docker 容器的 Python 版本
        packages: 需要预装的包列表
        mode: 运行模式（dp/greedy/auto）

    Returns:
        True 表示修复成功，False 表示失败
    """
    fsm = AgentFSM(
        bug_file=bug_file,
        test_command=test_command,
        max_retries=max_retries,
        python_version=python_version,
        packages=packages,
        mode=mode,
    )
    return fsm.run()
