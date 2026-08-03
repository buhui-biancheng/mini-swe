"""Traceback 解析器：从测试日志提取精确出错坐标（Phase 2 模块 E2）。

三条确定性规则：
    规则 1：链式异常取最后一段（"During handling..." 之后最后抛出的才是根因）
    规则 2：只保留项目文件（过滤 site-packages / pytest 内部 / stdlib）
    规则 3：取第一个项目文件作为新起点（FSM 下一轮 LOCATE 的新坐标）

确定性保证：纯正则 + 字符串处理，无外部依赖、无网络、无歧义。
"""

import os
import re
from dataclasses import dataclass, field

# 链式异常分隔符（最后一段 = 最后抛出的异常 = 根因）
_CHAIN_MARKER = "During handling of the above exception, another exception occurred"

# 栈帧行，两种格式都支持：
#   长格式：File "/path/to/x.py", line 42, in funcname
#   短格式：/path/to/x.py:42: in funcname   （pytest 默认 --tb=short）
_FRAME_RE = re.compile(
    r'File\s+"([^"]+\.py)",\s*line\s+(\d+)(?:,\s*in\s+([^\s(]+))?'
    r'|'
    r'([\w./\\\-]+\.py):(\d+):\s*in\s+([^\s(]+)'
)


@dataclass
class TracebackFrame:
    """单个栈帧（项目文件）。"""
    file: str          # 项目相对路径（容器 /workspace 前缀已归一化）
    lineno: int
    funcname: str
    abs_path: str      # 原始路径（可能是容器内路径）

    @property
    def key(self) -> str:
        return f"{self.file}:{self.lineno}"


@dataclass
class TracebackResult:
    """解析结果。"""
    new_start: "TracebackFrame | None" = None   # 规则 3：新起点
    frames: list = field(default_factory=list)  # 最后一段的项目帧
    raw_section: str = ""                       # 链式异常最后一段原文


def to_project_rel(path: str, code_dir: str) -> "str | None":
    """把任意路径归一化为项目相对路径；非项目文件返回 None。

    - 容器路径 /workspace/x → x（Phase 6 的图 key 约定）
    - 宿主机绝对路径在 code_dir 内 → 相对路径
    - 宿主机绝对路径在 code_dir 外（stdlib/site-packages）→ None
    - 相对路径 → 原样返回
    """
    p = path.replace("\\", "/")
    if p.startswith("/workspace/"):
        return p[len("/workspace/"):]
    code_dir_abs = os.path.abspath(code_dir)
    if os.path.isabs(p):
        try:
            rel = os.path.relpath(p, code_dir_abs)
        except ValueError:
            return None
        if rel.startswith(".."):
            return None
        return rel
    return p


def extract_frames(text: str) -> list[TracebackFrame]:
    """提取文本中所有栈帧（原始路径，未过滤）。同时支持长/短两种格式。"""
    frames = []
    for m in _FRAME_RE.finditer(text):
        if m.group(1):   # 长格式 File "...", line N, in func
            frames.append(TracebackFrame(
                file=m.group(1),
                lineno=int(m.group(2)),
                funcname=m.group(3) or "",
                abs_path=m.group(1),
            ))
        elif m.group(4):  # 短格式 path.py:N: in func
            frames.append(TracebackFrame(
                file=m.group(4),
                lineno=int(m.group(5)),
                funcname=m.group(6) or "",
                abs_path=m.group(4),
            ))
    return frames


def split_sections(raw_log: str) -> list[str]:
    """按链式异常分隔符把日志切段。"""
    sections = []
    current = []
    for line in raw_log.splitlines():
        if _CHAIN_MARKER in line:
            if current:
                sections.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current))
    return sections or [raw_log]


def parse_traceback(raw_log: str, code_dir: str) -> TracebackResult:
    """从测试日志解析精确出错坐标。

    Args:
        raw_log: 测试完整输出（stdout + stderr）
        code_dir: 项目代码目录（用于过滤项目文件）

    Returns:
        TracebackResult：new_start 为规则 3 的新起点，None 表示无项目坐标。
    """
    # 规则 1：链式异常取最后一段
    sections = split_sections(raw_log)
    last = sections[-1]

    # 规则 2：只保留项目文件
    frames = extract_frames(last)
    project_frames = []
    for f in frames:
        rel = to_project_rel(f.file, code_dir)
        if rel is None:
            continue
        if any(bad in rel for bad in ("site-packages", "dist-packages", "_pytest")):
            continue
        f.file = rel
        project_frames.append(f)

    # 规则 3：第一个项目文件作为新起点
    if not project_frames:
        return TracebackResult(new_start=None, frames=[], raw_section=last)
    return TracebackResult(
        new_start=project_frames[0],
        frames=project_frames,
        raw_section=last,
    )
