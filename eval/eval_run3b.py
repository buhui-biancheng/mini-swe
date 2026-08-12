# -*- coding: utf-8 -*-
"""官方 SWE-bench 模式 v2（2026-08-13）：
agent 有环境（可 view_file/search_function 自由探索），无 test_patch（测试不可见）。
LLM 自评完成 → 输出 patch JSON → 评测阶段应用 model_patch + test_patch → FTP/P2P。
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
    iid = inst["instance_id"]
    work = os.path.join(WORK, iid + "_official")
    subprocess.run(["rm", "-rf", work], capture_output=True)
    subprocess.run(["git", "worktree", "add", work, inst["base_commit"]],
                   cwd=os.path.join(REPOS, inst["repo"].split("/")[1]),
                   capture_output=True, timeout=300)
    return work


TOOLS = [
    {"type": "function", "function": {
        "name": "view_file",
        "description": "查看文件内容（行范围）",
        "parameters": {"type": "object",
                       "properties": {"file": {"type": "string", "description": "相对路径"},
                                      "start_line": {"type": "integer"},
                                      "end_line": {"type": "integer"}},
                       "required": ["file"]}}},
    {"type": "function", "function": {
        "name": "run_test",
        "description": "运行仓库自带测试（pytest 容器内）。参数 file=测试文件相对路径（如 tests/test_cli.py），或 keyword=测试名关键字",
        "parameters": {"type": "object",
                       "properties": {"file": {"type": "string"},
                                      "keyword": {"type": "string"}},
                       "required": ["file"]}}},
    {"type": "function", "function": {
        "name": "search_function",
        "description": "按名字搜索函数/类，返回候选位置",
        "parameters": {"type": "object",
                       "properties": {"name": {"type": "string"}},
                       "required": ["name"]}}},
]


def make_executor(work):
    def executor(name: str, args: dict) -> str:
        try:
            if name == "view_file":
                fp = os.path.join(work, args["file"])
                if not os.path.exists(fp):
                    return "文件不存在"
                lines = open(fp, encoding="utf-8").read().splitlines()
                s = int(args.get("start_line", 1)) - 1
                e = int(args.get("end_line", min(len(lines), s + 120)))
                return "\n".join("%d: %s" % (i + 1, l) for i, l in enumerate(lines[s:e]))
            if name == "run_test":
                from swe_agent.sandbox.docker_runner import run_in_docker
                tf = args.get("file", "")
                kw = args.get("keyword", "")
                cmd = "python3 -m pytest %s -q -p no:cacheprovider" % tf
                if kw:
                    cmd += " -k " + kw
                r = run_in_docker(work, cmd, python_version="3.8",
                                  packages=["pytest==6.2.5", "werkzeug==2.2.3",
                                            "click==8.1.3", "itsdangerous==2.1.2",
                                            "jinja2==3.1.2", "blinker==1.7.0",
                                            "importlib_metadata==6.7.0"],
                                  timeout=300)
                return "exit=%d\n%s" % (r.exit_code, (r.stdout or "")[-800:])
            if name == "search_function":
                pat = args["name"].lower()
                hits = []
                for root, dirs, files in os.walk(work):
                    dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
                    if root[len(work):].count(os.sep) > 4:
                        continue
                    for f in files:
                        if not f.endswith(".py"):
                            continue
                        rp = os.path.join(root[len(work):].lstrip("/"), f)
                        for i, l in enumerate(open(os.path.join(root, f), encoding="utf-8", errors="replace")):
                            if pat in l.lower() and l.strip().startswith(("def ", "class ")):
                                hits.append("%s:%d: %s" % (rp, i + 1, l.strip()[:100]))
                                if len(hits) >= 15:
                                    return "\n".join(hits)
                return "\n".join(hits) or "未找到"
        except Exception as e:
            return "工具错误: %s" % e
    return executor


def ask_llm_for_patch(inst, work):
    from swe_agent.prompts.prompt_manager import PromptManager
    pm = PromptManager()
    # 文件树
    tree_lines = []
    for root, dirs, files in os.walk(work):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        if root[len(work):].count(os.sep) > 3:
            continue
        for f in sorted(files):
            if f.endswith((".py", ".js", ".ts")):
                tree_lines.append(os.path.join(root[len(work):].lstrip("/"), f))
    tree_txt = "\n".join(tree_lines[:150])

    sys_msg = (pm.load("base.md") + "\n" + pm.load("locate.md") + "\n" + pm.load("patch.md") + "\n"
               "[官方评测模式] 仓库自带测试可见可运行（run_test），但官方隐藏测试不可见。"
               "文件树：\n" + tree_txt + "\n"
               "流程：最多 8 次工具调用完成探索与验证（run_test 可跑自带测试确认方向），"
               "**第 9 次回复必须输出交卷 JSON**（只输出 JSON，不要解释）：\n"
               '{"file": "相对路径", "start_line": 起始行, "end_line": 结束行, "new_code": "替换后的完整代码"}')

    user_msg = ("# Issue\n" + inst["problem_statement"] +
                "\n\n分析并修复这个 bug。可以先看文件再修改。完成时输出 patch JSON。")

    client = LLMClient()
    start = time.time()
    _, conversation = client.chat_with_tools(
        messages=[{"role": "system", "content": sys_msg},
                  {"role": "user", "content": user_msg}],
        tools=TOOLS, tool_executor=make_executor(work),
        max_rounds=20)
    # chat_with_tools 返回 ("", conversation)——最终文本在最后一条 assistant 消息
    final_text = ""
    for msg in reversed(conversation):
        if msg.get("role") == "assistant" and msg.get("content"):
            final_text = msg["content"]
            break
    dur = time.time() - start
    m = re.search(r"\{[^{}]*\"file\"[^{}]*\}", final_text, re.S)
    return (json.loads(m.group(0)) if m else None), dur, final_text


def apply_patch(work, patch_spec):
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
        print("=== %s（官方模式 v2）===" % iid)
        work = make_worktree(inst)
        patch_spec, dur, final_text = ask_llm_for_patch(inst, work)
        err = apply_patch(work, patch_spec)
        res = {"instance_id": iid, "mode": "official_v2",
               "llm_duration": round(dur, 1),
               "patch_ok": err is None, "patch_err": err}
        if res["patch_ok"]:
            ev = run_eval(inst, work)
            res.update(ev)
        else:
            res["success"] = False
        results.append(res)
        print(json.dumps(res, ensure_ascii=False)[:400])
        print("LLM 尾部输出:", (final_text or "")[-150:])

    with open(os.path.join(DATA, "eval_official_results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print("\n结果已保存 eval_official_results.json")


if __name__ == "__main__":
    main()