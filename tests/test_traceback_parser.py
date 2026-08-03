"""Traceback 解析器单元测试（Phase 2 模块 E2）。"""

from swe_agent.graph import parse_traceback, split_sections, extract_frames, to_project_rel

CODE_DIR = "/workspace"

SINGLE_TRACEBACK = '''\
______________________________ test_add ______________________________

    def test_add():
>       assert add(1, 2) == 3
E       assert 1 == 3

/workspace/test_bug.py:5: in test_add
    assert add(1, 2) == 3
/workspace/bug.py:3: in add
    return a - b
E       AssertionError
'''

CHAINED = '''\
______ test_chain ______

    def test_chain():
>       process()
E       KeyError: 'missing'

/workspace/lib.py:10: in process
    raise KeyError('missing')
During handling of the above exception, another exception occurred
    Traceback (most recent call last):
      File "/workspace/test_chain.py", line 4, in test_chain
        process()
      File "/workspace/lib.py", line 12, in process
        raise ValueError('bad')
E       ValueError: bad
'''

SITE_PACKAGES = '''\
File "/usr/lib/python3.11/site-packages/requests/api.py", line 58, in get
  File "/workspace/app.py", line 5, in main
    return get()
'''


class TestSplitSections:
    def test_no_chain_marker_single_section(self):
        sections = split_sections(SINGLE_TRACEBACK)
        assert len(sections) == 1

    def test_chained_splits_into_two(self):
        sections = split_sections(CHAINED)
        assert len(sections) == 2
        # 最后一段是根因（最后一次抛出的异常）
        assert "ValueError: bad" in sections[-1]
        assert "KeyError" not in sections[-1]


class TestExtractFrames:
    def test_extracts_file_line_func(self):
        frames = extract_frames(SINGLE_TRACEBACK)
        assert len(frames) == 2
        assert frames[0].file == "/workspace/test_bug.py"
        assert frames[0].lineno == 5
        assert frames[0].funcname == "test_add"
        assert frames[1].funcname == "add"


class TestToProjectRel:
    def test_container_workspace_path(self):
        assert to_project_rel("/workspace/bug.py", "/workspace") == "bug.py"

    def test_outside_code_dir_is_none(self):
        assert to_project_rel("/usr/lib/python3.11/x.py", "/workspace") is None

    def test_relative_path_passthrough(self):
        assert to_project_rel("bug.py", "/workspace") == "bug.py"


class TestParseTraceback:
    def test_new_start_single(self):
        result = parse_traceback(SINGLE_TRACEBACK, CODE_DIR)
        assert result.new_start is not None
        # 规则 3：第一个项目文件作为新起点
        assert result.new_start.file == "test_bug.py"
        assert result.new_start.lineno == 5
        assert len(result.frames) == 2

    def test_chain_takes_last_section(self):
        result = parse_traceback(CHAINED, CODE_DIR)
        assert result.new_start is not None
        # 根因是 ValueError，取最后一段的项目帧
        assert result.new_start.file == "test_chain.py"
        assert "ValueError" in result.raw_section
        assert "KeyError" not in result.raw_section

    def test_filters_site_packages(self):
        result = parse_traceback(SITE_PACKAGES, CODE_DIR)
        assert len(result.frames) == 1
        assert result.frames[0].file == "app.py"

    def test_no_project_file_returns_none(self):
        result = parse_traceback(
            'File "/usr/lib/python3.11/os.py", line 225, in getcwd',
            CODE_DIR,
        )
        assert result.new_start is None
        assert result.frames == []

    def test_deterministic(self):
        r1 = parse_traceback(SINGLE_TRACEBACK, CODE_DIR)
        r2 = parse_traceback(SINGLE_TRACEBACK, CODE_DIR)
        assert r1.new_start.key == r2.new_start.key
