"""测试日志解析器单元测试（Phase 2 模块 D）。"""

import os

from swe_agent.graph import parse_test_log, save_full_log

CODE_DIR = "/workspace"

TWO_FAILURES = '''\
============================= test session starts ==============================
collected 2 items

test_bug.py::test_add FAILED
test_bug.py::test_sub FAILED

______________________________ test_add ______________________________

    def test_add():
>       assert add(1, 2) == 3
E       assert 1 == 3
E        +  where 1 = add(1, 2)

/workspace/bug.py:3: in add
    return a - b
E       AssertionError

______________________________ test_sub ______________________________

    def test_sub():
>       assert sub(5, 2) == 3
E       assert 7 == 3
E        +  where 7 = sub(5, 2)

/workspace/bug.py:8: in sub
    return a + b
E       AssertionError

========================= short test summary info =============================
FAILED test_bug.py::test_add - AssertionError
FAILED test_bug.py::test_sub - AssertionError
'''

SINGLE_NO_SECTION = '''\
Traceback (most recent call last):
  File "/workspace/bug.py", line 3, in add
    return a - b
ZeroDivisionError
'''


class TestParseTestLog:
    def test_green_returns_empty(self):
        result = parse_test_log("2 passed in 0.1s", 0, CODE_DIR)
        assert result.has_failures is False
        assert result.grouped_errors == []

    def test_groups_two_failures(self):
        result = parse_test_log(TWO_FAILURES, 1, CODE_DIR)
        assert result.has_failures is True
        assert len(result.grouped_errors) == 2

        e0 = result.grouped_errors[0]
        assert e0.error_type == "AssertionError"
        assert e0.file == "bug.py"
        assert e0.lineno == 3
        assert e0.function == "add"
        # 日志行号范围落在分节内
        assert 0 < e0.log_start_line <= e0.log_end_line

        e1 = result.grouped_errors[1]
        assert e1.lineno == 8
        assert "sub" in e1.function

    def test_callsite_chain(self):
        result = parse_test_log(TWO_FAILURES, 1, CODE_DIR)
        e0 = result.grouped_errors[0]
        assert any("bug.py" in c for c in e0.callsite)

    def test_failures_segment_truncated(self):
        result = parse_test_log(TWO_FAILURES, 1, CODE_DIR,
                                failures_segment_limit=100)
        assert len(result.raw_log_failures) <= 100 + 10  # 加截断标记长度

    def test_no_section_but_traceback(self):
        result = parse_test_log(SINGLE_NO_SECTION, 1, CODE_DIR)
        assert len(result.grouped_errors) >= 1
        assert result.grouped_errors[0].error_type == "ZeroDivisionError"
        assert result.grouped_errors[0].file == "bug.py"

    def test_plain_failure_no_error(self):
        result = parse_test_log("FAILED test.py::test_x", 1, CODE_DIR)
        # 无分节、无 Traceback → grouped_errors 为空（无法结构化，AI 看日志兜底）
        assert result.grouped_errors == []


class TestSaveFullLog:
    def test_save_full_log(self, tmp_path):
        path = save_full_log("full log content", str(tmp_path))
        assert path == os.path.join(str(tmp_path), "last_test.log")
        with open(path, "r", encoding="utf-8") as f:
            assert f.read() == "full log content"
