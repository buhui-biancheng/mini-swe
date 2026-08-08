"""SyntaxFirewall 单元测试（Phase 3）。"""

import os
from types import SimpleNamespace

import pytest

from swe_agent.graph import SyntaxFirewall, SyntaxCheckResult, SyntaxErrorInfo


class TestSyntaxFirewall:
    def setup_method(self):
        self.firewall = SyntaxFirewall()

    def test_valid_code_passes(self):
        code = "def add(a, b):\n    return a + b\n"
        result = self.firewall.check_code(code)
        assert result.ok is True
        assert result.errors == []

    def test_syntax_error_returns_line(self):
        code = "def broken(:\n    pass\n"
        result = self.firewall.check_code(code)
        assert result.ok is False
        assert len(result.errors) == 1
        # 语法错误行号精确（def broken( 在第 1 行）
        assert result.errors[0].line == 1

    def test_syntax_error_later_line(self):
        code = "def ok():\n    return 1\n\nif True:\n    pass\n    pass =\n"
        result = self.firewall.check_code(code)
        assert result.ok is False
        # 错误在第 6 行（pass = 非法赋值）
        assert result.errors[0].line == 6

    def test_indentation_error_caught(self):
        code = "def f():\nreturn 1\n"
        result = self.firewall.check_code(code)
        assert result.ok is False
        assert result.errors[0].line >= 2

    def test_check_file(self, tmp_path):
        good = tmp_path / "good.py"
        good.write_text("def f():\n    return 1\n", encoding="utf-8")
        result = self.firewall.check_file(str(good))
        assert result.ok is True

        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:\n", encoding="utf-8")
        result = self.firewall.check_file(str(bad))
        assert result.ok is False
        assert result.errors[0].line == 1

    def test_check_file_missing(self, tmp_path):
        result = self.firewall.check_file(str(tmp_path / "missing.py"))
        assert result.ok is False
        assert "读取失败" in result.summary

    def test_summary_human_readable(self):
        ok = SyntaxCheckResult(ok=True)
        assert ok.summary == "语法检查通过"
        bad = SyntaxCheckResult(ok=False, errors=[SyntaxErrorInfo(line=3, msg="invalid syntax")])
        assert bad.summary == "第 3 行: invalid syntax"

    def test_ast_parse_is_deterministic(self):
        """纯语法检查，无外部依赖/网络。"""
        code = "x = 1\n"
        r1 = self.firewall.check_code(code)
        r2 = self.firewall.check_code(code)
        assert r1.ok == r2.ok


class TestFSMSyntaxFirewallIntegration:
    """FSM 集成：PATCH → TEST 之间拦截语法错误，不进 Docker。"""

    def _make_fsm(self, tmp_path, monkeypatch, code, docker_exit=0):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        monkeypatch.setattr(
            "swe_agent.sandbox.docker_runner.run_in_docker",
            lambda *a, **k: SimpleNamespace(exit_code=docker_exit, stdout="", stderr=""),
        )
        proj = tmp_path / "proj"
        proj.mkdir(exist_ok=True)
        bug = proj / "bug.py"
        bug.write_text(code, encoding="utf-8")
        from swe_agent.fsm.agent_fsm import AgentFSM
        fsm = AgentFSM(str(bug), "pytest", max_retries=2, mode="dp")
        return fsm, bug

    def test_firewall_intercepts_and_feeds_back(self, tmp_path, monkeypatch):
        """语法错误被拦截 → 反馈给 LLM → LLM 修正后通过 → 成功。

        mock LLM：第二轮读到"语法错误"反馈后把文件改合法。
        """
        fsm, bug = self._make_fsm(tmp_path, monkeypatch, "def broken(:\n    pass\n")
        calls = []

        def fake_chat(messages, tools=None, tool_executor=None, max_rounds=5, usage_callback=None, thinking=False, reasoning_effort="high"):
            calls.append(list(messages))
            # 模拟 LLM 看到语法错误反馈后修好文件
            bug.write_text("def broken():\n    pass\n", encoding="utf-8")
            if usage_callback:
                usage_callback({})
            return ("fixed", messages)

        fsm.client.chat_with_tools = fake_chat
        fsm.machine.set_state("patch")
        fsm._on_enter_patch()

        # 文件被修好后通过防火墙和 Docker 测试 → 成功
        assert fsm.state == "success"
        # 第二轮 LLM 确实收到了语法错误反馈（含行号）
        feedback = [m["content"] for msgs in calls for m in msgs if "语法错误" in m.get("content", "")]
        assert len(feedback) == 1
        assert "第 1 行" in feedback[0]

    def test_valid_code_passes_firewall(self, tmp_path, monkeypatch):
        """有效代码放行进入 TEST（mock Docker），最终成功。"""
        fsm, _ = self._make_fsm(tmp_path, monkeypatch, "def f():\n    return 1\n")
        fsm.machine.set_state("patch")
        fsm._on_enter_patch()

        assert fsm.state == "success"   # 防火墙放行 → test（mock docker 通过）
        assert fsm._syntax_errors == []
