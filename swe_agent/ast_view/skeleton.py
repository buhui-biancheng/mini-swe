"""SkeletonTree: 多文件项目的 AST 骨架提取与上下文压缩。

功能：
1. 递归遍历目录，提取所有 .py 文件的函数/类签名
2. expand_function(file_path, func_name) 返回完整源码
3. LRU 缓存（最多缓存 5 个展开函数）
"""

import ast
import os
from functools import lru_cache
from dataclasses import dataclass, field


@dataclass
class FunctionInfo:
    """函数/方法信息。"""
    name: str
    file_path: str
    start_line: int
    end_line: int
    class_name: str | None = None  # 类内方法才有值

    @property
    def full_name(self) -> str:
        """完整名称，如 ClassName.method_name 或 function_name。"""
        if self.class_name:
            return f"{self.class_name}.{self.name}"
        return self.name


@dataclass
class FileSkeleton:
    """单个文件的骨架信息。"""
    file_path: str
    functions: list[FunctionInfo] = field(default_factory=list)
    error: str | None = None


class SkeletonTree:
    """多文件项目的 AST 骨架树。

    用法：
        tree = SkeletonTree("/path/to/project")
        skeleton_text = tree.generate_skeleton()
        source = tree.expand_function("src/utils.py", "helper_func")
    """

    def __init__(self, root_dir: str, max_cache_size: int = 5):
        """初始化骨架树。

        Args:
            root_dir: 项目根目录
            max_cache_size: expand_function 最大缓存数量
        """
        self.root_dir = os.path.abspath(root_dir)
        self.max_cache_size = max_cache_size
        self._file_skeletons: dict[str, FileSkeleton] = {}
        self._expand_cache: dict[str, str] = {}

    def scan(self) -> None:
        """扫描项目目录，提取所有 .py 文件的骨架。"""
        self._file_skeletons.clear()

        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            # 跳过隐藏目录和 __pycache__
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith('.') and d != '__pycache__' and d != 'venv'
            ]

            for filename in filenames:
                if filename.endswith('.py'):
                    file_path = os.path.join(dirpath, filename)
                    self._scan_file(file_path)

    def _scan_file(self, file_path: str) -> None:
        """扫描单个文件，提取函数/类签名。"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                source = f.read()
            tree = ast.parse(source)
        except (SyntaxError, UnicodeDecodeError) as e:
            self._file_skeletons[file_path] = FileSkeleton(
                file_path=file_path,
                error=str(e),
            )
            return

        functions = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                # 顶层函数
                functions.append(FunctionInfo(
                    name=node.name,
                    file_path=file_path,
                    start_line=node.lineno,
                    end_line=self._get_end_line(node),
                ))
            elif isinstance(node, ast.ClassDef):
                # 类内方法
                for method in ast.iter_child_nodes(node):
                    if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef):
                        functions.append(FunctionInfo(
                            name=method.name,
                            file_path=file_path,
                            start_line=method.lineno,
                            end_line=self._get_end_line(method),
                            class_name=node.name,
                        ))

        self._file_skeletons[file_path] = FileSkeleton(
            file_path=file_path,
            functions=functions,
        )

    def _get_end_line(self, node: ast.AST) -> int:
        """获取 AST 节点的结束行号。"""
        if hasattr(node, 'end_lineno') and node.end_lineno:
            return node.end_lineno
        # fallback: 找子节点的最大行号
        max_line = node.lineno
        for child in ast.walk(node):
            if hasattr(child, 'lineno'):
                max_line = max(max_line, child.lineno)
        return max_line

    def generate_skeleton(self) -> str:
        """生成项目骨架文本。

        返回格式：
        ```
        === src/utils.py ===
        helper_func (lines 10-25)
        ClassA.method_a (lines 30-45)
        ClassA.method_b (lines 47-60)

        === src/main.py ===
        main (lines 1-20)
        ```
        """
        if not self._file_skeletons:
            self.scan()

        parts = []
        for file_path in sorted(self._file_skeletons.keys()):
            skeleton = self._file_skeletons[file_path]
            rel_path = os.path.relpath(file_path, self.root_dir)

            if skeleton.error:
                parts.append(f"=== {rel_path} === [ERROR: {skeleton.error}]")
                continue

            if not skeleton.functions:
                continue

            lines = [f"=== {rel_path} ==="]
            for func in skeleton.functions:
                lines.append(
                    f"  {func.full_name} (lines {func.start_line}-{func.end_line})"
                )
            parts.append("\n".join(lines))

        return "\n\n".join(parts)

    def expand_function(self, file_path: str, func_name: str) -> str:
        """展开指定函数的完整源码。

        Args:
            file_path: 文件路径（可以是相对路径或绝对路径）
            func_name: 函数名（如 "helper_func" 或 "ClassA.method_a"）

        Returns:
            函数的完整源码，找不到时返回错误信息
        """
        # 解析绝对路径
        abs_path = os.path.abspath(file_path)
        if abs_path not in self._file_skeletons:
            # 尝试在 root_dir 下查找
            abs_path = os.path.join(self.root_dir, file_path)
            if not os.path.exists(abs_path):
                return f"[ERROR] 文件不存在: {file_path}"

        # 检查缓存
        cache_key = f"{abs_path}::{func_name}"
        if cache_key in self._expand_cache:
            return self._expand_cache[cache_key]

        skeleton = self._file_skeletons.get(abs_path)
        if not skeleton:
            return f"[ERROR] 文件未扫描: {file_path}"

        if skeleton.error:
            return f"[ERROR] 文件解析错误: {skeleton.error}"

        # 查找函数
        target_func = None
        for func in skeleton.functions:
            if func.full_name == func_name or func.name == func_name:
                target_func = func
                break

        if not target_func:
            available = [f.full_name for f in skeleton.functions]
            return f"[ERROR] 函数 '{func_name}' 未找到。可用函数: {available}"

        # 读取源码
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as e:
            return f"[ERROR] 读取文件失败: {e}"

        # 提取函数源码（行号从1开始）
        start = target_func.start_line - 1
        end = target_func.end_line
        source = "".join(lines[start:end])

        # 管理缓存（LRU 简易实现）
        if len(self._expand_cache) >= self.max_cache_size:
            # 移除最早插入的缓存
            oldest_key = next(iter(self._expand_cache))
            del self._expand_cache[oldest_key]

        self._expand_cache[cache_key] = source
        return source

    def search_function(self, name: str) -> list[FunctionInfo]:
        """搜索函数（支持模糊匹配）。

        Args:
            name: 函数名（或部分名称）

        Returns:
            匹配的函数列表
        """
        if not self._file_skeletons:
            self.scan()

        results = []
        name_lower = name.lower()

        for skeleton in self._file_skeletons.values():
            for func in skeleton.functions:
                if name_lower in func.full_name.lower():
                    results.append(func)

        return results

    def get_file_functions(self, file_path: str) -> list[FunctionInfo]:
        """获取指定文件的所有函数。

        Args:
            file_path: 文件路径

        Returns:
            函数列表
        """
        abs_path = os.path.abspath(file_path)
        skeleton = self._file_skeletons.get(abs_path)
        if not skeleton:
            # 尝试在 root_dir 下查找
            abs_path = os.path.join(self.root_dir, file_path)
            skeleton = self._file_skeletons.get(abs_path)

        return skeleton.functions if skeleton else []
