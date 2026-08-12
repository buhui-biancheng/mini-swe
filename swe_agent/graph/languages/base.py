# -*- coding: utf-8 -*-
"""多语言支持：语言无关的图构建中间态（2026-08-08 Tree-sitter 扩展）。

Python 仍走 ast 路径（稳定 + 324 测试覆盖）；其他语言经 Tree-sitter
解析成 RawFile 中间态，由 builder 转换为 _FileInfo——图结构（节点/边/
权重/索引）完全复用，只换解析前端。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RawSymbol:
    """语言无关的符号（函数/类/方法/全局）。"""
    kind: str            # "function" | "class" | "global" | "file"
    name: str            # 符号名（方法含类前缀 "Class.method"）
    lineno: int = 0
    end_lineno: int = 0
    class_name: Optional[str] = None
    methods: list = field(default_factory=list)  # 类的方法名列表


@dataclass
class RawCall:
    """语言无关的调用（callee 表达式文本，如 "foo" / "obj.method"）。"""
    callee: str          # 调用表达式（含点链）
    lineno: int = 0
    args_count: int = 0


@dataclass
class RawImport:
    """语言无关的导入绑定。"""
    kind: str            # "module" | "symbol"
    module: str          # 目标模块/文件路径（相对）
    symbol: Optional[str] = None   # symbol 导入的名字（如 from x import y / import {y}）
    local: Optional[str] = None    # 本地别名


@dataclass
class RawFile:
    """语言无关的单个文件解析结果。"""
    rel_path: str
    source: str
    symbols: list = field(default_factory=list)        # [RawSymbol]
    globals: list = field(default_factory=list)        # [RawSymbol(kind=global)]
    imports: list = field(default_factory=list)        # [RawImport]
    calls: dict = field(default_factory=dict)          # symbol_name -> [RawCall]
    error: Optional[str] = None


class SymbolProvider:
    """语言解析器基类。子类实现 parse()。"""

    def parse(self, source: str, rel_path: str) -> RawFile:
        raise NotImplementedError
