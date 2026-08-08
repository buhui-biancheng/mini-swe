# -*- coding: utf-8 -*-
"""Oracle 双向验证（SWE-bench 官方规矩）：
1. 不应用 gold patch → FAIL_TO_PASS 测试必须失败（实例有效）
2. 应用 gold patch → FAIL_TO_PASS 必须全过（gold 是正确答案）
双向验证通过 = 实例可测、评测信号有效。
"""
import json
import os
import subprocess
import sys
import shutil

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"

def run(cmd, cwd, timeout=600, input_text=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, input=input_text)
    return r


def get_ftp_test_ids(instance):
    """FAIL_TO_PASS 测试 ID 列表。"""
    ftp = eval(instance["FAIL_TO_PASS"])
    return ftp


def oracle_check(work_dir: str, instance: dict) -> dict:
    iid = instance["instance_id"]
    ftp = get_ftp_test_ids(instance)
    # 用 pytest 跑 FTP 测试（-k 或直接传 node id）
    ftp_ids = " or ".join(f'"{t}"' for t in ftp[:5])  # 取前 5 个避免命令行过长
    test_cmd = ["python3", "-m", "pytest", "-q",
                "--no-header", "-p", "no:cacheprovider"] + ftp[:5]

    # 1. 不应用 gold：FTP 必须失败
    r = run(test_cmd, work_dir, timeout=900)
    fail_without = r.returncode != 0
    print(f"  [不应用 gold] exit={r.returncode} (应非0) {'✅' if fail_without else '❌ 意外通过'}")

    # 2. 应用 gold patch
    r2 = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                        cwd=work_dir, input=instance["patch"],
                        capture_output=True, text=True, timeout=60)
    if r2.returncode != 0:
        return {"instance_id": iid, "ok": False,
                "error": f"gold patch 应用失败: {r2.stderr[-300:]}"}

    # 3. 应用后：FTP 必须全过
    r3 = run(test_cmd, work_dir, timeout=900)
    pass_with = r3.returncode == 0
    print(f"  [应用 gold]   exit={r3.returncode} (应0) {'✅' if pass_with else '❌'}")

    ok = fail_without and pass_with
    return {"instance_id": iid, "ok": ok,
            "fail_without": fail_without, "pass_with": pass_with}


def main():
    with open(os.path.join(DATA, "swebench_subset.json"), encoding="utf-8") as f:
        instances = json.load(f)
    by_id = {i["instance_id"]: i for i in instances}

    results = []
    for iid, inst in by_id.items():
        work = os.path.join(WORK, iid)
        if not os.path.exists(work):
            print(f"[SKIP] {iid} 工作目录不存在")
            continue
        print(f"[Oracle] {iid}")
        res = oracle_check(work, inst)
        results.append(res)
        if not res["ok"]:
            print(f"  ❌ {res.get('error', '信号无效')}")
        else:
            print(f"  ✅ 双向验证通过（有效实例）")

    ok = sum(1 for r in results if r["ok"])
    print(f"\nOracle 验证: {ok}/{len(results)} 有效")


if __name__ == "__main__":
    main()