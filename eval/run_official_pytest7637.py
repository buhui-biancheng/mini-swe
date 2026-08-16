# -*- coding: utf-8 -*-
"""官方模式冒烟：pytest-dev__pytest-7637（与 Claude Code 子 agent 双跑对比）。
不设 token 硬顶（自然成本），靠 watchdog/升级干预/max_retries 终止。
后台运行 + 实时状态落盘（供外部每 5 分钟监控）。"""
import glob
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")
sys.setrecursionlimit(20000)

from swe_agent.fsm.agent_fsm import AgentFSM
from swe_agent.graph.config import AgentConfig

IID = "pytest-dev__pytest-7637"
DATA = "/home/yuanyin292/桌面/xiangmu1/eval"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"
REPO_DIR = {"pytest-dev/pytest": "pytest"}
REPO_DEPS = {"pytest-dev/pytest": ["pytest==6.2.5", "pluggy==1.0.0", "iniconfig",
                                   "packaging", "py==1.11.0", "attrs==22.2.0",
                                   "more-itertools", "wcwidth", "atomicwrites", "colorama"]}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")
BUDGET = 10_000_000  # 模糊多文件 FTP12，给足预算

STATUS = "/home/yuanyin292/桌面/xiangmu1/eval/official_pytest7637_status.json"
RESULT = "/home/yuanyin292/桌面/xiangmu1/eval/official_pytest7637_result.json"


def pytest_dev_patch(work, inst):
    pp = glob.glob(os.path.join(work, "_pytest")) + glob.glob(os.path.join(work, "src", "_pytest"))
    if pp:
        with open(os.path.join(pp[0], "_version.py"), "w", encoding="utf-8") as f:
            f.write('version = "8.3.0"\n')


def make_worktree(inst):
    work = os.path.join(WORK, IID)
    if os.path.exists(work):
        subprocess.run(["git", "worktree", "remove", "--force", work],
                       cwd=os.path.join(REPOS, REPO_DIR[inst["repo"]]),
                       capture_output=True, text=True, timeout=60)
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    r = subprocess.run(["git", "worktree", "add", "--detach", work, inst["base_commit"]],
                       cwd=os.path.join(REPOS, REPO_DIR[inst["repo"]]),
                       capture_output=True, text=True, env=PROXY_ENV, timeout=600)
    if r.returncode != 0:
        print("worktree add failed:", r.stderr[-300:], flush=True)
        return None
    return work


def main():
    inst = json.load(open(os.path.join(DATA, "pytest7637_instance.json"), encoding="utf-8"))
    fuzzy = open(os.path.join(DATA, "fuzzy_pytest7637_issue.txt"), encoding="utf-8").read().strip()
    inst["problem_statement"] = fuzzy
    work = make_worktree(inst)
    if not work:
        json.dump({"success": False, "error": "worktree failed"}, open(RESULT, "w"))
        return
    # pytest-dev 特殊：写 _version.py（setuptools_scm 构建产物）
    pytest_dev_patch(work, inst)

    first = inst["patch"].split("diff --git a/")[1].split(" ")[0]
    bug_file = os.path.join(work, first)

    src_hint = "PYTHONPATH=/workspace/src " if os.path.isdir(os.path.join(work, "src")) else "PYTHONPATH=/workspace "
    # 官方模式：推导相关测试文件（bug 文件同名 test_*.py）
    test_dir = "tests"
    for cand in ("tests", "test", "testing"):
        if os.path.isdir(os.path.join(work, cand)):
            test_dir = cand
            break
    _bug_base = os.path.basename(bug_file).replace(".py", "")
    _rel_tests = glob.glob(os.path.join(work, test_dir, "**", f"test_{_bug_base}.py"), recursive=True)
    # 7637：FTP 在 deprecated_test.py（deprecated 特性警告恢复），直接跑该文件
    test_cmd = (f"cd /workspace && {src_hint}python3 -m pytest testing/deprecated_test.py "
                f"-q -p no:cacheprovider -c /dev/null -p pytester")
    print(f"[DRIVER] bug_file={bug_file}\n[DRIVER] test_cmd={test_cmd}\n[DRIVER] budget={BUDGET}", flush=True)

    cfg = AgentConfig(thinking_enabled=True, reasoning_effort="high",
                      token_budget=BUDGET, max_tool_calls=None)
    fsm = AgentFSM(bug_file=bug_file, test_command=test_cmd, code_dir=work,
                   max_retries=8, mode="dp", no_degrade=True,
                   python_version="3.8", packages=REPO_DEPS[inst["repo"]],
                   config=cfg, graph_level=2, early_stop=False,
                   official_mode=True, problem_statement=inst["problem_statement"],
                   load_prior=False, repo_key=inst["repo"])

    def monitor():
        while True:
            try:
                tb = fsm.token_budget
                json.dump({
                    "state": fsm.state, "attempt": fsm.attempt,
                    "tool_calls": fsm.tool_call_count,
                    "edits": len(fsm._edited_ranges),
                    "verified": bool(getattr(fsm, "_verified", False)),
                    "tokens_total": tb.total,
                    "prompt_total": tb.prompt_total,
                    "completion_total": tb.completion_total,
                    "cached_total": tb.cached_total,
                    "cost_yuan": tb.estimate_cost(),
                    "ts": round(time.time(), 1),
                }, open(STATUS, "w"))
            except Exception:
                pass
            time.sleep(10)

    threading.Thread(target=monitor, daemon=True).start()

    start = time.time()
    success = fsm.run()
    dur = round(time.time() - start, 1)
    tb = fsm.token_budget
    result = {
        "instance_id": IID, "success": success, "duration_s": dur,
        "state": fsm.state, "attempt": fsm.attempt,
        "tool_calls": fsm.tool_call_count,
        "edits": len(fsm._edited_ranges),
        "verified": bool(getattr(fsm, "_verified", False)),
        "tokens_total": tb.total, "prompt_total": tb.prompt_total,
        "completion_total": tb.completion_total, "cached_total": tb.cached_total,
        "cost_yuan": tb.estimate_cost(),
        "graph_queries": getattr(fsm.graph_index, "query_count", 0),
        "verification_log": getattr(fsm, "verification_log", None),
    }
    json.dump(result, open(RESULT, "w"), ensure_ascii=False, indent=1)
    print("RESULT:", json.dumps(result, ensure_ascii=False)[:600], flush=True)


if __name__ == "__main__":
    main()
