# -*- coding: utf-8 -*-
"""官方模式双跑：pallets__flask-4074（4 文件跨切面 + 模糊 issue 改写）。
flask-4074 不在 20 子集，实例数据从 flask4074_instance.json 加载；
problem_statement 用模糊化改写（fuzzy_flask4074_issue.txt）。
预算：上限 1000 万 token。"""
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

IID = "pallets__flask-4074"
DATA = "/home/yuanyin292/桌面/xiangmu1/eval"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"
REPO_DIR = {"pallets/flask": "flask"}
# flask 2.0.1 时代依赖（base a541c2ac，2021-05）
REPO_DEPS = {"pallets/flask": ["pytest==6.2.5", "werkzeug==2.0.3", "jinja2==3.0.3",
                               "click==8.1.7", "itsdangerous==2.1.2", "MarkupSafe==2.1.3",
                               "blinker==1.6.2", "importlib_metadata==6.7.0", "python-dotenv==1.0.0"]}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")
BUDGET = 10_000_000

STATUS = os.path.join(DATA, "official_flask4074_status.json")
RESULT = os.path.join(DATA, "official_flask4074_result.json")


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
    inst = json.load(open(os.path.join(DATA, "flask4074_instance.json"), encoding="utf-8"))
    # 用模糊化 issue 覆盖原 problem_statement
    fuzzy = open(os.path.join(DATA, "fuzzy_flask4074_issue.txt"), encoding="utf-8").read().strip()
    inst["problem_statement"] = fuzzy
    work = make_worktree(inst)
    if not work:
        json.dump({"success": False, "error": "worktree failed"}, open(RESULT, "w"))
        return
    first = inst["patch"].split("diff --git a/")[1].split(" ")[0]
    bug_file = os.path.join(work, first)
    src_hint = "PYTHONPATH=/workspace/src " if os.path.isdir(os.path.join(work, "src")) else ""
    # 官方模式：跑仓库相关测试文件（test_blueprints.py，FTP 所在）
    test_cmd = f"{src_hint}python3 -m pytest tests/test_blueprints.py -q -p no:cacheprovider"
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
