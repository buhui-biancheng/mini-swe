# -*- coding: utf-8 -*-
"""评测结果分析：三组消融对比（成功率/耗时/token/步数/工具调用）。"""
import json
import os
import sys
from collections import defaultdict

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
RESULT = os.path.join(DATA, "eval_layer1_results.json")


def main():
    if not os.path.exists(RESULT):
        print("结果文件不存在（评测未完成）")
        return
    with open(RESULT, encoding="utf-8") as f:
        results = json.load(f)
    if not results:
        print("结果为空")
        return

    print("=" * 70)
    print("SWE-bench 层1 评测结果分析")
    print("=" * 70)

    # 按 (mode, graph_enabled) 分组
    groups = defaultdict(list)
    for r in results:
        key = f"{r['mode']}{'-无图' if not r.get('graph_enabled', True) else ''}"
        groups[key].append(r)

    for gname, items in groups.items():
        succ = sum(1 for r in items if r["success"])
        avg_dur = sum(r.get("duration", 0) for r in items) / len(items)
        avg_tok = sum(r.get("token_total", 0) for r in items) / len(items)
        avg_att = sum(r.get("attempts", 0) for r in items) / len(items)
        avg_tc = sum(r.get("tool_calls", 0) for r in items) / len(items)
        print(f"\n[{gname}] n={len(items)}")
        print(f"  成功率: {succ}/{len(items)}")
        print(f"  平均耗时: {avg_dur:.1f}s")
        print(f"  平均 token: {avg_tok:,.0f}" + ("（本次无 token 数据，下次评测有）" if avg_tok == 0 else ""))
        print(f"  平均尝试: {avg_att:.1f} | 平均工具调用: {avg_tc:.1f}")

    # 每实例明细
    print("\n" + "=" * 70)
    print("明细（每实例每组）")
    for r in results:
        g = "有图" if r.get("graph_enabled", True) else "无图"
        print(f"  {r['instance_id'][:30]} {r['mode']}({g}) "
              f"{'✅' if r['success'] else '❌'} {r.get('duration', 0):.0f}s "
              f"tok={r.get('token_total', 0):,} att={r.get('attempts', 0)} tc={r.get('tool_calls', 0)}")


if __name__ == "__main__":
    main()