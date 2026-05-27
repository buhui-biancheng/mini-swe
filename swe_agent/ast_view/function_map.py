import ast
from typing import Optional


def get_function_line_map(filepath: str) -> dict[str, tuple[int, int]]:
    """提取 Python 文件中所有函数和方法的行号范围。

    Args:
        filepath: Python 源文件路径

    Returns:
        字典，key 为函数名（类方法格式为 ClassName.method_name），
        value 为 (start_line, end_line) 元组
    """
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()

    tree = ast.parse(source, filename=filepath)
    result: dict[str, tuple[int, int]] = {}

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            result[node.name] = (node.lineno, node.end_lineno or node.lineno)

        elif isinstance(node, ast.ClassDef):
            class_name = node.name
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    method_name = f"{class_name}.{child.name}"
                    result[method_name] = (child.lineno, child.end_lineno or child.lineno)

    return result


def get_function_source(filepath: str, func_name: str) -> Optional[str]:
    """获取指定函数的完整源码。

    Args:
        filepath: Python 源文件路径
        func_name: 函数名（类方法格式为 ClassName.method_name）

    Returns:
        函数源码字符串，未找到返回 None
    """
    line_map = get_function_line_map(filepath)
    if func_name not in line_map:
        return None

    start, end = line_map[func_name]
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()

    return "".join(lines[start - 1 : end])
