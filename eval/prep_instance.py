# -*- coding: utf-8 -*-
"""prep 脚本 v2：git worktree add（共享 promisor 对象库）+ test_patch 应用。"""
import json
import os
import subprocess
import shutil

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"

REPO_DIR = {
    "pallets/flask": "flask",
    "pylint-dev/pylint": "pylint",
    "pytest-dev/pytest": "pytest",
    "psf/requests": "requests",
}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")


def run(cmd, cwd=None, timeout=300, input_text=None, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, input=input_text, env=env or PROXY_ENV)


def prepare(instance: dict) -> dict:
    iid = instance["instance_id"]
    repo = instance["repo"]
    base = instance["base_commit"]
    test_patch = instance["test_patch"]

    work_dir = os.path.join(WORK, iid)
    if os.path.exists(work_dir):
        shutil.rmtree(work_dir, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    src = os.path.join(REPOS, REPO_DIR[repo])
    # worktree add（共享对象库 + promisor，checkout 自动拉 blob）
    r = run(["git", "worktree", "add", "--detach", work_dir, base], cwd=src, timeout=600)
    if r.returncode != 0:
        return {"instance_id": iid, "ok": False, "error": f"worktree: {r.stderr[-250:]}"}

    # 应用 test_patch（官方测试）
    r = run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work_dir,
            input_text=test_patch, timeout=60)
    if r.returncode != 0:
        # 尝试 3-way 或 reverse-check 后重试
        r2 = run(["git", "apply", "--3way", "--whitespace=nowarn", "-"],
                 cwd=work_dir, input_text=test_patch, timeout=120)
        if r2.returncode != 0:
            return {"instance_id": iid, "ok": False,
                    "error": f"test_patch: {r2.stderr[-300:]}"}

    return {"instance_id": iid, "ok": True, "work_dir": work_dir}


def main():
    with open(os.path.join(DATA, "swebench_subset.json"), encoding="utf-8") as f:
        instances = json.load(f)
    results = []
    for inst in instances:
        res = prepare(inst)
        results.append(res)
        print(f"[{'OK' if res['ok'] else 'FAIL'}] {res['instance_id']} {res.get('error', '')}")
    ok = sum(1 for r in results if r["ok"])
    print(f"\n准备完成: {ok}/{len(results)}")


if __name__ == "__main__":
    main()