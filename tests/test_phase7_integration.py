# -*- coding: utf-8 -*-
"""Phase 7 模块集成测试（合成体系，全部 mock 不依赖网络）。

覆盖：diagnose 锚点/定位 → 桥接 → Probe 校验/沙箱 → 覆盖映射 → 分层验证 → 意图路由 → 证据
"""
import sys
import os
import json

sys.path.insert(0, "/home/yuanyin292/桌面/xiangmu1")

import pytest

PROJECT = "/home/yuanyin292/桌面/xiangmu1/examples/multi_file_project"
PROJECT2 = "/home/yuanyin292/桌面/xiangmu1/examples"


class MockLLM:
    """可编程 mock LLM：按调用次数返回预设响应。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, max_tokens=None, temperature=None):
        self.calls += 1
        if self.calls <= len(self.responses):
            return type("R", (), {"content": self.responses[self.calls - 1]})()
        return type("R", (), {"content": json.dumps(
            {"is_fix_request": False, "issue": "", "reason": "默认拒绝"})})()


# ── 模块 B：diagnose ──
class TestDiagnose:
    def test_anchor_extraction_graph_driven(self):
        from swe_agent.diagnose import DiagnoseAgent
        agent = DiagnoseAgent(project_dir=PROJECT, verbose=False)
        anchors = agent._extract_anchors("compute_rectangle_area 返回错误，ValueError")
        assert "compute_rectangle_area" in anchors["functions"]
        assert "ValueError" in anchors["errors"]

    def test_anchor_matches_graph(self):
        from swe_agent.diagnose import DiagnoseAgent
        agent = DiagnoseAgent(project_dir=PROJECT, verbose=False)
        anchors = agent._extract_anchors("compute_rectangle_area 结果错误")
        hits = agent._match_anchors(anchors)
        assert len(hits) >= 1
        assert any("calculator.py" in h["file"] for h in hits)

    def test_llm_parse_valid_json(self):
        from swe_agent.diagnose import DiagnoseAgent
        agent = DiagnoseAgent(project_dir=PROJECT, verbose=False)
        agent.llm_client = MockLLM([json.dumps({
            "issue": "x", "candidates": [
                {"file": "calculator.py", "functions": ["compute_rectangle_area"],
                 "reason": "锚点命中", "test_anchor": "pytest test_calculator.py"}],
            "confidence": 0.9, "summary": "ok"})])
        r = agent.diagnose("compute_rectangle_area 错误")
        assert r.best() is not None
        assert r.best().file == "calculator.py"

    def test_llm_invalid_json_retries(self):
        from swe_agent.diagnose import DiagnoseAgent
        agent = DiagnoseAgent(project_dir=PROJECT, verbose=False)
        agent.llm_client = MockLLM(["不是JSON", "也不是JSON"])
        r = agent.diagnose("bug")
        assert r.candidates == []
        assert agent.llm_client.calls == agent.max_rounds


# ── 模块 B2：桥接 + 漂移 ──
class TestBridge:
    def test_matches_drift(self):
        from swe_agent.diagnose.bridge import _matches, _candidate_files
        from swe_agent.diagnose.schemas import DiagnoseResult, DiagnoseCandidate
        assert _matches(["calculator.py"], "calculator.py")
        assert not _matches(["calculator.py"], "other.py")
        assert _matches(["calculator.py"], None)
        r = DiagnoseResult(issue="x", candidates=[
            DiagnoseCandidate(file="src/calculator.py", functions=["f"])])
        assert _candidate_files(r) == ["calculator.py"]


# ── 模块 C：Probe ──
class TestProbe:
    def test_safety_check(self):
        from swe_agent.diagnose.probe import ProbeSpec, ProbeGenerator
        gen = ProbeGenerator(ProbeSpec(issue="x", target_file="a.py"), verbose=False)
        assert gen._check_safety('import subprocess\nsubprocess.run("x")')
        assert not gen._check_safety('import math\nassert math.sqrt(4) == 2')

    def test_assertion_tokens(self):
        from swe_agent.diagnose.probe import ProbeValidator
        tokens = ProbeValidator.extract_expected_tokens("长4宽5应该返回20")
        assert "20" in tokens
        missing = ProbeValidator.validate_assertion("assert calc(4,5) == 20", tokens)
        assert missing == []

    def test_sandbox_blocks_write(self):
        import subprocess
        from swe_agent.diagnose.probe import _PROBE_HEADER
        d = "/tmp/probe_pytest_test"
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "p.py"), "w", encoding="utf-8") as f:
            f.write(_PROBE_HEADER + '\nopen("/tmp/evil2.txt", "w").write("x")')
        r = subprocess.run(["python3", os.path.join(d, "p.py")],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode != 0
        assert not os.path.exists("/tmp/evil2.txt")


# ── 模块 D：覆盖 + 分层验证 ──
class TestVerification:
    def test_coverage_capture_and_prune(self):
        from swe_agent.diagnose.coverage import CoverageMap
        cm = CoverageMap(PROJECT)
        files = cm.discover_test_files()
        result = cm.capture(files, timeout=60)
        assert result.get("mapped"), "应捕获覆盖映射"
        assert any("test_calculator" in t for t in cm.tests_covering_file("calculator.py"))
        cmd = cm.prune_command("pytest -q", ["calculator.py"])
        assert "test_calculator" in cmd

    def test_l1_reports_failure_on_bug_fixture(self):
        from swe_agent.diagnose.verification import VerificationScheduler
        vs = VerificationScheduler(PROJECT, verbose=False)
        r = vs.run_l1("python3 -m pytest test_calculator.py -q")
        assert r.passed is False  # bug 夹具如实报失败
        assert r.layer == "L1"

    def test_l1_passes_on_clean(self):
        from swe_agent.diagnose.verification import VerificationScheduler
        vs = VerificationScheduler(PROJECT2, verbose=False)
        r = vs.run_l1("python3 -m pytest test_return_value.py -q")
        assert r.passed is True


# ── 模块 E：证据 ──
class TestEvidence:
    def test_evidence_package(self):
        from swe_agent.diagnose.evidence import EvidenceBuilder
        ev = EvidenceBuilder.build(
            issue="长4宽5应该返回20", test_command="pytest test_x.py -q",
            passed=True, edited_files=["calculator.py"])
        assert "技术层证据" in ev.technical
        assert "业务层证据" in ev.business
        assert "4" in ev.confirm_question and "5" in ev.confirm_question
        assert ev.needs_confirmation is True

    def test_evidence_failure_no_confirm(self):
        from swe_agent.diagnose.evidence import EvidenceBuilder
        ev = EvidenceBuilder.build(issue="x", test_command="", passed=False)
        assert "尚未通过验证" in ev.business


# ── 模块 F：意图路由 ──
class TestRouter:
    def test_fix_request(self):
        from swe_agent.diagnose.router import IntentRouter
        router = IntentRouter(llm_client=MockLLM([json.dumps(
            {"is_fix_request": True, "issue": "add 返回错误",
             "reason": "报 bug"})]))
        d = router.decide("add 函数返回错误")
        assert d.is_fix_request

    def test_non_fix_request(self):
        from swe_agent.diagnose.router import IntentRouter
        router = IntentRouter(llm_client=MockLLM([json.dumps(
            {"is_fix_request": False, "issue": "", "reason": "闲聊"})]))
        d = router.decide("你好")
        assert not d.is_fix_request
