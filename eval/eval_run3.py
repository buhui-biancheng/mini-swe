# -*- coding: utf-8 -*-
"""官方 SWE-bench 模式评测（2026-08-13 对齐官方）：
非交互：模型只见 issue + base_commit 仓库（无 test_patch）
→ 单轮产出 patch → 评测阶段应用 model_patch + test_patch → FTP/P2P 判定。
与 eval_run2（测试可见交互变体）区别：eval_run3 无测试反馈（官方标准）。
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")
sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1/eval")

from swe_agent.llm.client import LLMClient
from swe_agent.sandbox.docker_runner import run_in_docker

DATA = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_data"
WORK = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_work"
REPOS = "/home/yuanyin292/桌面/xiangmu1/eval/swe_bench_repos"

REPO_DEPS = {
    "pallets/flask": ["werkzeug==2.2.3", "click==8.1.3", "itsdangerous==2.1.2",
                      "jinja2==3.1.2", "blinker==1.7.0", "importlib_metadata==6.7.0"],
    "psf/requests": ["requests==2.7.0", "urllib3==1.25.11", "chardet==4.0.0",
                     "idna==3.4", "certifi", "six==1.16.0", "PySocks==1.7.1"],
    "pytest-dev/pytest": ["pytest==6.2.5", "py==1.11.0", "six==1.16.0", "setuptools",
                          "attrs==22.2.0", "more-itertools==10.1.0", "atomicwrites==1.4.1",
                          "pluggy==1.0.0", "iniconfig", "packaging", "tomli==2.0.1"],
    "pylint-dev/pylint": [],
}


def make_worktree(inst):
    """官方模式：base_commit 干净 worktree，不应用 test_patch。"""
    iid = inst["instance_id"]
    work = os.path.join(WORK, iid + "_official")
    repo_dir = os.path.join(REPOS, inst["repo"].split("/")[1])
    subprocess.run(["git", "worktree", "remove", "--force", work],
                   cwd=repo_dir, capture_output=True)
    subprocess.run(["git", "worktree", "prune"], cwd=repo_dir, capture_output=True)
    r = subprocess.run(["git", "worktree", "add", work, inst["base_commit"]],
                       cwd=repo_dir, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError("worktree add 失败: " + r.stderr[-200:])
    return work


def ask_llm_for_patch(inst, work):
    """非交互：LLM 读 issue + 文件树 + 相关文件内容 → 产出 patch。"""
    from swe_agent.prompts.prompt_manager import PromptManager
    pm = PromptManager()
    sys_msg = pm.load("base.md") + "\n" + pm.load("locate.md") + "\n" + pm.load("patch.md")

    src_dirs = []
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".graph")]
        depth = root[len(work):].count(os.sep)
        if depth > 3:
            dirs[:] = []
            continue
        for f in sorted(files):
            if f.endswith((".py", ".js", ".ts")):
                src_dirs.append(os.path.join(root[len(work):].lstrip("/"), f))
    tree_txt = "\n".join(src_dirs[:300])

    kw = [w for w in re.findall(r"[A-Za-z_]{4,}", inst["problem_statement"])
          if w.lower() not in ("the", "and", "with", "that", "this", "from",
                               "when", "should", "does", "have", "been", "not")]
    file_blocks = []
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules", ".graph")]
        if root[len(work):].count(os.sep) > 3:
            continue
        for f in sorted(files):
            if not f.endswith(".py"):
                continue
            rp = os.path.join(root[len(work):].lstrip("/"), f)
            base = os.path.basename(f).lower()
            if any(k.lower() in base or k.lower() in rp.lower() for k in kw[:8]):
                try:
                    lines = open(os.path.join(root, f), encoding="utf-8").read().splitlines()
                    file_blocks.append("### " + rp + "\n" + "\n".join(lines[:300]))
                except Exception:
                    pass
    content_txt = "\n\n".join(file_blocks[:4]) or "（未匹配到相关文件，请用文件名推断）"

    user_msg = (
        "# Issue\n" + inst["problem_statement"] + "\n\n"
        "# 项目文件（前 200 个）\n" + tree_txt + "\n\n"
        "# 相关文件内容（issue 关键词匹配）\n" + content_txt + "\n\n"
        "请分析 bug 并产出修复 patch。输出格式（JSON，不要解释、不要 markdown 代码块）：\n"
        '{"file": "相对路径", "start_line": 起始行, "end_line": 结束行, "new_code": "替换后的完整代码"}\n'
        "直接输出 JSON 对象本身（第一行就是 {，最后一行就是 }）。"
    )

    client = LLMClient()
    start = time.time()
    resp = client.chat(messages=[{"role": "system", "content": sys_msg},
                                 {"role": "user", "content": user_msg}],
                       temperature=0.2)
    dur = time.time() - start
    text = resp.content if hasattr(resp, "content") else str(resp)
    spec = _parse_json_lenient(text)
    return spec, dur, resp.usage


def _parse_json_lenient(text: str):
    """宽容 JSON 提取：剥 markdown 代码块 → 首尾大括号切片 → 双引号兼容。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"```(?:json)?\s*|\s*```", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e <= s:
        return None
    chunk = t[s:e + 1]
    for fn in (json.loads,):
        try:
            return fn(chunk)
        except Exception:
            pass
    # 单引号兼容
    try:
        import ast
        return ast.literal_eval(chunk)
    except Exception:
        return None


