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
    # 2026-08-13 提交机制：test exit 0 官方模式 → 回 patch 继续（agent 决定提交）
    {"trigger": "test_ok", "source": "test", "dest": "patch"},
    # agent 主动提交 → 交卷（2026-08-13 触发源放宽，见下方 submitted 定义）
    {"trigger": "test_fail", "source": "test", "dest": "locate"},
    # 2026-08-13 交卷触发源放宽：official 模式 submitted() 会在 locate/test 状态被调用
    # （_run_llm_turn 内），原来只有 patch 源 → 从 locate/test/check 调会 MachineError 崩溃
    {"trigger": "submitted", "source": ["patch", "locate", "test", "check"], "dest": "success"},
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


# 2026-08-13 图上下文瘦身：L1 邻域节点上限（seed 优先 + 高 in_degree 核心枢纽）
MAX_L1_NEIGHBOR_NODES = 30
# 历史裁剪阈值（2026-08-13 v2）：单次请求体超限会压垮 NAT/代理链路；且官方模式
# 全量重发累积历史是 token 爆炸主因（实测 7.5M 里 7.3M 是 cache-read 的重发）。
# 2M 字符（≈50万token）太宽——实测对话涨到 ~1.5M 字符从不裁剪。压到 80 万字符
# （≈20万token），超限即保留 system + 首条任务 + 最近 N 条（LLM 依赖近期上下文）。
MAX_HISTORY_CHARS = 800000
KEEP_LAST_MESSAGES = 25


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
        early_stop_min_attempts: int = 3,
        official_mode: bool = False,
        problem_statement: str = "",
        load_prior: bool = True,
        repo_key: str = "",
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
        # 免死期：前 N 次尝试不触发早停（给积累期，2026-08-08 三修①）
        self.early_stop_min_attempts = max(1, early_stop_min_attempts)
        # 官方模式（2026-08-13）：无 test_patch，自带测试不验证 bug →
        # 要求 LLM 用 run_command 自验证并声明，FSM 检查验证证据
        self.official_mode = official_mode
        self.problem_statement = problem_statement
        # 2026-08-13 工具硬上限废弃（用户反馈：120 次硬上限太垃圾）——改为可配置，
        # 默认 None=关闭；防无限试探改由 watchdog 无进展检测 + max_retries 兜底
        self.max_tool_calls = getattr(config or AgentConfig(), "max_tool_calls", None)
        self._verified = False  # 官方模式：是否已运行测试/验证命令（交卷前置条件）
        self._no_edit_strikes = 0  # 官方模式：无修改就结束回合的连续次数（升级干预）

        # 轨迹落盘（2026-08-13）：验证脚本记录 + 上下文长度统计
        self.verification_log = []  # run_command 调用记录（参数+输出截断）
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
        # 2026-08-14 跨会话先验：仓库级"历史易出 bug 区域"软偏置（评测禁用，防剧透）
        self.load_prior = load_prior
        self.repo_key = repo_key or os.path.basename(self.code_dir)
        if load_prior:
            self.graph_manager.apply_prior(self.repo_key)
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
            graph_level=self.graph_level,
            block_network=self.official_mode,  # 2026-08-13 官方模式封 run_command 网络
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
        self._guided_nodes: set = set()         # 2026-08-14 已注入过阅读指南的节点（去重）

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
        # 2026-08-14：全量 L-1（35K）换成紧凑源码包模块树（~0.5-2K，含 docstring 用途）
        if self.graph_level >= 2:
            parts.append(self.graph_index.module_tree_text(self.bug_file, with_purpose=True))

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

        # 2026-08-13 用户定稿（修正）：
        #   L1 模式（graph_level==1，只有细准）：L1 邻域注入保留（消融一档）
        #   L2 模式（graph_level==2，完整）：细准改按需（view_file 附带清单），
        #     注入只剩粗准 L-1+L0（≈8k/轮，比 L1 更省）
        if self.graph_level == 1:
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
                key=lambda x: (0 if x.node_id in seed_ids else 1, -x.in_degree, x.file, x.lineno),
            )
            # 2026-08-13 瘦身：邻域节点上限（seed 优先 + 核心枢纽），防止邻居爆炸
            nbr_nodes = nbr_nodes[:MAX_L1_NEIGHBOR_NODES]
            kept = {n.node_id for n in nbr_nodes}
            nbr_edges = [e for e in g.edges if e.source in kept and e.target in kept]
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

    def _trim_history(self) -> None:
        """裁剪对话历史：总字符超限时保留 system + 最近 KEEP_LAST_MESSAGES 条。"""
        total = sum(len(str(m.get("content", ""))) for m in self.messages)
        if total <= MAX_HISTORY_CHARS:
            return
        # 保留第一条（system/初始说明）+ 最近 N 条
        head = self.messages[:1]
        tail = self.messages[-KEEP_LAST_MESSAGES:]
        self.messages = head + [
            {"role": "system", "content": "[上下文裁剪] 早期工具结果已裁剪（历史超限）。"
                                          "基于当前可见信息继续任务。"}] + tail
        print(f"  [TRIM] 历史裁剪: {total:,} 字符 → 保留最近 {KEEP_LAST_MESSAGES} 条")

    def _run_llm_turn(self) -> str:
        """调用 LLM 进行工具调用回合（LOCATE 与 PATCH 重修复共用）。"""
        self._trim_history()
        def tool_executor(tool_name: str, arguments: dict) -> str:
            # 编辑前先记录初始快照（保证 ROLLBACK 能恢复编辑前状态）
            if tool_name == "edit_function":
                fp = self._resolve_file(arguments.get("file_path", ""))
                self.checkpoint.save_initial(fp)

            # 2026-08-13 工具调用计数（修复 double-increment：原 509/567 各 +1，
            # 120 硬上限实际 60 次真实调用就触发——假超限）
            self.tool_call_count += 1
            # 可配置安全上限（默认 None=关闭）。硬上限 120 已按用户反馈废弃，
            # 官方模式防无限试探改由 watchdog 无进展检测 + max_retries 兜底
            if self.max_tool_calls is not None and self.tool_call_count > self.max_tool_calls:
                _has_edit = bool(getattr(self, "_edited_ranges", None))
                print(f"  [LIMIT] 工具调用超限（{self.tool_call_count}/{self.max_tool_calls}）——强制{'交卷' if _has_edit else '失败'}")
                if _has_edit:
                    self.submitted()
                    return "已交卷"
                self.cancel(reason=CancelReason.API_ERROR)
                return "失败"

            print(f"  [TOOL] 调用 {tool_name}({json.dumps(arguments, ensure_ascii=False)})")
            result = self.registry.execute(tool_name, arguments)
            result_data = json.loads(result)

            # 轨迹落盘：run_command 验证记录（官方模式自验证证据）
            if tool_name == "run_command":
                self.verification_log.append({
                    "tool": tool_name,
                    "arguments": arguments,
                    "output_excerpt": str(result)[:400],
                })
            # 官方模式验证证据：run_test 或 python/pytest 类 run_command = 已自验证
            if tool_name == "run_test":
                self._verified = True
            elif tool_name == "run_command":
                _cmd = str(arguments.get("command", ""))
                if "python" in _cmd or "pytest" in _cmd:
                    self._verified = True

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
                    self._no_edit_strikes = 0  # 开始修改后，重置无修改升级计数
                    self._edited_ranges.append((
                        self._resolve_file(file_path),
                        int(result_data.get("start_line", 1)),
                        int(result_data.get("end_line", 1)),
                    ))
                    # Phase 4 机制二：认知保持（记录修改历史，回退时注入）
                    self._cognition_history.append(
                        f"{os.path.basename(file_path)}:{result_data.get('start_line', 1)}"
                        f"-{result_data.get('end_line', 1)} 编辑")

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
        # 2026-08-13 最简交卷（用户定稿，移除 submit 工具）：LLM 回复【无工具调用】
        # = 自然完成 = 交卷（mini-swe agent 思路——零协议）
        if self.official_mode:
            _last = conversation[-1] if conversation else {}
            if not _last.get("tool_calls") and _last.get("role") == "assistant":
                _has_edit = bool(getattr(self, "_edited_ranges", None))
                if _has_edit:
                    if not getattr(self, "_verified", False):
                        # 有修改但没验证 → 不交卷，先要求验证（自评可靠性围栏）
                        self.messages.append({"role": "user", "content":
                            "[系统] 你已经修改了代码，但还没有运行任何验证。交卷前请先运行 "
                            "run_test（或用 run_command 运行 python/pytest）确认修改可运行。"})
                        print("  [DONE-未验证] 有修改但未验证——要求先跑测试")
                        return "继续"
                    print("  [DONE] agent 无工具调用回复 + 已验证——交卷")
                    self.submitted()  # → success
                    return "已交卷"
                # 无修改就结束 → 升级干预（flash 不主动 edit 的核心对策）：
                # 第1/2次 nudge，第3次强指令，第4次判失败（不给 watchdog 5 次 locate 抢杀）
                self._no_edit_strikes += 1
                _strike = self._no_edit_strikes
                if _strike >= 4:
                    self.messages.append({"role": "user", "content":
                        "[系统] 你多次未修改任何代码，本次尝试结束（无进展）。"})
                    print("  [DONE-但无修改] 连续无修改达上限——判失败")
                    self.cancel(reason=CancelReason.NO_PROGRESS)
                    return "失败"
                if _strike >= 3:
                    _msg = ("[强制] 最后一次机会：请立即用 edit_function 修改 bug 文件"
                            "（任务开头给出的文件路径）实现修复。本回合若不修改，将直接失败。")
                elif _strike >= 2:
                    _msg = ("[警告] 你第二次未修改代码就结束回合。只读不写不会成功。"
                            "请立即用 edit_function 修改 bug 文件（任务开头给出的文件路径）。")
                else:
                    _msg = ("[提示] 你还未修改任何代码。任务开头给出了 bug 文件路径——"
                            "请用 edit_function 修改该文件代码修复问题，再用 run_test 验证。不要直接结束。")
                self.messages.append({"role": "user", "content": _msg})
                print(f"  [DONE-但无修改] 无工具调用但未 edit（第 {_strike} 次）——提示继续")
                return "继续"
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
            f"问题描述：\n{self.problem_statement}\n\n"
            # 2026-08-13 用户定稿：骨架不再全量注入（9.1k/轮）——
            # 函数级视图由 view_file 按需提供（_file_symbol_listing）
            f"修复完成后运行测试：{self._display_command()}"
        )

        # DP 模式：注入图索引上下文 + 围栏软约束
        user_content = self._plain_task_msg
        if self.effective_mode in ("dp", "auto"):
            user_content += f"\n\n{self._graph_context_text()}"
            fence_text = self.fence.fence_text()
            if fence_text:
                user_content += f"\n\n{fence_text}"

        # 2026-08-13 官方模式：初始即强化"必须 edit + 离线"契约（flash 不主动 edit 的对策）
        if self.official_mode:
            user_content += ("\n\n【官方模式要求（必须遵守）】\n"
                "1. 必须用 edit_function 实际修改 bug 文件（任务开头给出的文件路径）才能解决问题；\n"
                "   只读文件 / 只跑命令 / 从不修改 = 失败。\n"
                "2. 修改后用 run_test 验证你的改动。\n"
                "3. 严禁联网或读取仓库之外的内容——这是严格离线任务，外部代码对你无用。\n"
                "4. 行动偏向：信息够了就动手，不要反复确认；给具体修复方案而不是罗列选项。\n"
                "5. edit_function 推荐用 old_string→new_string 精确替换（无需行号），改起来最快。")

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
        self._verified = False
        self._no_edit_strikes = 0
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
            # 2026-08-13 用户定稿修正：阈值 3→8——连续语法失败是常态
            # （flash 写代码语法出错概率高——3 次就回滚会误杀丢成功修改）
            # 8 次还错才是真问题（上下文错乱）→ 回滚兜底（防 patch↔check 死循环）
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
                # 4. 修改链路阅读指南（2026-08-14）：高影响/签名变更时注入 up/down 链路
                self._inject_change_guide(impact)

        self.check_pass()  # → test

    def _inject_change_guide(self, impact: dict) -> None:
        """高影响/签名变更时，注入"修改链路阅读指南"（调用方 up + 被调 down + 建议）。

        - gated：只对高影响（影响面 ≥ 阈值）节点注入，非每编辑
        - 去重：同节点只注入一次（_guided_nodes），省 token
        - 追加到 messages 末尾 → 不改前缀，缓存命中率基本不降
        - 软约束：建议性质，不拦截；作用 = 给 flash 一个具体沿图阅读路径
        """
        if not getattr(self, "graph_index", None):
            return
        guide = []
        for nid in impact.get("nodes", []):
            if nid in self._guided_nodes:
                continue
            detail = self.graph_index.compute_impact_detail(nid)
            # 门槛：连通节点数 ≥ 3 才引导（有实际调用关系的改动才有读链路的必要）
            if detail.get("affected_nodes", 0) < 3:
                continue
            up = [d["node"] for d in detail.get("up_details", [])[:3]]
            down = [d["node"] for d in detail.get("down_details", [])[:3]]
            guide.append(
                f"- {nid}（影响面 {detail['total_cost']:.4f}）\n"
                f"  调用方（改动会影响谁，去读验证兼容）: {', '.join(up) if up else '无'}\n"
                f"  被调（依赖什么，确认还在）: {', '.join(down) if down else '无'}"
            )
            self._guided_nodes.add(nid)
        if guide:
            msg = ("【图反馈 · 修改链路】你这次改动涉及高影响节点，建议沿图读：\n"
                   + "\n".join(guide)
                   + "\n→ 先读直接调用方验证改动兼容，再跑测试；若测试失败且改动范围大，考虑缩小改动。")
            self.messages.append({"role": "user", "content": msg})
            print(f"[GUIDE] 已注入修改链路阅读指南（{len(guide)} 个节点）")

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

        # 2026-08-13 官方模式：尚未编辑任何代码 → 跳过 Docker 测试运行（打破空转循环）。
        # 原空转：locate→patch→check→test(无修改)→"确认交卷"→无工具回复→nudge→test_ok→
        #   又跑全量测试…每轮烧 30-60s Docker 且无任何新信息——硬上限 120 就是为它兜底的
        if self.official_mode and not self._edited_ranges:
            print("[TEST] 官方模式：尚未编辑任何代码，跳过测试运行")
            self.messages.append({"role": "user", "content":
                "[系统] 你还没有修改任何业务代码。请先定位问题（search_function / view_file），"
                "再用 edit_function 实施修改，最后运行测试验证。不要在没有修改时反复进入验证。"})
            self._last_test_failed = False
            self.test_fail()  # → locate（干净新起点，不注入失败上下文）
            return

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
            # 2026-08-13 最简交卷：test exit 0 → test 状态内调 LLM 工具循环
            # （agent 继续分析/修改——无工具调用回复=交卷；patch 过渡不调 LLM——死循环修复）
            if self.official_mode:
                _edits_before = len(self._edited_ranges)
                self.messages.append({"role": "user", "content":
                    "[测试反馈] 现有测试全部通过（无回归）。注意：现有测试通过 ≠ 问题一定已修复"
                    "（问题可能未被现有测试覆盖）。请对照【问题描述】做最终判断："
                    "若你认为问题已真正修复，直接回复『我已完成修复』即可交卷；"
                    "若还需修改，继续调用工具。"})
                _final = self._run_llm_turn()  # agent 继续工具循环（或交卷）
                if self.state in ("fail", "success"):
                    return
                if len(self._edited_ranges) > _edits_before:
                    self.test_ok()  # 有新修改 → 再验一轮
                else:
                    # 没改也没交卷 → 不空转再跑测试，回定位重来
                    self.messages.append({"role": "user", "content":
                        "[系统] 你既未修改也未交卷。请二选一：用 edit_function 实际修改，"
                        "或直接回复『我已完成修复』。"})
                    self._last_test_failed = False
                    self.test_fail()  # → locate
                return
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

            # 影响面（2026-08-14 改为"信号"而非"独立开关"）：高影响只在"无进展"时触发回滚，
            # 避免误杀合法大重构（之前 impact≥阈值 单独熔断 → 一次失败就回滚）
            impact = compute_edited_impact(
                self.graph_index, self._edited_ranges, self.agent_config, self.fence
            )
            impact_high = impact["total"] >= self.agent_config.impact_threshold
            if impact_high:
                print(f"[TEST] ⚠️ 影响面高 {impact['total']:.4f}（旁路信号，不单独熔断）")
            if self.step > self.agent_config.max_steps:
                print(f"[TEST] 步数超限（{self.step}/{self.agent_config.max_steps}），失败")
                self.retries_exhausted()
                return

            # 记录尝试轨迹（供影响面/早停决策）
            fail_sig = frozenset(
                f"{e.file}:{e.lineno}:{e.error_type}" for e in parsed.grouped_errors)
            self.attempt_trajectory.append({
                "attempt": self.attempt,
                "fail_count": len(parsed.grouped_errors),
                "fail_signature": fail_sig,
                "token": self.token_budget.total,
                "cost": self.token_budget.estimate_cost(),
            })

            # 高影响 + 失败数未优于历史最优 → 回滚（真死路）；其余情况让测试结果主导
            if impact_high and len(self.attempt_trajectory) >= 2:
                best_prev = min(x["fail_count"] for x in self.attempt_trajectory[:-1])
                if self.attempt_trajectory[-1]["fail_count"] >= best_prev:
                    print(f"[TEST] 高影响 + 失败数未优于历史最优"
                          f"（{self.attempt_trajectory[-1]['fail_count']} ≥ {best_prev}）→ 回滚")
                    self.logger.rollback_triggered("impact", self.bug_file)
                    self.rollback()  # test → rollback
                    return

            # 收益早停 v2（2026-08-14 修复反效果）：
            # 原逻辑把"错误签名变化"当进展（换方向≠收敛）→ 卡住的 agent 永不被停 / 误停。
            # 现在只看失败数：最近 patience 次里是否刷新"窗口前最优失败数"（允许中途波动）。
            if (self.early_stop
                    and len(self.attempt_trajectory) > self.early_stop_min_attempts
                    and len(self.attempt_trajectory) > self.early_stop_patience):
                recent = self.attempt_trajectory[-self.early_stop_patience:]
                best_before = min(
                    x["fail_count"] for x in self.attempt_trajectory[:-self.early_stop_patience])
                improved = any(x["fail_count"] < best_before for x in recent)
                if not improved:
                    print(f"[EARLY-STOP] 最近 {self.early_stop_patience} 次未刷新窗口前最优"
                          f"（best_before={best_before}，最近 {[x['fail_count'] for x in recent]}）")
                    if self.no_degrade:
                        # 评测禁降级（消融变量控制）：直接停止
                        print("[EARLY-STOP] no_degrade 模式，直接停止")
                        self.retries_exhausted()
                    else:
                        # 产品模式：降级清上下文重来（最后机会）
                        print("[EARLY-STOP] 降级 Greedy 重试")
                        self._switch_mode("greedy", reason="early_stop")
                        self.messages = [{"role": "user", "content": self._plain_task_msg}]
                        self._rollback_notice = True
                        self.degrade()  # → locate
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
        # 跨会话先验（2026-08-14）：验证成功的会话 → 成功节点并入仓库级先验。
        # 只在 load_prior 开启（评测关）时写入——坏会话/评测不污染、不剧透。
        if getattr(self, "load_prior", False):
            try:
                self.graph_manager.merge_prior(self.repo_key, self._success_weight_targets())
            except Exception:
                pass
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
