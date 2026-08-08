# -*- coding: utf-8 -*-
"""oracle v3：每实例 worktree 重建（干净）+ 修正依赖。"""
import json
import os
import subprocess
import sys
import shutil

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

from swe_agent.sandbox.docker_runner import run_in_docker, ensure_image

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"

REPO_DIR = {"pallets/flask": "flask", "pylint-dev/pylint": "pylint",
            "pytest-dev/pytest": "pytest", "psf/requests": "requests"}
PROXY_ENV = dict(os.environ, HTTPS_PROXY="http://10.0.2.2:7897", HTTP_PROXY="http://10.0.2.2:7897")

# 修正后的依赖（按 base_commit 时代）
REPO_DEPS = {
    "psf/requests": ["urllib3==1.26.18", "idna==2.10", "certifi", "charset_normalizer==2.1.1", "PySocks"],
    "pallets/flask": ["werkzeug==2.2.3", "jinja2==3.1.2", "click==8.1.7", "itsdangerous==2.1.2", "MarkupSafe==2.1.3"],
    "pylint-dev/pylint": ["astroid==2.15.8", "isort==5.12.0", "mccabe==0.7.0", "toml"],
    "pytest-dev/pytest": ["pluggy==1.0.0", "iniconfig", "packaging", "py==1.11.0", "attrs==22.2.0"],
}
SHIM = ("import collections, collections.abc\n"
        "for _n in ('MutableMapping','Mapping','Sequence','Iterable','MutableSet'):\n"
        "    if not hasattr(collections, _n): setattr(collections, _n, getattr(collections.abc, _n))\n")
IMAGE_CACHE = {}

def get_image(repo):
    if repo not in IMAGE_CACHE:
        deps = REPO_DEPS.get(repo, [])
        IMAGE_CACHE[repo] = ensure_image(python_version="3.8",
                                         packages=["pytest==6.2.5"] + deps)
    return IMAGE_CACHE[repo]

def container_test(work_dir, tests, repo):
    image = get_image(repo)
    src_hint = "PYTHONPATH=/workspace/src" if os.path.isdir(os.path.join(work_dir, "src")) else ""
    py_code = SHIM + "import sys; sys.exit(__import__('pytest').main(sys.argv[1:]))"
    cmd = (f"{src_hint} python3 -c \"{py_code}\" -q --no-header -p no:cacheprovider "
           + " ".join(f'"{t}"' for t in tests))
    return run_in_docker(work_dir, cmd, image=image, timeout=300, network=True)

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
        return None, f"worktree: {r.stderr[-200:]}"
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                       input=inst["test_patch"], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return None, f"test_patch: {r.stderr[-200:]}"
    return work, None

def oracle(inst):
    work, err = make_worktree(inst)
    if work is None:
        return {"instance_id": inst["instance_id"], "ok": False, "error": err}
    ftp = eval(inst["FAIL_TO_PASS"])
    if not ftp:
        return {"instance_id": inst["instance_id"], "ok": False, "error": "FTP 空"}
    r1 = container_test(work, ftp[:6], inst["repo"])
    fail_without = r1.exit_code != 0
    subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                   input=inst["patch"], capture_output=True, text=True, timeout=60)
    r2 = container_test(work, ftp[:6], inst["repo"])
    pass_with = r2.exit_code == 0
    # 清理 worktree（下个实例重建）
    subprocess.run(["git", "worktree", "remove", "--force", work],
                   cwd=os.path.join(REPOS, REPO_DIR[inst["repo"]]),
                   capture_output=True, text=True, timeout=60)
    ok = fail_without and pass_with
    return {"instance_id": inst["instance_id"], "ok": ok,
            "fail_without": fail_without, "pass_with": pass_with,
            "detail": f"无gold={r1.exit_code} 有gold={r2.exit_code}"}

def main():
    with open(os.path.join(DATA, "swebench_subset.json"), encoding="utf-8") as f:
        instances = json.load(f)
    results = []
    for inst in instances:
        print(f"[Oracle] {inst['instance_id']} ...", flush=True)
        try:
            res = oracle(inst)
        except Exception as e:
            res = {"instance_id": inst["instance_id"], "ok": False, "error": str(e)[-150:]}
        results.append(res)
        print(f"  {'✅ 有效' if res['ok'] else '❌'} {res.get('detail', res.get('error', ''))}", flush=True)
    ok_ids = [r["instance_id"] for r in results if r["ok"]]
    print(f"\n有效实例: {len(ok_ids)}/20")
    for i in ok_ids:
        print(" ", i)
    with open(os.path.join(DATA, "oracle_valid.json"), "w", encoding="utf-8") as f:
        json.dump(ok_ids, f, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()