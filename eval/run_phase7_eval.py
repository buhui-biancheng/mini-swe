# -*- coding: utf-8 -*-
"""Phase 7 评测脚本：分层评测（层1 已定位 / 层2 全链路）+ 三组消融 + 降级开关。

设计（2026-08-05 定稿）：
    问题1（消融）：图先验有没有用 → dp-无图(禁降级) vs dp(禁降级)
    问题2（产品）：带图 vs 无图 → greedy(允许) vs dp(允许)
    评测分层：层1 = gold patch 提取文件路径（测 FSM 上界）
             层2 = problem_statement → diagnose → FSM（测全链路）

用法：
    python eval/run_phase7_eval.py --instances eval/swe_bench_subset/instances.json \
        --project-dir <项目根> --mode dp --layer 2 --no-degrade
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class EvalResult:
    instance_id: str
    mode: str
    layer: int
    success: bool
    attempts: int = 0
    drift_count: int = 0
    duration_seconds: float = 0.0
    error: str = ""


# ── 层1：从 gold patch 提取文件路径 ──
def extract_gold_files(patch: str) -> list[str]:
    """从 gold patch diff 头提取文件路径（a/ b/ 前缀去掉）。"""
    files = []
    for m in re.finditer(r"^diff --git a/(\S+) b/(\S+)", patch, re.MULTILINE):
        for f in m.groups():
            if f not in files:
                files.append(f)
    return files


def extract_test_command(instance: dict, gold_files: list[str]) -> str:
    """构造测试命令（FAIL_TO_PASS 优先，其次按测试文件）。"""
    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    if fail_to_pass:
        return "python3 -m pytest " + " ".join(fail_to_pass) + " -q"
    test_patch = instance.get("test_patch", "")
    test_files = re.findall(r"^diff --git a/(\S+test\S+\.py)", test_patch, re.MULTILINE)
    if test_files:
        return "python3 -m pytest " + " ".join(test_files) + " -q"
    return "python3 -m pytest -q"


# ── 层1：已定位修复（gold 文件路径直接喂 FSM） ──
def run_layer1(instance: dict, project_root: str, mode: str,
               no_degrade: bool) -> EvalResult:
    from swe_agent.fsm.agent_fsm import AgentFSM

    result = EvalResult(instance_id=instance["instance_id"], mode=mode,
                        layer=1, success=False)
    gold_files = extract_gold_files(instance.get("patch", ""))
    if not gold_files:
        result.error = "gold patch 无文件"
        return result
    bug_file = os.path.join(project_root, gold_files[0])
    test_cmd = extract_test_command(instance, gold_files)
    start = time.time()
    try:
        fsm = AgentFSM(bug_file=bug_file, test_command=test_cmd,
                       max_retries=2, mode=mode, no_degrade=no_degrade)
        result.success = fsm.run()
        result.attempts = fsm.attempt + 1
    except Exception as e:
        result.error = str(e)[:300]
    result.duration_seconds = round(time.time() - start, 2)
    return result


# ── 层2：全链路（diagnose → FSM） ──
def run_layer2(instance: dict, project_root: str, mode: str,
               no_degrade: bool) -> EvalResult:
    from swe_agent.diagnose.bridge import run_diagnose_fix

    result = EvalResult(instance_id=instance["instance_id"], mode=mode,
                        layer=2, success=False)
    start = time.time()
    try:
        fix = run_diagnose_fix(
            instance["problem_statement"], project_root,
            max_retries=2, mode=mode,
        )
        result.success = fix.success
        result.attempts = len(fix.attempts)
        result.drift_count = fix.drift_count
    except Exception as e:
        result.error = str(e)[:300]
    result.duration_seconds = round(time.time() - start, 2)
    return result


# ── 主入口 ──
def main():
    parser = argparse.ArgumentParser(description="Phase 7 分层评测")
    parser.add_argument("--instances", required=True, help="instances.json 路径")
    parser.add_argument("--project-dir", required=True, help="项目根目录（SWE-bench repo 已 checkout）")
    parser.add_argument("--mode", choices=["dp", "greedy", "auto"], default="dp")
    parser.add_argument("--layer", type=int, choices=[1, 2], default=1)
    parser.add_argument("--no-degrade", action="store_true", help="禁止降级（消融实验用）")
    parser.add_argument("--max-instances", type=int, default=None)
    parser.add_argument("--output", default="", help="结果输出路径")
    args = parser.parse_args()

    with open(args.instances, "r", encoding="utf-8") as f:
        instances = json.load(f)
    if args.max_instances:
        instances = instances[:args.max_instances]

    print(f"\n{'#'*60}\n# Phase 7 评测 layer={args.layer} mode={args.mode} "
          f"no_degrade={args.no_degrade}\n# 实例数: {len(instances)}\n{'#'*60}")

    results = []
    for i, inst in enumerate(instances, 1):
        print(f"\n--- [{i}/{len(instances)}] {inst.get('instance_id', '?')} ---")
        if args.layer == 1:
            r = run_layer1(inst, args.project_dir, args.mode, args.no_degrade)
        else:
            r = run_layer2(inst, args.project_dir, args.mode, args.no_degrade)
        print(f"    结果: {'✅' if r.success else '❌'} "
              f"attempts={r.attempts} drift={r.drift_count} "
              f"耗时={r.duration_seconds}s")
        if r.error:
            print(f"    错误: {r.error[:200]}")
        results.append(r)
        # 断点续跑：增量保存
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)

    success = sum(1 for r in results if r.success)
    total_time = sum(r.duration_seconds for r in results)
    print(f"\n{'='*60}")
    print(f"[EVAL] layer={args.layer} mode={args.mode} no_degrade={args.no_degrade}")
    print(f"[EVAL] 成功率: {success}/{len(results)} ({success/len(results)*100:.1f}%)")
    print(f"[EVAL] 总耗时: {total_time:.0f}s")
    print(f"[EVAL] 平均 attempts: {sum(r.attempts for r in results)/max(len(results),1):.1f}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
