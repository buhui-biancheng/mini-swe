# -*- coding: utf-8 -*-
"""官方模式冒烟：pallets__flask-5063（与 Claude Code 离线 run3 同实例同任务）。
预算硬卡 = Claude Code 离线用量(4,490,652) + 3,000,000 = 7,490,652 tokens。
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

IID = "pallets__flask-5063"
DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"
REPO_DIR = {"pallets/flask": "flask"}
REPO_DEPS = {"pallets/flask": ["pytest==6.2.5", "werkzeug==2.2.3", "jinja2==3.1.2",
                               "click==8.1.7", "itsdangerous==2.1.2", "MarkupSafe==2.1.3",
                               "blinker==1.7.0", "importlib_metadata==6.7.0", "python-dotenv==1.0.0"]}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")
BUDGET = 10_000_000  # 2026-08-14 优化版冒烟：flask 上限 1000 万

STATUS = "/home/yuanyin292/桌面/xiangmu1/eval/official_flask5063_status.json"
RESULT = "/home/yuanyin292/桌面/xiangmu1/eval/official_flask5063_result.json"


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
    insts = {i["instance_id"]: i for i in json.load(open(os.path.join(DATA, "swebench_subset_v2.json"), encoding="utf-8"))}
    inst = insts[IID]
    work = make_worktree(inst)
    if not work:
        json.dump({"success": False, "error": "worktree failed"}, open(RESULT, "w"))
        return
    first = inst["patch"].split("diff --git a/")[1].split(" ")[0]
    bug_file = os.path.join(work, first)

    src_hint = "PYTHONPATH=/workspace/src " if os.path.isdir(os.path.join(work, "src")) else ""
    test_dir = "tests"
    for cand in ("tests", "test", "testing"):
        if os.path.isdir(os.path.join(work, cand)):
            test_dir = cand
            break
    _bug_base = os.path.basename(bug_file).replace(".py", "")
    _rel_tests = glob.glob(os.path.join(work, test_dir, "**", f"test_{_bug_base}.py"), recursive=True)
    if _rel_tests:
        _tf = os.path.relpath(_rel_tests[0], work)
        test_cmd = f"{src_hint}python3 -m pytest {_tf} -q -p no:cacheprovider"
    else:
        test_cmd = f"{src_hint}python3 -m pytest {test_dir} -q -p no:cacheprovider"
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
