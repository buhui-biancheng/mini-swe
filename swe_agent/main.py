"""SWE-Agent 主入口：支持直线流程和 FSM 状态机两种模式。"""

import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

from swe_agent.llm.client import LLMClient
from swe_agent.tools.registry import ToolRegistry
from swe_agent.tools.schemas import TOOLS
from swe_agent.sandbox.docker_runner import run_in_docker
from swe_agent.utils.token_counter import analyze_project_tokens


SYSTEM_PROMPT = """\
你是一个专业的代码修复助手。你的任务是找到并修复 Python 代码中的 bug。

工作流程：
1. 使用 search_function 搜索相关函数
2. 使用 expand_function 查看函数的完整源码，或用 view_file 按行号范围精确定位代码（如查看报错行周围的代码）
3. 分析代码，找到 bug
4. 使用 edit_function 修复 bug（指定文件路径、起始行、结束行、新代码）
5. 使用 run_test 运行测试验证修复

重要规则：
- 每次只修复一个 bug
- edit_function 的 start_line 和 end_line 必须精确对应要替换的代码行
- 新代码必须是完整的、可运行的 Python 代码
- 修复后必须运行测试验证
- 文件路径使用骨架中显示的相对路径即可，系统会自动处理路径转换"""


def build_skeleton(filepath: str) -> str:
    """构建单文件骨架（兼容旧接口）。"""
    from swe_agent.ast_view.function_map import get_function_line_map
    line_map = get_function_line_map(filepath)
    parts = []
    for name, (start, end) in line_map.items():
        parts.append(f"{name} (lines {start}-{end})")
    return f"{filepath}: {', '.join(parts)}"


def run_agent_linear(
    bug_file: str,
    test_command: str,
    max_retries: int = 2,
    python_version: str = "3.11",
    packages: list[str] | None = None,
) -> bool:
    """直线流程 Agent（兼容旧接口）。

    Args:
        bug_file: 有 bug 的 Python 文件路径
        test_command: 测试命令
        max_retries: 最大重试次数
        python_version: Docker 容器的 Python 版本
        packages: 需要预装的包列表

    Returns:
        True 表示修复成功，False 表示失败
    """
    bug_file = os.path.abspath(bug_file)
    code_dir = os.path.dirname(bug_file)

    skeleton = build_skeleton(bug_file)
    print(f"\n{'='*50}")
    print(f"[INIT] 读取文件: {bug_file}")
    print(f"[INIT] 项目骨架:\n{skeleton}")
    print(f"[INIT] 测试命令: {test_command}")
    print(f"{'='*50}\n")

    registry = ToolRegistry(
        skeleton_text=skeleton,
        code_dir=code_dir,
        python_version=python_version,
        packages=packages or ["pytest"],
    )

    def tool_executor(tool_name: str, arguments: dict) -> str:
        print(f"  [TOOL] 调用 {tool_name}({json.dumps(arguments, ensure_ascii=False)})")
        result = registry.execute(tool_name, arguments)
        result_data = json.loads(result)
        if "error" in result_data:
            print(f"  [TOOL] 错误: {result_data['error']}")
        else:
            print(f"  [TOOL] 成功")
        return result

    client = LLMClient()

    # 将测试命令中的绝对路径转换为相对路径（用于显示和 Docker）
    display_command = test_command
    if code_dir in display_command:
        display_command = display_command.replace(code_dir + "/", "")
        display_command = display_command.replace(code_dir, "")

    for attempt in range(max_retries + 1):
        print(f"\n--- 第 {attempt + 1} 次尝试 ---")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请修复以下文件中的 bug：\n\n文件：{bug_file}\n\n骨架：\n{skeleton}\n\n修复完成后运行测试：{display_command}"},
        ]

        print("[LLM] 发送请求到 DeepSeek...")
        final_response, conversation = client.chat_with_tools(
            messages=messages,
            tools=TOOLS,
            tool_executor=tool_executor,
            max_rounds=10,
        )

        if final_response:
            print(f"\n[LLM] 回复: {final_response[:200]}")

        print(f"\n[TEST] 运行测试: {display_command}")
        test_result = run_in_docker(code_dir, display_command)
        print(f"[TEST] exit_code: {test_result.exit_code}")
        if test_result.stdout:
            print(f"[TEST] stdout: {test_result.stdout[:500]}")
        if test_result.stderr:
            print(f"[TEST] stderr: {test_result.stderr[:500]}")

        if test_result.exit_code == 0:
            print(f"\n{'='*50}")
            print(f"[SUCCESS] 修复成功！共尝试 {attempt + 1} 次")
            print(f"{'='*50}")
            return True

        if attempt < max_retries:
            print(f"\n[RETRY] 测试未通过，重试中...")

    print(f"\n{'='*50}")
    print(f"[FAIL] 修复失败，已用尽 {max_retries + 1} 次尝试")
    print(f"{'='*50}")
    return False


