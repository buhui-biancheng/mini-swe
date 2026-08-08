# -*- coding: utf-8 -*-
"""容器 Oracle 验证 v2：python3.8 镜像 + PYTHONPATH=src + 兼容层。

按 SWE-bench 官方规矩：老 Python 环境跑测试，信号才可信。
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

from swe_agent.sandbox.docker_runner import run_in_docker, ensure_image

WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"

# per-repo 依赖（老代码兼容版本）
REPO_DEPS = {
    "psf/requests": ["urllib3==1.26.18", "idna==2.10", "certifi", "charset_normalizer==2.1.1", "PySocks"],
    "pallets/flask": ["werkzeug==0.16.1", "jinja2==2.11.3", "click==7.1.2", "itsdangerous==1.1.0"],
    "pylint-dev/pylint": ["astroid==2.15.8", "isort==5.12.0", "mccabe==0.7.0", "toml"],
    "pytest-dev/pytest": ["pluggy==1.0.0", "iniconfig", "packaging", "py==1.11.0", "attrs==22.2.0"],
}

# 兼容垫片（老代码在 py3.8 也可能需要；3.8 本身兼容 collections）
SHIM = (
    "import collections, collections.abc\n"
    "for _n in ('MutableMapping','Mapping','Sequence','Iterable','MutableSet'):\n"
    "    if not hasattr(collections, _n): setattr(collections, _n, getattr(collections.abc, _n))\n"
)


def container_test(work_dir, ftp_tests, repo, extra_packages=None):
    """在 py3.8 容器跑 FTP 测试。返回 exit_code。"""
    deps = REPO_DEPS.get(repo, [])
    if extra_packages:
        deps = deps + extra_packages
    image = ensure_image(python_version="3.8", packages=["pytest==6.2.5"] + deps)
    # src 布局检测
    src_hint = "PYTHONPATH=/workspace/src" if os.path.isdir(os.path.join(work_dir, "src")) else ""
    py_code = SHIM + "import sys; sys.exit(__import__('pytest').main(sys.argv[1:]))"
    cmd = (f"{src_hint} python3 -c \"{py_code}\" -q --no-header -p no:cacheprovider "
           + " ".join(f'"{t}"' for t in ftp_tests))
    r = run_in_docker(work_dir, cmd, image=image, timeout=300)
    return r


def main():
    with open(os.path.join(DATA, "swebench_subset.json"), encoding="utf-8") as f:
        instances = json.load(f)
    by_id = {i["instance_id"]: i for i in instances}

    # 先验证 requests-3362（之前 py3.12 信号失真的实例）
    iid = "psf__requests-3362"
    inst = by_id[iid]
    ftp = eval(inst["FAIL_TO_PASS"])
    print(f"[容器 Oracle] {iid} FTP={len(ftp)}")

    # 1. 不应用 gold
    r1 = container_test(os.path.join(WORK, iid), ftp[:5], inst["repo"])
    print(f"  不应用 gold: exit={r1.exit_code} (应非0) {'✅' if r1.exit_code != 0 else '❌ 失真'}")
    if r1.exit_code == 0:
        print("  输出:", r1.stdout[-200:])

    # 2. 应用 gold（在原 worktree 上，测完还原——用 git stash？直接 apply 后测，不还原也行因为 worktree 独立）
    subprocess.run(["git", "apply", "--whitespace=nowarn", "-"],
                   cwd=os.path.join(WORK, iid), input=inst["patch"],
                   capture_output=True, text=True, timeout=60)
    r2 = container_test(os.path.join(WORK, iid), ftp[:5], inst["repo"])
    print(f"  应用 gold:   exit={r2.exit_code} (应0) {'✅' if r2.exit_code == 0 else '❌'}")
    if r2.exit_code != 0:
        print("  输出:", (r2.stdout + r2.stderr)[-300:])
    # 还原（git checkout 该文件）
    subprocess.run(["git", "checkout", "--", "."], cwd=os.path.join(WORK, iid),
                   capture_output=True, text=True, timeout=60)

    ok = r1.exit_code != 0 and r2.exit_code == 0
    print(f"\nrequests-3362 容器 Oracle: {'✅ 有效' if ok else '❌ 无效'}")


if __name__ == "__main__":
    main()