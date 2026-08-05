# -*- coding: utf-8 -*-
"""diagnose 结构化输出模型（Pydantic 校验，衔接 FSM 入口）。"""

from pydantic import BaseModel, Field
from typing import Optional


class DiagnoseCandidate(BaseModel):
    """单个 bug 候选（按置信度排序）。"""

    file: str = Field(description="候选 bug 文件路径（仓库相对路径）")
    functions: list[str] = Field(default_factory=list, description="候选函数名列表")
    reason: str = Field(default="", description="定位理由（引用 Issue 锚点/图结构）")
    test_anchor: Optional[str] = Field(
        default=None, description="测试锚点：可用的测试命令/测试文件（若已知）")


class DiagnoseResult(BaseModel):
    """diagnose 输出：候选集 + 测试锚点 + 置信度。"""

    issue: str = Field(description="原始 Issue 描述")
    candidates: list[DiagnoseCandidate] = Field(
        default_factory=list, description="候选 bug 列表，按置信度降序")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="整体置信度")
    summary: str = Field(default="", description="一句话诊断总结")

    def best(self) -> Optional[DiagnoseCandidate]:
        return self.candidates[0] if self.candidates else None
