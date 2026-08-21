"""测试日志解析器：TEST 出口前置动作（Phase 2 模块 D）。

挂在 TEST 状态出口，绿色跳过，红色解析为结构化错误路径列表。
不是独立模块/状态，执行完立即销毁（原始日志保留并落盘）。

输出：
    grouped_errors: 每个失败用例一条，含 file/lineno + log_start_line/log_end_line
    raw_log_failures: FAILURES 段（上限 ~2000 字符，防上下文膨胀）
    完整日志落盘 .graph/last_test.log（AI 用 view_file 按需读）

确定性保证：纯文本 + 正则解析，无外部依赖、无网络。
"""

import os
import re
from dataclasses import dataclass, field

from .traceback_parser import extract_frames, to_project_rel

# 错误类型（常见异常/错误后缀）
_ERROR_TYPE_RE = re.compile(r'\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning))(?:\s*:|:|\b)')

# 摘要行：src/file.py:42: KeyError 或 FAILED ... - KeyError
_SUMMARY_RE = re.compile(r'([\w./\\-]+\.py):(\d+):\s*([A-Za-z_]\w*)')


@dataclass
class GroupedError:
    """结构化错误路径（LOCATE 收到的最小单位）。"""
    error_type: str
    file: str                  # 项目相对路径
    lineno: int
    function: str
    callsite: list             # list[str]：调用链 "file:lineno"
    log_start_line: int        # 该错误在完整日志中的起始行（1-based）
    log_end_line: int


@dataclass
class TestLogResult:
    """日志解析结果。"""
    exit_code: int
    has_failures: bool
    grouped_errors: list = field(default_factory=list)  # list[GroupedError]
    raw_log_failures: str = ""   # FAILURES 段截断文本
    full_log_path: str = ""      # 完整日志落盘路径


def _split_failure_blocks(stdout: str) -> list[tuple[int, int]]:
    """找到 pytest 下划线分节标题，切成 (start_line, end_line) 块（1-based）。

    start 指向标题行（含），end 指向下一标题行前（不含）。无标题时返回空。
    """
    lines = stdout.splitlines()
    header_idx = []
    for i, line in enumerate(lines):
        s = line.strip()
        if len(s) >= 22 and s.startswith("_") and s.endswith("_"):
            header_idx.append(i)
    blocks = []
    for j, h in enumerate(header_idx):
        end = header_idx[j + 1] if j + 1 < len(header_idx) else len(lines)
        blocks.append((h, end))
    return blocks


def _extract_error_type(text: str) -> str:
    """从错误块提取错误类型。

    顺序：全文中找异常类型后缀（最可靠）→ E 行 → 摘要行。
    摘要行 `file:42: in add` 的 group3 是函数名，不能直接当错误类型。
    """
    # 1. 全文中找常见异常类型（AssertionError/KeyError/ValueError...）
    m = _ERROR_TYPE_RE.search(text)
    if m:
        return m.group(1)
    # 2. E 行兜底：E       CustomError: ...
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("E "):
            m = _ERROR_TYPE_RE.search(s[2:])
            if m:
                return m.group(1)
    # 3. 摘要行兜底：src/file.py:42: KeyError
    m = re.search(r'\.py:(\d+):\s*([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning))', text)
    if m:
        return m.group(2)
    return "Error"


def _extract_file_refs(text: str, code_dir: str) -> list[tuple[str, int, str]]:
    """提取项目文件引用：(file, lineno, funcname)。优先栈帧行，兜底摘要行。"""
    refs = []
    for f in extract_frames(text):
        rel = to_project_rel(f.file, code_dir)
        if rel is None:
            continue
        if any(bad in rel for bad in ("site-packages", "dist-packages", "_pytest")):
            continue
        refs.append((rel, f.lineno, f.funcname))
    if refs:
        return refs
    m = _SUMMARY_RE.search(text)
    if m:
        rel = to_project_rel(m.group(1), code_dir)
        if rel is not None:
            return [(rel, int(m.group(2)), "")]
    return []


def _parse_block(text: str, code_dir: str, start_idx: int, end_idx: int) -> GroupedError:
    """把一个失败块解析为 GroupedError。"""
    refs = _extract_file_refs(text, code_dir)
    if refs:
        file, lineno, funcname = refs[0]
    else:
        file, lineno, funcname = "", 0, ""
    callsite = [f"{r[0]}:{r[1]}" for r in refs]
    return GroupedError(
        error_type=_extract_error_type(text),
        file=file,
        lineno=lineno,
        function=funcname,
        callsite=callsite,
        log_start_line=start_idx + 1,
        log_end_line=end_idx,
    )


def _extract_failures_segment(stdout: str, blocks: list[tuple[int, int]],
                              limit: int = 2000) -> str:
    """从 === FAILURES === 或第一个失败块开始，截断到 limit 字符。"""
    lines = stdout.splitlines()
    if not blocks:
        return ""
    start_line = blocks[0][0]
    # 有显式 "=== FAILURES ===" 标记则从那里开始
    for i, line in enumerate(lines):
        if "=== FAILURES ===" in line or "= FAILURES =" in line:
            start_line = i
            break
    seg = "\n".join(lines[start_line:])
    if len(seg) > limit:
        seg = seg[:limit] + "\n...[截断]"
    return seg


def save_full_log(stdout: str, graph_dir: str, filename: str = "last_test.log") -> str:
    """完整日志落盘到 graph_dir/<filename>，返回路径。

    filename 默认 last_test.log（FSM 测试失败落盘）；终端工具大输出落盘
    复用本函数（run_command → last_cmd.log）。
    """
    os.makedirs(graph_dir, exist_ok=True)
    path = os.path.join(graph_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(stdout)
    return path


def parse_test_log(stdout: str, exit_code: int,
                   code_dir: str = "",
                   failures_segment_limit: int = 2000) -> TestLogResult:
    """解析测试日志。

    Args:
        stdout: 测试完整输出（stdout + stderr）
        exit_code: 测试进程退出码
        code_dir: 项目目录（用于过滤项目文件）
        failures_segment_limit: FAILURES 段截断上限

    Returns:
        TestLogResult：绿色（exit_code==0）时 grouped_errors 为空。
    """
    has_failures = exit_code != 0
    blocks = _split_failure_blocks(stdout) if has_failures else []
    errors = [_parse_block("\n".join(stdout.splitlines()[s:e]), code_dir, s, e)
              for s, e in blocks]

    # 非 pytest 输出（无分节标题）但有 Traceback → 整体当一段
    if has_failures and not errors and ("Traceback" in stdout or "File \"" in stdout):
        errors.append(_parse_block(stdout, code_dir, 0, len(stdout.splitlines())))

    return TestLogResult(
        exit_code=exit_code,
        has_failures=has_failures,
        grouped_errors=errors,
        raw_log_failures=_extract_failures_segment(stdout, blocks, failures_segment_limit),
    )
