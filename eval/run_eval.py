"""批量评测脚本：在 SWE-bench-lite 子集上运行 Agent。"""

import os
import sys
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Any

# 添加项目根目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from swe_agent.fsm.agent_fsm import AgentFSM


@dataclass
class EvalResult:
    """单个评测实例的结果。"""
    instance_id: str
    status: str  # "success" / "fail" / "error"
    attempts: int = 0
    tokens_used: int = 0
    duration_seconds: float = 0.0
    error_message: str = ""
    state_transitions: list[str] = field(default_factory=list)


def load_swe_bench_subset(subset_dir: str) -> list[dict[str, Any]]:
    """加载 SWE-bench-lite 子集数据。

    Args:
        subset_dir: 子集数据目录

    Returns:
        实例列表
    """
    instances = []
    subset_path = os.path.join(subset_dir, "instances.json")

    if os.path.exists(subset_path):
        with open(subset_path, "r", encoding="utf-8") as f:
            instances = json.load(f)
    else:
        # 创建示例数据
        instances = [
            {
                "instance_id": "example_1",
                "repo": "example/project",
                "problem_statement": "add 函数返回 a - b，应该是 a + b",
                "file_path": "examples/bug_return_value.py",
                "test_command": "pytest examples/test_return_value.py -v",
            },
            {
                "instance_id": "example_2",
                "repo": "example/project",
                "problem_statement": "greet 函数使用了未定义变量 msg",
                "file_path": "examples/bug_undefined_var.py",
                "test_command": "pytest examples/test_undefined_var.py -v",
            },
        ]
        with open(subset_path, "w", encoding="utf-8") as f:
            json.dump(instances, f, ensure_ascii=False, indent=2)

    return instances


def run_single_eval(instance: dict[str, Any], timeout: int = 120) -> EvalResult:
    """运行单个评测实例。

    Args:
        instance: 实例数据
        timeout: 超时时间（秒）

    Returns:
        评测结果
    """
    instance_id = instance["instance_id"]
    file_path = instance["file_path"]
    test_command = instance["test_command"]

    print(f"\n{'='*60}")
    print(f"[EVAL] 开始评测: {instance_id}")
    print(f"[EVAL] 文件: {file_path}")
    print(f"[EVAL] 测试: {test_command}")
    print(f"{'='*60}")

    start_time = time.time()

    try:
        # 获取绝对路径
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        abs_file_path = os.path.join(project_root, file_path)

        # 运行 Agent
        agent = AgentFSM(
            bug_file=abs_file_path,
            test_command=test_command,
            max_retries=2,
        )

        success = agent.run()
        duration = time.time() - start_time

        return EvalResult(
            instance_id=instance_id,
            status="success" if success else "fail",
            attempts=agent.attempt + 1,
            duration_seconds=round(duration, 2),
        )

    except Exception as e:
        duration = time.time() - start_time
        return EvalResult(
            instance_id=instance_id,
            status="error",
            duration_seconds=round(duration, 2),
            error_message=str(e),
        )


def run_evaluation(
    subset_dir: str,
    output_dir: str,
    max_instances: int | None = None,
) -> dict[str, Any]:
    """运行批量评测。

    Args:
        subset_dir: 子集数据目录
        output_dir: 结果输出目录
        max_instances: 最大评测实例数（None 表示全部）

    Returns:
        汇总结果
    """
    instances = load_swe_bench_subset(subset_dir)
    if max_instances:
        instances = instances[:max_instances]

    print(f"\n{'#'*60}")
    print(f"# SWE-bench-lite 评测")
    print(f"# 实例数量: {len(instances)}")
    print(f"{'#'*60}")

    results = []
    success_count = 0
    fail_count = 0
    error_count = 0
    total_tokens = 0

    for i, instance in enumerate(instances, 1):
        print(f"\n--- 进度: {i}/{len(instances)} ---")
        result = run_single_eval(instance)
        results.append(result)

        if result.status == "success":
            success_count += 1
        elif result.status == "fail":
            fail_count += 1
        else:
            error_count += 1

        total_tokens += result.tokens_used

    # 汇总结果
    summary = {
        "total_instances": len(instances),
        "success": success_count,
        "fail": fail_count,
        "error": error_count,
        "success_rate": round(success_count / len(instances) * 100, 2) if instances else 0,
        "total_tokens": total_tokens,
        "results": [asdict(r) for r in results],
    }

    # 保存结果
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"eval_result_{int(time.time())}.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"[EVAL] 评测完成")
    print(f"[EVAL] 成功: {success_count}/{len(instances)} ({summary['success_rate']}%)")
    print(f"[EVAL] 失败: {fail_count}")
    print(f"[EVAL] 错误: {error_count}")
    print(f"[EVAL] 结果已保存: {output_path}")
    print(f"{'='*60}")

    return summary


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SWE-bench-lite 批量评测")
    parser.add_argument("--subset-dir", default=os.path.join(os.path.dirname(__file__), "swe_bench_subset"),
                        help="子集数据目录")
    parser.add_argument("--output-dir", default=os.path.join(os.path.dirname(__file__), "results"),
                        help="结果输出目录")
    parser.add_argument("--max-instances", type=int, default=None,
                        help="最大评测实例数")

    args = parser.parse_args()
    run_evaluation(args.subset_dir, args.output_dir, args.max_instances)


if __name__ == "__main__":
    main()
