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
from swe_agent.sandbox.docker_runner import run_in_docker
from swe_agent.graph.config import AgentConfig

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"

REPO_DIR = {"pallets/flask": "flask", "pylint-dev/pylint": "pylint",
            "pytest-dev/pytest": "pytest", "psf/requests": "requests"}
REPO_DEPS = {
    "psf/requests": ["pytest==6.2.5", "urllib3==1.26.18", "idna==2.10", "certifi", "charset_normalizer==2.1.1", "PySocks"],
    "pallets/flask": ["pytest==6.2.5", "werkzeug==2.2.3", "jinja2==3.1.2", "click==8.1.7", "itsdangerous==2.1.2", "MarkupSafe==2.1.3", "blinker==1.7.0", "importlib_metadata==6.7.0"],
    "pylint-dev/pylint": ["pytest==6.2.5", "astroid==2.15.8", "isort==5.12.0", "mccabe==0.7.0", "toml"],
    "pytest-dev/pytest": ["pytest==6.2.5", "pluggy==1.0.0", "iniconfig", "packaging", "py==1.11.0", "attrs==22.2.0"],
}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")


def pytest_dev_patch(work, inst):
    """pytest-dev 配方（2026-08-08 验证）：手写 _version.py（setuptools_scm 构建产物）。"""
    import glob
    pp = glob.glob(os.path.join(work, "_pytest")) + glob.glob(os.path.join(work, "src", "_pytest"))
    if pp:
        with open(os.path.join(pp[0], "_version.py"), "w", encoding="utf-8") as f:
            f.write('version = "8.3.0"\n')


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
    # 2026-08-13 官方模式（OFFICIAL=1）：不应用 test_patch（官方隐藏测试防泄漏），
    # 自带测试保留（agent 可跑自带测试——真实工程师工作流）
    if os.environ.get("OFFICIAL") != "1":
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
    # pytest-dev 特殊：源码版自举（-c /dev/null 绕 tox.ini，-p pytester 加载内置插件）
    if repo == "pytest-dev/pytest":
        pytest_dev_patch(work, inst)
        src_hint = ("PYTHONPATH=/workspace/src " if os.path.isdir(os.path.join(work, "src"))
                    else "PYTHONPATH=/workspace ")
        test_cmd = (f"cd /workspace && {src_hint}python3 -m pytest "
                    + " ".join(f'"{t}"' for t in ftp[:6])
                    + " -q -p no:cacheprovider -c /dev/null -p pytester")
    if os.environ.get("OFFICIAL") == "1":
        # 官方模式：跑相关自带测试（仓库 tests/ 目录）——agent 可跑自带测试
        test_dir = "tests"
        for cand in ("tests", "test"):
            if os.path.isdir(os.path.join(work, cand)):
                test_dir = cand
                break
        test_cmd = f"{src_hint}python3 -m pytest {test_dir} -q -p no:cacheprovider"
    else:
        test_cmd = f"{src_hint}python3 -m pytest " + " ".join(f'"{t}"' for t in ftp[:6]) + " -q -p no:cacheprovider"
    cfg = AgentConfig(thinking_enabled=True, reasoning_effort="high",
                      token_budget=None)  # 用户定稿：评测不设上限（防失控靠 max_retries）
    fsm = AgentFSM(bug_file=bug_file, test_command=test_cmd,
                   code_dir=work,  # 2026-08-08：容器挂载项目根（bug 在子目录也能跑根目录测试）
                   max_retries=8,  # 2026-08-08 用户预案：难档关闭早停 + 预算加大
                   mode=mode, no_degrade=no_degrade,
                   python_version="3.8", packages=REPO_DEPS[repo],
                   config=cfg, graph_level=graph_level,
                   early_stop=False,  # 2026-08-08 用户预案：评测关早停（保成功率），产品模式可开
                   official_mode=os.environ.get("OFFICIAL") == "1")
    start = time.time()
    success = fsm.run()
    dur = round(time.time() - start, 1)
    # 效率指标（用户定稿：主指标 = 效率）：
    # token 消耗 = token_budget.total（整组累计），工具调用数 = tool_call_count
    token_total = getattr(fsm.token_budget, "total", 0)
    tool_calls = getattr(fsm, "tool_call_count", 0)
    # 成本估算（元）：DeepSeek 定价——输入未命中 1 元/百万、缓存命中 0.02、输出 2
    cost_yuan = 0.0
    cost_detail = {}
    try:
        tb = fsm.token_budget
        cost_yuan = tb.estimate_cost()
        cost_detail = {"prompt": tb.prompt_total, "completion": tb.completion_total,
                       "cached": tb.cached_total}
    except Exception:
        pass
    # 官方模式（OFFICIAL=1）：agent 自评成功后，评测阶段应用 test_patch → FTP 真实判定
    official = os.environ.get("OFFICIAL") == "1"
    if official and success:
        subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                       input=inst["test_patch"], capture_output=True, text=True, timeout=60)
        ftp_cmd = (f"{src_hint}python3 -m pytest " + " ".join(f'"{t}"' for t in ftp[:6])
                   + " -q -p no:cacheprovider")
        rf = run_in_docker(work, ftp_cmd, python_version="3.8",
                           packages=REPO_DEPS[repo], timeout=900)
        success = rf.exit_code == 0
        ftp_detail = (rf.stdout or "")[-200:] if not success else None

    # PASS_TO_PASS 回归验证（官方 harness 口径，2026-08-08）：
    # FSM 成功（FTP 过）后，再跑 P2P——全过才算官方 resolve；有挂 = 引入回归
    p2p_pass = None
    p2p_detail = ""
    if success:
        p2p = eval(inst["PASS_TO_PASS"])
        if p2p:
            # 2026-08-08 完善：文件级跑 + -v 按节点判定——
            # 逐节点会被参数化 node ID 的 :: 拆断（IPv6 URL）；文件级跑则含环境噪声测试
            # （外网/版本差异）。正确做法：文件级跑，只检查 P2P 节点是否 PASSED。
            import re as _re
            p2p_files = sorted({x.split("::")[0] for x in p2p if x.split("::")[0].endswith(".py")})
            if not p2p_files:
                p2p_files = [x.split("::")[0] for x in p2p][:5]
            cmd2 = (f"{src_hint}python3 -m pytest " + " ".join(f'"{f}"' for f in p2p_files)
                    + " -v --no-header -p no:cacheprovider")
            r2 = run_in_docker(work, cmd2, python_version="3.8",
                               packages=REPO_DEPS[repo], timeout=900)
            # 解析 -v 输出：PASSED 的节点集合（路径相对 cwd=/workspace，与 P2P 列表一致）
            passed_nodes = set()
            for line in (r2.stdout or "").splitlines():
                # 行尾状态词（rsplit 比正则稳：参数化节点名可含任意字符）
                ls = line.strip()
                if ls.endswith(" PASSED"):
                    passed_nodes.add(ls[: -len(" PASSED")])
            # P2P 节点全部 PASSED = 无回归；环境噪声节点失败不影响判定
            p2p_pass = all(n in passed_nodes for n in p2p)
            if not p2p_pass:
                missing = [n for n in p2p if n not in passed_nodes]
                p2p_detail = f"P2P 未通过节点: {missing[:5]}"
    return {"success": success, "attempts": fsm.attempt + 1,
            "duration": dur, "mode": mode, "no_degrade": no_degrade,
            "token_total": token_total, "tool_calls": tool_calls,
            "graph_level": graph_level, "p2p_pass": p2p_pass,
            "official_ftp": locals().get("ftp_detail"),
            # 2026-08-13 轨迹落盘：上下文长度（最终 messages 字符数）+ 验证记录
            "context_chars": sum(len(str(m.get("content", ""))) for m in fsm.messages
                                 if m.get("content")),
            "verification_log": getattr(fsm, "verification_log", None),
            "p2p_detail": p2p_detail,
            "cost_yuan": cost_yuan, "cost_detail": cost_detail}


def main():
    with open(os.path.join(DATA, "oracle_valid.json"), encoding="utf-8") as f:
        valid = json.load(f)
    with open(os.path.join(DATA, "swebench_subset_v2.json"), encoding="utf-8") as f:
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
        # level=0 纯贪心 / level=1 只有细准 / level=2 完整；EVAL_LEVELS 环境变量可指定
        levels_env = os.environ.get("EVAL_LEVELS", "0,1,2")
        _levels = [int(x) for x in levels_env.split(",") if x.strip() in ("0", "1", "2")]
        for mode, nd, lv in [("dp", True, l) for l in _levels]:
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