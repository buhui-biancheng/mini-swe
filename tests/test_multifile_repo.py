# -*- coding: utf-8 -*-
"""场景测试：多文件 repo（bug 在子目录 + 测试在根目录）。
验证：code_dir 参数传入 → 测试容器挂载项目根 → 测试可找到。
"""
import os
import sys
import json
import shutil

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest
from types import SimpleNamespace


@pytest.fixture
def repo_project(tmp_path):
    """SWE-bench 式结构：bug 在子目录 pkg/，测试在根目录 tests/。"""
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / "pkg").mkdir()
    (proj / "tests").mkdir()
    (proj / "pkg" / "utils.py").write_text(
        "def add(a, b):\n    return a - b  # BUG\n", encoding="utf-8")
    (proj / "tests" / "test_utils.py").write_text(
        "from pkg.utils import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8")
    return proj


class TestMultiFileRepo:
    def test_code_dir_mounts_project_root(self, repo_project, monkeypatch):
        """code_dir=项目根 → 容器挂载项目根（bug 在子目录也能跑根目录测试）。"""
        from swe_agent.fsm.agent_fsm import AgentFSM

        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        proj = str(repo_project)
        bug_file = os.path.join(proj, "pkg", "utils.py")

        # mock docker：捕获挂载的 code_dir + 验证测试命令
        captured = {}
        def fake_docker(code_dir, command, **kw):
            captured["code_dir"] = code_dir
            captured["command"] = command
            return SimpleNamespace(exit_code=0, stdout="1 passed", stderr="")
        monkeypatch.setattr("swe_agent.sandbox.docker_runner.run_in_docker", fake_docker)

        fsm = AgentFSM(
            bug_file=bug_file,
            test_command="python3 -m pytest tests/test_utils.py -q",
            code_dir=proj,          # ← 关键：显式传项目根
            max_retries=1, mode="dp",
        )

        # 模拟进入 TEST 状态（手动触发测试执行）
        fsm.machine.set_state("test")
        fsm._on_enter_test()

        # 断言：容器挂载的是项目根（不是 pkg/ 子目录）
        assert captured.get("code_dir") == proj, f"应挂载项目根，实际: {captured.get('code_dir')}"
        # 测试命令保留 tests/ 前缀
        assert "tests/test_utils.py" in captured.get("command", "")
        print(f"✅ 挂载项目根: {captured['code_dir']}")
        print(f"✅ 测试命令: {captured['command'][:60]}")

    def test_default_code_dir_is_bug_dir(self, repo_project, monkeypatch):
        """兼容：不传 code_dir 时默认 = bug 文件所在目录（原行为）。"""
        from swe_agent.fsm.agent_fsm import AgentFSM
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        proj = str(repo_project)
        bug_file = os.path.join(proj, "pkg", "utils.py")
        captured = {}
        def fake_docker(code_dir, command, **kw):
            captured["code_dir"] = code_dir
            return SimpleNamespace(exit_code=0, stdout="1 passed", stderr="")
        monkeypatch.setattr("swe_agent.sandbox.docker_runner.run_in_docker", fake_docker)
        fsm = AgentFSM(bug_file=bug_file,
                       test_command="python3 -m pytest test_utils.py -q",
                       max_retries=1, mode="dp")
        fsm.machine.set_state("test")
        fsm._on_enter_test()
        assert captured["code_dir"] == os.path.dirname(bug_file), "默认应 = bug 文件目录"
        print(f"✅ 默认兼容: {captured['code_dir']}")