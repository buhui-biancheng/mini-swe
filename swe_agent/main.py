import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

from swe_agent.llm.client import LLMClient
from swe_agent.ast_view.function_map import get_function_line_map
from swe_agent.tools.registry import ToolRegistry
from swe_agent.tools.schemas import TOOLS
from swe_agent.sandbox.docker_runner import run_in_docker


SYSTEM_PROMPT = """\
你是一个专业的代码修复助手。你的任务是找到并修复 Python 代码中的 bug。

工作流程：
1. 使用 search_function 搜索相关函数
2. 使用 expand_function 查看函数的完整源码
3. 分析代码，找到 bug
4. 使用 edit_function 修复 bug（指定文件路径、起始行、结束行、新代码）
5. 使用 run_test 运行测试验证修复

重要规则：
- 每次只修复一个 bug
- edit_function 的 start_line 和 end_line 必须精确对应要替换的代码行
- 新代码必须是完整的、可运行的 Python 代码
- 修复后必须运行测试验证"""


def build_skeleton(filepath: str) -> str:
    line_map = get_function_line_map(filepath)
    parts = []
    for name, (start, end) in line_map.items():
        parts.append(f"{name} (lines {start}-{end})")
    return f"{filepath}: {', '.join(parts)}"


def run_agent(
    bug_file: str,
    test_command: str,
    max_retries: int = 2,
    python_version: str = "3.11",
    packages: list[str] | None = None,
) -> bool:
    """运行 Agent 修复 bug。

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

    for attempt in range(max_retries + 1):
        print(f"\n--- 第 {attempt + 1} 次尝试 ---")

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"请修复以下文件中的 bug：\n\n文件：{bug_file}\n\n骨架：\n{skeleton}\n\n修复完成后运行测试：{test_command}"},
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

        print(f"\n[TEST] 运行测试: {test_command}")
        test_result = run_in_docker(code_dir, test_command)
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


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SWE-Agent 自动代码修复")
    parser.add_argument("bug_file", help="有 bug 的 Python 文件路径")
    parser.add_argument("test_command", help="测试命令")
    parser.add_argument("--python", default="3.11", help="Docker 容器 Python 版本 (默认: 3.11)")
    parser.add_argument("--packages", nargs="+", default=["pytest"], help="预装的包 (默认: pytest)")
    parser.add_argument("--max-retries", type=int, default=2, help="最大重试次数 (默认: 2)")

    args = parser.parse_args()

    success = run_agent(
        args.bug_file,
        args.test_command,
        max_retries=args.max_retries,
        python_version=args.python,
        packages=args.packages,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