def apply_patch(work, patch_spec):
    """行范围替换写入。"""
    if not patch_spec:
        return "LLM 未产出 patch"
    fp = os.path.join(work, patch_spec["file"])
    if not os.path.exists(fp):
        return "文件不存在: " + patch_spec["file"]
    lines = open(fp, encoding="utf-8").read().splitlines()
    s = int(patch_spec["start_line"]) - 1
    e = int(patch_spec["end_line"])
    new = patch_spec["new_code"].splitlines()
    lines[s:e] = new
    open(fp, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return None


def run_eval(inst, work):
    """评测阶段：应用 test_patch → 跑 FTP/P2P（官方口径）。"""
    repo = inst["repo"]
    r = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=work,
                       input=inst["test_patch"], capture_output=True, text=True)
    if r.returncode != 0:
        return {"success": False, "reason": "test_patch 应用失败: " + r.stderr[:200]}
    ftp = eval(inst["FAIL_TO_PASS"])
    src_hint = "PYTHONPATH=/workspace/src " if os.path.isdir(os.path.join(work, "src")) else ""
    cmd = (src_hint + "python3 -m pytest " + " ".join('"%s"' % t for t in ftp[:6])
           + " -q -p no:cacheprovider")
    r1 = run_in_docker(work, cmd, python_version="3.8",
                       packages=REPO_DEPS.get(repo, []), timeout=900)
    return {"success": r1.exit_code == 0, "ftp_exit": r1.exit_code,
            "ftp_out": (r1.stdout or "")[-200:]}


def main():
    with open(os.path.join(DATA, "oracle_valid.json"), encoding="utf-8") as f:
        valid = json.load(f)
    with open(os.path.join(DATA, "swebench_subset_v2.json"), encoding="utf-8") as f:
        all_insts = {i["instance_id"]: i for i in json.load(f)}

    results = []
    for iid in valid:
        inst = all_insts[iid]
        print("=== %s（官方模式）===" % iid)
        work = make_worktree(inst)
        patch_spec, dur, usage = ask_llm_for_patch(inst, work)
        err = apply_patch(work, patch_spec)
        res = {"instance_id": iid, "mode": "official",
               "llm_duration": round(dur, 1),
               "patch_ok": err is None, "patch_err": err}
        if res["patch_ok"]:
            ev = run_eval(inst, work)
            res.update(ev)
        else:
            res["success"] = False
        res["token_total"] = getattr(usage, "total_tokens", 0) if usage else 0
        results.append(res)
        print(json.dumps(res, ensure_ascii=False)[:400])

    with open(os.path.join(DATA, "eval_official_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("\n结果已保存 eval_official_results.json")


if __name__ == "__main__":
    main()