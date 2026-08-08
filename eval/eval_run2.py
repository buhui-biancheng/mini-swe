# -*- coding: utf-8 -*-
"""评测驱动 v2：有效实例 × 三组消融 × 层1（per-repo 环境注入 FSM）。"""
import json
import os
import subprocess
import sys
import time
import shutil

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

from swe_agent.fsm.agent_fsm import AgentFSM
from swe_agent.graph.config import AgentConfig

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"

REPO_DIR = {"pallets/flask": "flask", "pylint-dev/pylint": "pylint",
            "pytest-dev/pytest": "pytest", "psf/requests": "requests"}
REPO_DEPS = {
    "psf/requests": ["pytest==6.2.5", "urllib3==1.26.18", "idna==2.10", "certifi", "charset_normalizer==2.1.1", "PySocks"],
    "pallets/flask": ["pytest==6.2.5", "werkzeug==2.2.3", "jinja2==3.1.2", "click==8.1.7", "itsdangerous==2.1.2", "MarkupSafe==2.1.3"],
    "pylint-dev/pylint": ["pytest==6.2.5", "astroid==2.15.8", "isort==5.12.0", "mccabe==0.7.0", "toml"],
    "pytest-dev/pytest": ["pytest==6.2.5", "pluggy==1.0.0", "iniconfig", "packaging", "py==1.11.0", "attrs==22.2.0"],
}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")


def make_worktree(inst):
    iid = inst["instance_id"]
    work = os.path.join(WORK, iid)
    if os.path.exists(work):
        subprocess.run(["git", "worktree", "remove", "--force", work],
                       cwd=os.path.join(REPOS, REPO_DIR[inst["repo"]]),
                       capture_output=True, text=True, timeout=60)
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    src = os.path.join(REPOS, REPO_DIR[inst["repo"]])
    r = subprocess.run(["git", "worktree", "add", "--detach", work, inst["base_commit"]],
                       cwd=src, capture_output=True, text=True, env=PROXY_ENV, timeout=600)
    if r.returncode != 0:
        return None
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                       input=inst["test_patch"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None
    return work


def run_one(inst, work, mode, no_degrade, graph_level=2):
    repo = inst["repo"]
    gold_files = [f for f in inst["patch"].split("diff --git a/")[1:]]
    first = gold_files[0].split(" ")[0]
    bug_file = os.path.join(work, first)
    ftp = eval(inst["FAIL_TO_PASS"])
    src_hint = "PYTHONPATH=/workspace/src " if os.path.isdir(os.path.join(work, "src")) else ""
    # 2026-08-08：-p no:cacheprovider 必须加——容器只读挂载，pytest cache 写失败
    # 会导致 exit 1（即使测试全过）= 假失败（flask-4045 三组全挂的根因）
    test_cmd = f"{src_hint}python3 -m pytest " + " ".join(f'"{t}"' for t in ftp[:6]) + " -q -p no:cacheprovider"
    cfg = AgentConfig(thinking_enabled=True, reasoning_effort="high",
                      token_budget=None)  # 用户定稿：评测不设上限（防失控靠 max_retries）
    fsm = AgentFSM(bug_file=bug_file, test_command=test_cmd,
                   code_dir=work,  # 2026-08-08：容器挂载项目根（bug 在子目录也能跑根目录测试）
                   max_retries=2, mode=mode, no_degrade=no_degrade,
                   python_version="3.8", packages=REPO_DEPS[repo],
                   config=cfg, graph_level=graph_level)
    start = time.time()
    success = fsm.run()
    dur = round(time.time() - start, 1)
    # 效率指标（用户定稿：主指标 = 效率）：
    # token 消耗 = token_budget.total（整组累计），工具调用数 = tool_call_count
    token_total = getattr(fsm.token_budget, "total", 0)
    tool_calls = getattr(fsm, "tool_call_count", 0)
    return {"success": success, "attempts": fsm.attempt + 1,
            "duration": dur, "mode": mode, "no_degrade": no_degrade,
            "token_total": token_total, "tool_calls": tool_calls,
            "graph_level": graph_level}


def main():
    with open(os.path.join(DATA, "oracle_valid.json"), encoding="utf-8") as f:
        valid = json.load(f)
    with open(os.path.join(DATA, "swebench_subset.json"), encoding="utf-8") as f:
        instances = {i["instance_id"]: i for i in json.load(f)}

    results = []
    for iid in valid:
        inst = instances[iid]
        work = make_worktree(inst)
        if not work:
            print(f"[FAIL] {iid} worktree 重建失败")
            continue
        print(f"\n===== {iid} =====", flush=True)
        # 评测定稿三组（2026-08-08 用户定稿：显微镜粗准/细准消融）
        # level=0 纯贪心（无任何图信息）/ level=1 只有细准（L1 函数级，无文件级先验）
        # / level=2 完整（粗准 L-1+L0+骨架 + 细准 L1+影响面标注）
        # 三组同框架（dp、禁降级），唯一变量 = 图信息层级
        for mode, nd, lv in [("dp", True, 0), ("dp", True, 1), ("dp", True, 2)]:
            label = f"{mode}{'-无图' if nd and mode=='dp' else ''}{'(允许降级)' if not nd and mode=='dp' else ''}"
            print(f"--- {label} ---", flush=True)
            try:
                r = run_one(inst, work, mode, nd, lv)
                print(f"  结果: {'✅ 成功' if r['success'] else '❌ 失败'} 尝试={r['attempts']} 耗时={r['duration']}s", flush=True)
                r["instance_id"] = iid
                results.append(r)
            except Exception as e:
                print(f"  异常: {str(e)[-200:]}", flush=True)
                results.append({"instance_id": iid, "mode": mode, "no_degrade": nd,
                                "success": False, "error": str(e)[-200:]})
        # 清理
        subprocess.run(["git", "worktree", "remove", "--force", work],
                       cwd=os.path.join(REPOS, REPO_DIR[inst["repo"]]),
                       capture_output=True, text=True, timeout=60)

    with open(os.path.join(DATA, "eval_layer1_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("\n结果已保存 eval_layer1_results.json")
    for r in results:
        print(f"  {r['instance_id']} {r['mode']} nd={r['no_degrade']} {'✅' if r['success'] else '❌'} {r.get('duration', '')}s")


if __name__ == "__main__":
    main()