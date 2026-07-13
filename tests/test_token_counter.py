"""Token 统计工具测试。"""

import os
import tempfile
import shutil
import pytest
from swe_agent.utils.token_counter import (
    count_tokens,
    compare_token_counts,
    analyze_project_tokens,
)


@pytest.fixture
def sample_project():
    """创建一个临时测试项目。"""
    tmpdir = tempfile.mkdtemp()
    project_dir = os.path.join(tmpdir, "test_project")
    os.makedirs(project_dir)

    # 创建 utils.py
    utils_code = '''\
def helper_func(x, y):
    """帮助函数。"""
    return x + y

def another_func():
    pass

class MyClass:
    def method_a(self):
        pass

    def method_b(self, arg):
        return arg * 2
'''
    with open(os.path.join(project_dir, "utils.py"), "w") as f:
        f.write(utils_code)

    # 创建 main.py
    main_code = '''\
def main():
    print("hello")

if __name__ == "__main__":
    main()
'''
    with open(os.path.join(project_dir, "main.py"), "w") as f:
        f.write(main_code)

    yield project_dir

    shutil.rmtree(tmpdir)


class TestCountTokens:
    """count_tokens 测试类。"""

    def test_basic_counting(self):
        """测试基本 token 计数。"""
        text = "def hello(): pass"
        tokens = count_tokens(text)
        assert tokens > 0

    def test_empty_string(self):
        """测试空字符串。"""
        tokens = count_tokens("")
        assert tokens == 0

    def test_chinese_text(self):
        """测试中文文本。"""
        text = "这是一个测试函数"
        tokens = count_tokens(text)
        assert tokens > 0


class TestCompareTokenCounts:
    """compare_token_counts 测试类。"""

    def test_reduction_calculation(self):
        """测试压缩率计算。"""
        full_source = "def func():\n    pass\n\n" * 10
        skeleton = "func (lines 1-2)"

        result = compare_token_counts(full_source, skeleton)

        assert "full_tokens" in result
        assert "skeleton_tokens" in result
        assert "reduction_percent" in result
        assert result["reduction_percent"] > 0  # 骨架应该更短

    def test_empty_source(self):
        """测试空源码。"""
        result = compare_token_counts("", "")
        assert result["reduction_percent"] == 0


class TestAnalyzeProjectTokens:
    """analyze_project_tokens 测试类。"""

    def test_analyze_project(self, sample_project):
        """测试项目 token 分析。"""
        result = analyze_project_tokens(sample_project)

        assert "file_count" in result
        assert "total_functions" in result
        assert "full_tokens" in result
        assert "skeleton_tokens" in result
        assert "reduction_percent" in result
        assert "files" in result

        assert result["file_count"] == 2
        assert result["full_tokens"] > 0
        assert result["skeleton_tokens"] > 0

    def test_reduction_percent(self, sample_project):
        """测试压缩率。"""
        result = analyze_project_tokens(sample_project)

        # 骨架应该比完整源码短
        assert result["skeleton_tokens"] < result["full_tokens"]
        assert result["reduction_percent"] > 0
