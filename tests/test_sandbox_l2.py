# -*- coding: utf-8 -*-
"""Phase 6 补测：run_command 沙盒分流 / L2 容器属性（只读+断网）/ L1 AI 探查。"""
import os
import sys
import json
import shutil

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest
from types import SimpleNamespace


@pytest.fixture
def real_project(tmp_path):
    proj = tmp_path / "real_proj"
    proj.mkdir()
    (proj / "bug.py").write_text("def add(a, b):\n    return a - b  # BUG\n", encoding="utf-8")
    (proj / "secret.txt").write_text("宿主机机密", encoding="utf-8")
    return proj


class TestRunCommandSandbox:
    def test_sandbox_run_command_goes_to_container(self, real_project, monkeypatch):
        """sandbox 模式下 run_command 走容器（不碰宿主机）。

        2026-08-14 改造：非 git 命令一律进容器（断网 + 依赖环境），
        sandbox 挂载的是工作副本 ws。
        """
        from swe_agent.sandbox.l1_sandbox import L1Sandbox
        from swe_agent.tools.registry import ToolRegistry

        sb = L1Sandbox(str(real_project))
        ws = sb.create(task_id="t_rc_1")
        reg = ToolRegistry(skeleton_text="", code_dir=ws, sandbox=True)

        # monkeypatch run_in_docker 验证被调用（2026-08-14 起带 python_version/packages/reuse kwargs）
        called = {}
        def fake_docker(code_dir, command, *a, **k):
            called["code_dir"] = code_dir
            called["command"] = command
            return SimpleNamespace(stdout="ok", stderr="", exit_code=0)
        monkeypatch.setattr("swe_agent.sandbox.docker_runner.run_in_docker", fake_docker)

        result = json.loads(reg.execute("run_command", {"command": "ls -la"}))
        assert called, "run_command 应走容器"
        assert called["code_dir"] == ws, "容器挂载的应是工作副本"
        assert result["exit_code"] == 0
        sb.cleanup()
        print("✅ sandbox run_command 走容器（挂载工作副本）")

    def test_nonsandbox_git_command_stays_host(self, real_project, monkeypatch):
        """非沙盒模式下 git 命令留宿主执行（worktree 对象库在宿主主仓库）。

        2026-08-14 改造后：非 git 命令一律进容器；git 命令留宿主（容器内 git
        用不了 worktree 的 .git 文件）。本测试用 git 命令验证宿主路径。
        """
        from swe_agent.tools.registry import ToolRegistry
        reg = ToolRegistry(skeleton_text="", code_dir=str(real_project), sandbox=False)
        called = {}
        def fake_docker(*a, **k):
            called["called"] = True
            return SimpleNamespace(stdout="", stderr="", exit_code=0)
        monkeypatch.setattr("swe_agent.sandbox.docker_runner.run_in_docker", fake_docker)
        result = json.loads(reg.execute("run_command", {"command": "git status"}))
        assert "called" not in called, "git 命令应留宿主执行"
        assert "stdout" in result and "exit_code" in result
        print("✅ 非沙盒 git 命令保持宿主机")


class TestL2ContainerProps:
    def test_l2_container_is_ro_and_disconnected(self, real_project):
        """L2 测试容器：只读挂载（无法写工作副本）+ 断网 + 每次新容器。"""
        from swe_agent.sandbox.l2_sandbox import L2Sandbox
        # 在副本里跑：容器内尝试写 /workspace 应失败（只读）
        r = L2Sandbox.run_test(str(real_project),
            "python3 -c 'import os; os.write(1, b\"x\")' ; "
            "touch /workspace/try_write.txt && echo WROTE || echo READONLY", timeout=60)
        # 容器内 /workspace 只读：touch 应失败
        assert "READONLY" in (r.stdout + r.stderr), f"应只读: {r.stdout}{r.stderr}"
        assert not os.path.exists(os.path.join(str(real_project), "try_write.txt")), "工作副本不应被写入"
        print("✅ L2 容器只读：无法写工作副本")

    def test_l2_container_network_disabled(self, real_project):
        from swe_agent.sandbox.l2_sandbox import L2Sandbox
        r = L2Sandbox.run_test(str(real_project),
            "python3 -c 'import urllib.request; urllib.request.urlopen(\"http://10.255.255.1\", timeout=3)'",
            timeout=30)
        assert r.exit_code != 0, "断网容器应无法联网"
        print("✅ L2 容器断网")