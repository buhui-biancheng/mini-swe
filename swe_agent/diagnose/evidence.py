# -*- coding: utf-8 -*-
"""模块 E：证据双呈现 + 业务问题式确认（Phase 7）。

技术层证据：断言 + 输入输出（来自验证结果）
业务层证据：修复前后行为差异翻译成业务语言
业务问题式确认：SUCCESS 前用户背书（"买5件该付多少？"）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Evidence:
    """双呈现证据包。"""
    technical: str = ""        # 技术层：断言/测试输出/覆盖
    business: str = ""         # 业务层：行为差异翻译
    confirm_question: str = "" # 业务问题式确认
    needs_confirmation: bool = True


class EvidenceBuilder:
    """构建证据 + 生成业务确认问题。"""

    @staticmethod
    def technical_evidence(
        *,
        test_command: str,
        passed: bool,
        detail: str = "",
        edited_files: list[str] = None,
        probe_output: str = "",
    ) -> str:
        """技术层证据：测试命令 + 结果 + 断言细节。"""
        lines = [
            "【技术层证据】",
            f"- 验证命令: {test_command or 'L3 Probe'}",
            f"- 结果: {'通过 ✅' if passed else '失败 ❌'}",
        ]
        if edited_files:
            lines.append(f"- 修改文件: {', '.join(edited_files)}")
        if probe_output:
            # 提取断言行
            for line in probe_output.splitlines():
                if "assert" in line or "PROBE_PASS" in line:
                    lines.append(f"- 断言: {line.strip()}")
        if detail and passed is False:
            lines.append(f"- 失败输出: {detail[-300:]}")
        return "\n".join(lines)

    @staticmethod
    def business_evidence(issue: str, passed: bool) -> str:
        """业务层证据：把技术结果翻译成业务语言。

        从 Issue 提取业务要素（数值/行为），生成修复前后差异描述。
        """
        if not passed:
            return "【业务层证据】修复尚未通过验证，暂无法确认业务行为已恢复。"
        # 提取 Issue 中的业务要素
        nums = re.findall(r"\d+", issue)
        funcs = re.findall(r"([a-z_][a-z0-9_]{2,})", issue)
        biz = []
        if nums:
            biz.append(f"涉及数值: {'、'.join(nums[:5])}")
        if funcs:
            biz.append(f"涉及功能: {'、'.join(funcs[:5])}")
        base = "【业务层证据】修复已通过验证，Issue 描述的行为已恢复"
        if biz:
            base += f"（{'；'.join(biz)}）"
        return base + "。技术验证通过，业务行为与 Issue 期望一致。"

    @staticmethod
    def confirm_question(issue: str) -> str:
        """生成业务问题式确认（从 Issue 数值生成一个具体问题）。"""
        nums = re.findall(r"\d+", issue)
        # 尝试生成算术问题（Issue 提到两个数值 + 期望行为时）
        if len(nums) >= 2:
            a, b = int(nums[0]), int(nums[1])
            return (f"业务确认：按 Issue 描述（{a} 和 {b}），"
                    f"你期望的结果是多少？（请直接回答数字）")
        return f"业务确认：Issue「{issue[:40]}」描述的行为现在是否正常？（是/否）"

    @staticmethod
    def build(*, issue: str, test_command: str, passed: bool,
              detail: str = "", edited_files: list[str] = None,
              probe_output: str = "") -> Evidence:
        """一键构建完整证据包。"""
        return Evidence(
            technical=EvidenceBuilder.technical_evidence(
                test_command=test_command, passed=passed, detail=detail,
                edited_files=edited_files, probe_output=probe_output),
            business=EvidenceBuilder.business_evidence(issue, passed),
            confirm_question=EvidenceBuilder.confirm_question(issue),
            needs_confirmation=passed,  # 通过才需要用户背书
        )
