"""GraphIndex 骨架兼容层单元测试（SkeletonTree 迁移后的测试）。

SkeletonTree 已迁移到 GraphIndex（generate_skeleton_text / expand_function /
search_nodes / get_file_functions），本文件验证兼容层行为与旧接口一致。
"""

import os
import tempfile
import shutil
import pytest

from swe_agent.graph import GraphManager


@pytest.fixture
def sample_project():
    """创建一个临时测试项目。"""
    tmpdir = tempfile.mkdtemp()
    project_dir = os.path.join(tmpdir, "test_project")
    os.makedirs(project_dir)

    # 创建 utils.py（含顶层函数和类）
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

    # 创建 main.py（只含顶层函数）
    main_code = '''\
def main():
    print("hello")

if __name__ == "__main__":
    main()
'''
    with open(os.path.join(project_dir, "main.py"), "w") as f:
        f.write(main_code)

    # 创建 sub/ 目录
    sub_dir = os.path.join(project_dir, "sub")
    os.makedirs(sub_dir)
    sub_code = '''\
def sub_func():
    pass
'''
    with open(os.path.join(sub_dir, "module.py"), "w") as f:
        f.write(sub_code)

    yield project_dir

    # 清理
    shutil.rmtree(tmpdir)


@pytest.fixture
def index(sample_project, tmp_path):
    """构建 GraphIndex（迁移：SkeletonTree → GraphIndex）。"""
    mgr = GraphManager(sample_project, graph_dir=str(tmp_path / "g"))
    return mgr.build(force=True)


class TestGraphSkeletonCompat:
    """GraphIndex 骨架兼容层测试（对应原 SkeletonTree 测试）。"""

    def test_scan_discovers_all_files(self, index):
        """扫描能发现所有 .py 文件的文件节点。"""
        assert index.get_node("utils.py") is not None
        assert index.get_node("main.py") is not None
        assert index.get_node("sub/module.py") is not None

    def test_extract_top_level_functions(self, index):
        """提取顶层函数。"""
        funcs = index.get_file_functions("utils.py")
        names = {n.name for n in funcs if "." not in n.name}
        assert "helper_func" in names
        assert "another_func" in names

    def test_extract_class_methods(self, index):
        """提取类内方法。"""
        funcs = index.get_file_functions("utils.py")
        methods = [n.name for n in funcs if n.name.startswith("MyClass.")]
        assert "MyClass.method_a" in methods
        assert "MyClass.method_b" in methods

    def test_generate_skeleton_format(self, index):
        """骨架文本格式与 SkeletonTree 一致。"""
        text = index.generate_skeleton_text()
        assert "=== utils.py ===" in text
        assert "=== main.py ===" in text
        assert "=== sub/module.py ===" in text
        assert "helper_func" in text
        assert "MyClass.method_a" in text

    def test_expand_function(self, index):
        """展开函数源码。"""
        source = index.expand_function("utils.py", "helper_func")
        assert source is not None
        assert "def helper_func(x, y):" in source
        assert "return x + y" in source

    def test_expand_class_method(self, index):
        """展开类内方法。"""
        source = index.expand_function("utils.py", "MyClass.method_b")
        assert source is not None
        assert "def method_b(self, arg):" in source
        assert "return arg * 2" in source

    def test_expand_nonexistent_function(self, index):
        """展开不存在的函数返回 None。"""
        assert index.expand_function("utils.py", "nonexistent") is None

    def test_expand_nonexistent_file(self, index):
        """展开不存在的文件返回 None。"""
        assert index.expand_function("nonexistent.py", "func") is None

    def test_search_function(self, index):
        """搜索函数（模糊匹配）。"""
        results = index.search_nodes("helper")
        assert any(n.node_id == "utils.py::helper_func" for n in results)

    def test_search_function_case_insensitive(self, index):
        """搜索函数（大小写不敏感）。"""
        results = index.search_nodes("HELPER")
        assert any(n.node_id == "utils.py::helper_func" for n in results)

    def test_syntax_error_handling(self, tmp_path):
        """语法错误文件仍生成文件节点，不崩溃。"""
        tmpdir = tmp_path / "bad_proj"
        tmpdir.mkdir()
        (tmpdir / "bad.py").write_text("def broken(\n", encoding="utf-8")

        mgr = GraphManager(str(tmpdir), graph_dir=str(tmp_path / "g"))
        index = mgr.build(force=True)
        assert index.get_node("bad.py") is not None

    def test_skips_hidden_dirs(self, tmp_path):
        """跳过隐藏目录和 __pycache__。"""
        normal = tmp_path / "normal"
        normal.mkdir()
        (normal / "a.py").write_text("def func_a(): pass\n", encoding="utf-8")
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        (hidden / "b.py").write_text("def func_b(): pass\n", encoding="utf-8")
        cache = tmp_path / "__pycache__"
        cache.mkdir()
        (cache / "c.py").write_text("def func_c(): pass\n", encoding="utf-8")

        mgr = GraphManager(str(tmp_path), graph_dir=str(tmp_path / "g"))
        index = mgr.build(force=True)
        assert index.get_node("normal/a.py") is not None
        assert index.get_node(".hidden/b.py") is None
        assert index.get_node("__pycache__/c.py") is None

    def test_get_file_functions(self, index):
        """获取指定文件的函数列表。"""
        funcs = index.get_file_functions("utils.py")
        # 2 顶层函数 + 2 类方法
        assert len(funcs) == 4

    def test_relative_path_expand(self, index):
        """使用相对路径展开函数。"""
        source = index.expand_function("utils.py", "helper_func")
        assert "def helper_func(x, y):" in source
