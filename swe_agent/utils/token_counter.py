"""Token 统计工具：近似计算文本的 token 数量。

使用 tiktoken 库进行近似统计。
"""

import tiktoken


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """计算文本的 token 数量。

    Args:
        text: 要计算的文本
        model: 模型名称（用于选择编码器）

    Returns:
        token 数量
    """
    try:
        # 尝试使用 cl100k_base（GPT-4 / DeepSeek 通用）
        encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        # fallback 到 gpt2 编码
        encoding = tiktoken.get_encoding("gpt2")

    return len(encoding.encode(text))


def compare_token_counts(full_source: str, skeleton: str) -> dict:
    """比较完整源码和骨架的 token 数量。

    Args:
        full_source: 完整源码
        skeleton: 骨架文本

    Returns:
        包含统计信息的字典
    """
    full_tokens = count_tokens(full_source)
    skeleton_tokens = count_tokens(skeleton)

    reduction = 1 - (skeleton_tokens / full_tokens) if full_tokens > 0 else 0

    return {
        "full_tokens": full_tokens,
        "skeleton_tokens": skeleton_tokens,
        "reduction_percent": round(reduction * 100, 2),
    }


def analyze_project_tokens(project_dir: str) -> dict:
    """分析项目所有 Python 文件的 token 统计。

    Args:
        project_dir: 项目目录

    Returns:
        包含详细统计信息的字典
    """
    import os
    from swe_agent.graph import GraphManager

    # 构建图索引（迁移：SkeletonTree → GraphIndex）
    graph_mgr = GraphManager(project_dir)
    graph_index = graph_mgr.build()

    # 生成骨架
    skeleton_text = graph_index.generate_skeleton_text()

    # 计算完整源码的 token
    full_source_parts = []
    file_stats = []

    # 按文件分组统计函数数
    file_functions: dict[str, int] = {}
    for node in graph_index.graph.nodes.values():
        if node.node_type.value == "function":
            file_functions[node.file] = file_functions.get(node.file, 0) + 1

    for file_path in sorted(file_functions.keys()):
        abs_path = os.path.join(project_dir, file_path)
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                source = f.read()

            rel_path = os.path.relpath(abs_path, project_dir)
            file_tokens = count_tokens(source)

            full_source_parts.append(source)
            file_stats.append({
                "file": rel_path,
                "tokens": file_tokens,
                "functions": file_functions[file_path],
            })
        except Exception:
            continue

    full_source = "\n\n".join(full_source_parts)
    full_tokens = count_tokens(full_source)
    skeleton_tokens = count_tokens(skeleton_text)

    return {
        "file_count": len(file_stats),
        "total_functions": sum(f["functions"] for f in file_stats),
        "full_tokens": full_tokens,
        "skeleton_tokens": skeleton_tokens,
        "reduction_percent": round(
            (1 - skeleton_tokens / full_tokens) * 100 if full_tokens > 0 else 0, 2
        ),
        "files": file_stats,
    }
