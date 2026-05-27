import os
import tempfile
import pytest
from swe_agent.ast_view.function_map import get_function_line_map, get_function_source


SAMPLE_CODE = '''\
import os

def foo():
    x = 1
    return x

def bar(a, b):
    return a + b

class MyClass:
    def method_a(self):
        pass

    def method_b(self, x):
        return x * 2

async def async_func():
    await something()
'''


@pytest.fixture
def sample_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(SAMPLE_CODE)
        f.flush()
        yield f.name
    os.unlink(f.name)


class TestGetFunctionLineMap:
    def test_top_level_functions(self, sample_file):
        result = get_function_line_map(sample_file)
        assert "foo" in result
        assert "bar" in result

    def test_class_methods(self, sample_file):
        result = get_function_line_map(sample_file)
        assert "MyClass.method_a" in result
        assert "MyClass.method_b" in result

    def test_async_function(self, sample_file):
        result = get_function_line_map(sample_file)
        assert "async_func" in result

    def test_line_numbers_correct(self, sample_file):
        result = get_function_line_map(sample_file)
        # foo 在第 3 行定义，到第 5 行结束
        assert result["foo"] == (3, 5)
        # bar 在第 7 行定义，到第 8 行结束
        assert result["bar"] == (7, 8)

    def test_method_line_numbers(self, sample_file):
        result = get_function_line_map(sample_file)
        assert result["MyClass.method_a"] == (11, 12)
        assert result["MyClass.method_b"] == (14, 15)

    def test_returns_dict(self, sample_file):
        result = get_function_line_map(sample_file)
        assert isinstance(result, dict)
        for key, value in result.items():
            assert isinstance(key, str)
            assert isinstance(value, tuple)
            assert len(value) == 2


class TestGetFunctionSource:
    def test_get_top_level_function(self, sample_file):
        source = get_function_source(sample_file, "foo")
        assert source is not None
        assert "def foo" in source
        assert "return x" in source

    def test_get_class_method(self, sample_file):
        source = get_function_source(sample_file, "MyClass.method_b")
        assert source is not None
        assert "def method_b" in source
        assert "return x * 2" in source

    def test_nonexistent_function(self, sample_file):
        source = get_function_source(sample_file, "nonexistent")
        assert source is None

    def test_async_function_source(self, sample_file):
        source = get_function_source(sample_file, "async_func")
        assert source is not None
        assert "async def" in source