def run_agent_fsm(
    bug_file: str,
    test_command: str,
    max_retries: int = 2,
    python_version: str = "3.11",
    packages: list[str] | None = None,
    mode: str = "auto",
) -> bool:
    """FSM 状态机 Agent（新增）。

    Args:
        bug_file: 有 bug 的 Python 文件路径
        test_command: 测试命令
        max_retries: 最大重试次数
        python_version: Docker 容器的 Python 版本
        packages: 需要预装的包列表
        mode: 运行模式（dp/greedy/auto）

    Returns:
        True 表示修复成功，False 表示失败
    """
    from swe_agent.fsm.agent_fsm import AgentFSM

    fsm = AgentFSM(
        bug_file=bug_file,
        test_command=test_command,
        max_retries=max_retries,
        python_version=python_version,
        packages=packages,
        mode=mode,
    )
    return fsm.run()


def run_agent(
    bug_file: str,
    test_command: str,
    max_retries: int = 2,
    python_version: str = "3.11",
    packages: list[str] | None = None,
    use_fsm: bool = False,
    mode: str = "auto",
) -> bool:
    """运行 Agent 修复 bug（统一入口）。

    Args:
        bug_file: 有 bug 的 Python 文件路径
        test_command: 测试命令
        max_retries: 最大重试次数
        python_version: Docker 容器的 Python 版本
        packages: 需要预装的包列表
        use_fsm: 是否使用 FSM 状态机模式
        mode: 运行模式（dp/greedy/auto）

    Returns:
        True 表示修复成功，False 表示失败
    """
    if use_fsm:
        return run_agent_fsm(bug_file, test_command, max_retries, python_version, packages, mode)
    else:
        return run_agent_linear(bug_file, test_command, max_retries, python_version, packages)


def analyze_tokens(project_dir: str) -> None:
    """分析项目 token 统计。"""
    print(f"\n{'='*50}")
    print(f"[TOKEN] 分析项目: {project_dir}")
    print(f"{'='*50}")

    result = analyze_project_tokens(project_dir)

    print(f"\n文件数量: {result['file_count']}")
    print(f"函数数量: {result['total_functions']}")
    print(f"完整源码 tokens: {result['full_tokens']}")
    print(f"骨架 tokens: {result['skeleton_tokens']}")
    print(f"压缩率: {result['reduction_percent']}%")

    print(f"\n文件详情:")
    for f in result['files']:
        print(f"  - {f['file']}: {f['tokens']} tokens, {f['functions']} 个函数")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SWE-Agent 自动代码修复")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # fix 命令
    fix_parser = subparsers.add_parser("fix", help="修复 bug")
    fix_parser.add_argument("bug_file", help="有 bug 的 Python 文件路径")
    fix_parser.add_argument("test_command", help="测试命令")
    fix_parser.add_argument("--python", default="3.11", help="Docker 容器 Python 版本 (默认: 3.11)")
    fix_parser.add_argument("--packages", nargs="+", default=["pytest"], help="预装的包 (默认: pytest)")
    fix_parser.add_argument("--max-retries", type=int, default=2, help="最大重试次数 (默认: 2)")
    fix_parser.add_argument("--fsm", action="store_true", help="使用 FSM 状态机模式")
    fix_parser.add_argument("--mode", choices=["dp", "greedy", "auto"], default=None,
                            help="运行模式：dp=图索引引导 / greedy=无图探索 / auto=自动。指定 --mode 时自动启用 FSM")

    # graph 命令（Phase 1 新增）
    graph_parser = subparsers.add_parser("graph", help="加权图索引操作")
    graph_sub = graph_parser.add_subparsers(dest="graph_command", help="图操作")

    g_build = graph_sub.add_parser("build", help="构建/重建图索引")
    g_build.add_argument("project_dir", help="项目目录")
    g_build.add_argument("--force", action="store_true", help="强制全量重建（忽略缓存）")

    g_stats = graph_sub.add_parser("stats", help="图统计（节点/边/高入度）")
    g_stats.add_argument("project_dir", help="项目目录")

    g_viz = graph_sub.add_parser("viz", help="导出图可视化（mermaid/dot）")
    g_viz.add_argument("project_dir", help="项目目录")
    g_viz.add_argument("--format", choices=["mermaid", "dot"], default="mermaid",
                       help="导出格式（默认: mermaid）")
    g_viz.add_argument("--output", default="", help="输出文件路径（默认打印到终端）")

    # analyze 命令
    analyze_parser = subparsers.add_parser("analyze", help="分析项目 token 统计")
    analyze_parser.add_argument("project_dir", help="项目目录")

    # tui 命令
    tui_parser = subparsers.add_parser("tui", help="启动 TUI 终端界面")

    args = parser.parse_args()

    if args.command == "fix":
        mode = args.mode or "auto"
        # 指定 --mode 时自动启用 FSM（A/B 对比实验用）
        use_fsm = args.fsm or args.mode is not None
        success = run_agent(
            args.bug_file,
            args.test_command,
            max_retries=args.max_retries,
            python_version=args.python,
            packages=args.packages,
            use_fsm=use_fsm,
            mode=mode,
        )
        sys.exit(0 if success else 1)
    elif args.command == "graph":
        from swe_agent.cli import graph_build, graph_stats, graph_viz
        if args.graph_command == "build":
            graph_build(args.project_dir, force=args.force)
        elif args.graph_command == "stats":
            graph_stats(args.project_dir)
        elif args.graph_command == "viz":
            graph_viz(args.project_dir, format=args.format, output=args.output)
        else:
            graph_parser.print_help()
    elif args.command == "analyze":
        analyze_tokens(args.project_dir)
    elif args.command == "tui":
        from swe_agent.tui.app import run_tui
        run_tui()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
