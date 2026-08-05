# -*- coding: utf-8 -*-
"""分层验证调度（Phase 7 模块 D）：L1 现成测试 → L3 Probe → L2 影响面裁剪回归。

按可用性降级执行：
    L1：现成测试（断言验证，最可信）——有测试命令就用
    L3：无测试时的语义验证（Probe 生成 + 隔离执行）
    L2：影响面裁剪回归——修复验证通过后，跑覆盖被改文件的测试（不真全量）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from .coverage import CoverageMap
from .probe import ProbeSpec, ProbeGenerator, ProbeResult


@dataclass
class VerificationResult:
    """分层验证结果。"""
    layer: str                    # "L1" / "L3" / "L2"
    passed: bool
    detail: str = ""
    probe_result: Optional[ProbeResult] = None
    coverage: dict = field(default_factory=dict)


class VerificationScheduler:
    """验证调度器。"""

    def __init__(self, code_dir: str, llm_client=None, verbose: bool = True):
        self.code_dir = code_dir
        self.llm_client = llm_client
        self.verbose = verbose
        self.coverage = CoverageMap(code_dir)

    # ── L1：现成测试 ──
    def run_l1(self, test_command: str, timeout: int = 300) -> VerificationResult:
        """跑现成测试（复用 sandbox 的 run_in_docker）。"""
        try:
            from swe_agent.sandbox.docker_runner import run_in_docker
            container_cmd = test_command
            if self.code_dir in container_cmd:
                container_cmd = container_cmd.replace(self.code_dir, "/workspace")
            result = run_in_docker(self.code_dir, container_cmd)
            passed = result.exit_code == 0
            if self.verbose:
                print(f"[VERIFY][L1] exit={result.exit_code} "
                      f"passed={passed}")
            return VerificationResult(
                layer="L1", passed=passed,
                detail=(result.stdout or "")[-500:] + (result.stderr or "")[-500:],
            )
        except Exception as e:
            return VerificationResult(layer="L1", passed=False, detail=f"L1 异常: {e}")

    # ── L3：Probe 兜底 ──
    def run_l3(self, issue: str, target_file: str,
               target_functions: list[str]) -> VerificationResult:
        spec = ProbeSpec(
            issue=issue,
            target_file=target_file,
            target_functions=target_functions,
            code_dir=self.code_dir,
        )
        gen = ProbeGenerator(spec, llm_client=self.llm_client, verbose=self.verbose)
        result = gen.run()
        if self.verbose:
            print(f"[VERIFY][L3] passed={result.passed} "
                  f"error={result.error or '无'}")
        return VerificationResult(
            layer="L3", passed=result.passed,
            detail=result.output[-500:], probe_result=result,
        )

    # ── L2：影响面裁剪回归 ──
    def run_l2(self, test_command: str, edited_files: list[str],
               timeout: int = 300) -> VerificationResult:
        """裁剪回归：只跑覆盖被改文件的测试。

        无覆盖映射时回退全量（安全默认，绝不漏测）。
        """
        pruned = self.coverage.prune_command(test_command, edited_files)
        if pruned == test_command:
            if self.verbose:
                print("[VERIFY][L2] 无覆盖映射 → 全量回归（安全默认）")
        else:
            if self.verbose:
                print(f"[VERIFY][L2] 裁剪回归: {pruned}")
        return self.run_l1(pruned, timeout)

    # ── 完整验证闭环（修复后调用） ──
    def verify_fix(self, *, issue: str, test_command: str,
                   target_file: str, target_functions: list[str],
                   edited_files: list[str]) -> VerificationResult:
        """修复后验证：L1 优先 → L3 兜底 → L2 裁剪回归。

        返回最终验证结果（L2 是附加回归，L1/L3 决定主结论）。
        """
        # 1. 主验证：L1 或 L3
        if test_command:
            main = self.run_l1(test_command)
        else:
            main = self.run_l3(issue, target_file, target_functions)

        # 2. L2 附加回归（主验证通过后）
        if main.passed and edited_files:
            l2 = self.run_l2(test_command or "pytest", edited_files)
            if not l2.passed and test_command:
                if self.verbose:
                    print("[VERIFY] ⚠️ L2 裁剪回归失败（主验证通过但回归暴露问题）")
                main.passed = False
                main.detail += "\n[L2] " + l2.detail[-300:]

        return main
