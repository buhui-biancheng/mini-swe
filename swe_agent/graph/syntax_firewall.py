"""SyntaxFirewall：静态语法防火墙（Phase 3）。

在 Agent 生成 Patch 后、进入 Docker 沙盒前，用本地 ast.parse 做毫秒级快速拦截。
语法错误的 Patch 直接驳回并返回精确报错行号，不启动 Docker 容器。

确定性保证：ast.parse 是标准库纯语法检查，无外部依赖、无网络、无歧义。
"""

import ast
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SyntaxErrorInfo:
    """单个语法错误。"""
    line: int
    msg: str
    offset: int = 0


@dataclass
class SyntaxCheckResult:
    """语法检查结果。"""
    ok: bool
    errors: list = field(default_factory=list)  # list[SyntaxErrorInfo]

    @property
    def summary(self) -> str:
        """人类可读的错误摘要（供日志/提示词）。"""
        if self.ok:
            return "语法检查通过"
        lines = [f"第 {e.line} 行: {e.msg}" for e in self.errors]
        return "；".join(lines) or "语法错误"


class SyntaxFirewall:
    """静态语法防火墙。

    feature_version：目标环境 Python 版本（如 (3, 8)）——用 ast.parse 的
    feature_version 参数拦截目标环境不支持的语法（如 match 在 3.10+）。
    2026-08-08：评测容器 py3.8 时，本地 3.12 语法检查会放行 3.9+ 语法 → 容器爆。
    """

    def __init__(self, feature_version: Optional[tuple] = None):
        self.feature_version = feature_version

    def check_code(self, code: str, filename: str = "<patch>") -> SyntaxCheckResult:
        """对整段代码做语法检查。

        Args:
            code: 待检查的 Python 源码
            filename: 用于报错的文件名（不影响解析）

        Returns:
            SyntaxCheckResult：ok=False 时 errors 含精确行号
        """
        try:
            if self.feature_version:
                ast.parse(code, filename=filename, feature_version=self.feature_version)
            else:
                ast.parse(code, filename=filename)
            return SyntaxCheckResult(ok=True, errors=[])
        except SyntaxError as e:
            return SyntaxCheckResult(ok=False, errors=[
                SyntaxErrorInfo(
                    line=e.lineno or 1,
                    msg=e.msg,
                    offset=e.offset or 0,
                )
            ])

    def check_file(self, file_path: str) -> SyntaxCheckResult:
        """对文件做语法检查（供 FSM 在 PATCH → TEST 之间调用）。"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
        except (OSError, UnicodeDecodeError) as e:
            # 文件读不了按错误处理（避免带病进 Docker）
            return SyntaxCheckResult(ok=False, errors=[
                SyntaxErrorInfo(line=1, msg=f"文件读取失败: {e}"),
            ])
        return self.check_code(code, filename=file_path)
