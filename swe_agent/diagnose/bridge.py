# -*- coding: utf-8 -*-
"""diagnose → FSM 桥接（Phase 7 模块 B 衔接）+ 缺陷5 定位漂移反馈环。

流程：
    Issue → diagnose（候选集）→ 依次尝试候选 → FSM 修复
    缺陷5：FSM 失败后解析 last_test.log 的 Traceback，
           Traceback 首文件不在剩余候选集 → "定位漂移"信号 → 换候选/重定位
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import DiagnoseResult, DiagnoseCandidate


@dataclass
class FixAttemptResult:
    """单次候选的修复结果。"""
    candidate: DiagnoseCandidate
    success: bool
    drift_detected: bool = False      # 缺陷5：Traceback 指向候选集外文件
    drift_file: Optional[str] = None  # Traceback 实际指向的文件
    attempts: int = 0
    error: str = ""


@dataclass
class DiagnoseFixResult:
    """diagnose → FSM 全链路结果。"""
    issue: str
    success: bool
    attempts: list[FixAttemptResult] = field(default_factory=list)
    drift_count: int = 0
    final_message: str = ""

    @property
    def used_candidates(self) -> int:
        return len(self.attempts)


def _extract_traceback_first_file(code_dir: str) -> Optional[str]:
    """从 .graph/last_test.log 提取 Traceback 第一个项目文件（缺陷5 用）。"""
    log_path = os.path.join(code_dir, ".graph", "last_test.log")
    if not os.path.exists(log_path):
        return None
    try:
        from swe_agent.graph.traceback_parser import parse_traceback
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        result = parse_traceback(raw, code_dir)
        if result.new_start is not None:
            return result.new_start.file
    except Exception:
        return None
    return None


def _candidate_files(result: DiagnoseResult) -> list[str]:
    """候选文件列表（规范化相对路径）。"""
    files = []
    for c in result.candidates:
        f = c.file.replace("\\", "/")
        # 去掉可能的绝对路径前缀，取相对路径
        base = os.path.basename(f)
        files.append(base if base else f)
    return files


def _matches(fs: list[str], tb_file: Optional[str]) -> bool:
    """Traceback 文件是否命中候选集（缺陷5 判定）。"""
    if tb_file is None:
        return True  # 无 Traceback 信息 → 不判漂移
    tb_base = os.path.basename(tb_file)
    for f in fs:
        if tb_base and os.path.basename(f) == tb_base:
            return True
    return False


def run_diagnose_fix(
    issue: str,
    project_dir: str,
    *,
    max_candidates: int = 3,
    mode: str = "auto",
    max_retries: int = 2,
    llm_client=None,
    diagnose_agent=None,
    verbose: bool = True,
) -> DiagnoseFixResult:
    """diagnose → FSM 全链路入口。

    Args:
        issue: 自然语言 Issue
        project_dir: 项目目录
        max_candidates: 最多尝试几个候选
        mode: FSM 模式（dp/greedy/auto）
        max_retries: FSM max_retries
        llm_client: 可选注入（测试用）
        diagnose_agent: 可选注入（测试用）

    Returns:
        DiagnoseFixResult
    """
    from swe_agent.diagnose.agent import DiagnoseAgent
    from swe_agent.fsm.agent_fsm import AgentFSM

    if diagnose_agent is None:
        diagnose_agent = DiagnoseAgent(project_dir=project_dir, verbose=verbose)
    if llm_client is not None:
        diagnose_agent.llm_client = llm_client

    result = DiagnoseFixResult(issue=issue, success=False)

    # 1. diagnose
    if verbose:
        print(f"\n{'='*60}\n[PHASE7] diagnose 定位: {issue}\n{'='*60}")
    diag = diagnose_agent.diagnose(issue)
    candidates = diag.candidates[:max_candidates]
    if not candidates:
        result.final_message = "diagnose 未定位到候选（无 oracle 不修复）"
        return result

    cand_files = _candidate_files(diag)
    remaining_files = list(cand_files)

    # 2. 依次尝试候选（缺陷5：漂移 → 换下一个）
    for idx, cand in enumerate(candidates, 1):
        bug_file = os.path.join(project_dir, cand.file)
        if not os.path.exists(bug_file):
            # 尝试 basename 查找
            bug_file = os.path.join(project_dir, os.path.basename(cand.file))
        if not os.path.exists(bug_file):
            result.attempts.append(FixAttemptResult(
                candidate=cand, success=False, error=f"文件不存在: {cand.file}"))
            continue

        test_command = cand.test_anchor or ""
        if verbose:
            print(f"\n[PHASE7] 候选 {idx}/{len(candidates)}: {cand.file}")
            print(f"[PHASE7] 测试锚点: {test_command or '(无，需 L3 Probe)'}")

        # 无测试锚点 → 本阶段跳过（L3 Probe 是模块 C）
        if not test_command:
            result.attempts.append(FixAttemptResult(
                candidate=cand, success=False,
                error="无测试锚点（L3 Probe 未实现，等待模块 C）"))
            if verbose:
                print("[PHASE7] ⚠️ 无测试锚点，跳过（模块 C L3 Probe 将兜底）")
            continue

        # 3. FSM 修复
        try:
            fsm = AgentFSM(
                bug_file=os.path.abspath(bug_file),
                test_command=test_command,
                max_retries=max_retries,
                mode=mode,
            )
            success = fsm.run()
        except Exception as e:
            result.attempts.append(FixAttemptResult(
                candidate=cand, success=False, error=f"FSM 异常: {e}"))
            continue

        # 4. 缺陷5：失败时检测定位漂移
        drift_file = None
        drift = False
        if not success:
            drift_file = _extract_traceback_first_file(project_dir)
            if not _matches(remaining_files, drift_file):
                drift = True
                result.drift_count += 1
                if verbose:
                    print(f"[PHASE7] ⚠️ 定位漂移: Traceback 指向 {drift_file}"
                          f"（不在候选集 {remaining_files}）")

        attempt = FixAttemptResult(
            candidate=cand,
            success=success,
            drift_detected=drift,
            drift_file=drift_file,
        )
        result.attempts.append(attempt)
        if success:
            result.success = True
            result.final_message = f"候选 {cand.file} 修复成功"
            return result

        # 移除已尝试候选，继续下一个
        if idx < len(candidates):
            b = os.path.basename(cand.file)
            remaining_files = [f for f in remaining_files if os.path.basename(f) != b]

    result.final_message = (
        f"全部 {len(result.attempts)} 个候选失败"
        + (f"，检测到 {result.drift_count} 次定位漂移（建议重定位）" if result.drift_count else "")
    )
    return result
