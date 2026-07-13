"""SkeletonTree 单元测试。"""

import os
import tempfile
import shutil
import pytest
from swe_agent.ast_view.skeleton import SkeletonTree, FunctionInfo, FileSkeleton


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


class TestSkeletonTree:
    """SkeletonTree 测试类。"""

    def test_scan_discovers_all_files(self, sample_project):
        """测试扫描能发现所有 .py 文件。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        assert len(tree._file_skeletons) == 3  # utils.py, main.py, sub/module.py

    def test_extract_top_level_functions(self, sample_project):
        """测试提取顶层函数。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")
        skeleton = tree._file_skeletons[utils_path]

        func_names = [f.name for f in skeleton.functions if f.class_name is None]
        assert "helper_func" in func_names
        assert "another_func" in func_names

    def test_extract_class_methods(self, sample_project):
        """测试提取类内方法。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")
        skeleton = tree._file_skeletons[utils_path]

        methods = [f for f in skeleton.functions if f.class_name == "MyClass"]
        method_names = [m.name for m in methods]
        assert "method_a" in method_names
        assert "method_b" in method_names

    def test_generate_skeleton_format(self, sample_project):
        """测试骨架文本格式。"""
        tree = SkeletonTree(sample_project)
        tree.scan()
        text = tree.generate_skeleton()

        assert "=== utils.py ===" in text
        assert "=== main.py ===" in text
        assert "=== sub/module.py ===" in text
        assert "helper_func" in text
        assert "MyClass.method_a" in text

    def test_expand_function(self, sample_project):
        """测试展开函数源码。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")
        source = tree.expand_function(utils_path, "helper_func")

        assert "def helper_func(x, y):" in source
        assert "return x + y" in source

    def test_expand_class_method(self, sample_project):
        """测试展开类内方法。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")
        source = tree.expand_function(utils_path, "MyClass.method_b")

        assert "def method_b(self, arg):" in source
        assert "return arg * 2" in source

    def test_expand_nonexistent_function(self, sample_project):
        """测试展开不存在的函数。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")
        result = tree.expand_function(utils_path, "nonexistent")

        assert "[ERROR]" in result
        assert "未找到" in result

    def test_expand_nonexistent_file(self, sample_project):
        """测试展开不存在的文件。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        result = tree.expand_function("nonexistent.py", "func")

        assert "[ERROR]" in result

    def test_search_function(self, sample_project):
        """测试搜索函数（模糊匹配）。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        results = tree.search_function("helper")
        assert len(results) == 1
        assert results[0].name == "helper_func"

    def test_search_function_case_insensitive(self, sample_project):
        """测试搜索函数（大小写不敏感）。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        results = tree.search_function("HELPER")
        assert len(results) == 1

    def test_lru_cache(self, sample_project):
        """测试 LRU 缓存机制。"""
        tree = SkeletonTree(sample_project, max_cache_size=2)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")

        # 展开两个函数
        tree.expand_function(utils_path, "helper_func")
        tree.expand_function(utils_path, "another_func")
        assert len(tree._expand_cache) == 2

        # 展开第三个，应该淘汰第一个
        tree.expand_function(utils_path, "MyClass.method_a")
        assert len(tree._expand_cache) == 2
        assert f"{utils_path}::helper_func" not in tree._expand_cache

    def test_syntax_error_handling(self):
        """测试语法错误文件处理。"""
        tmpdir = tempfile.mkdtemp()
        bad_file = os.path.join(tmpdir, "bad.py")
        with open(bad_file, "w") as f:
            f.write("def broken(\n")  # 语法错误

        tree = SkeletonTree(tmpdir)
        tree.scan()

        skeleton = tree._file_skeletons[bad_file]
        assert skeleton.error is not None

        shutil.rmtree(tmpdir)

    def test_skips_hidden_dirs(self):
        """测试跳过隐藏目录。"""
        tmpdir = tempfile.mkdtemp()

        # 正常目录
        normal_dir = os.path.join(tmpdir, "normal")
        os.makedirs(normal_dir)
        with open(os.path.join(normal_dir, "a.py"), "w") as f:
            f.write("def func_a(): pass")

        # 隐藏目录
        hidden_dir = os.path.join(tmpdir, ".hidden")
        os.makedirs(hidden_dir)
        with open(os.path.join(hidden_dir, "b.py"), "w") as f:
            f.write("def func_b(): pass")

        # __pycache__ 目录
        cache_dir = os.path.join(tmpdir, "__pycache__")
        os.makedirs(cache_dir)
        with open(os.path.join(cache_dir, "c.py"), "w") as f:
            f.write("def func_c(): pass")

        tree = SkeletonTree(tmpdir)
        tree.scan()

        assert len(tree._file_skeletons) == 1
        assert os.path.join(normal_dir, "a.py") in tree._file_skeletons

        shutil.rmtree(tmpdir)

    def test_get_file_functions(self, sample_project):
        """测试获取指定文件的函数列表。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        utils_path = os.path.join(sample_project, "utils.py")
        functions = tree.get_file_functions(utils_path)

        assert len(functions) == 4  # 2 top-level + 2 methods

    def test_relative_path_expand(self, sample_project):
        """测试使用相对路径展开函数。"""
        tree = SkeletonTree(sample_project)
        tree.scan()

        source = tree.expand_function("utils.py", "helper_func")
        assert "def helper_func(x, y):" in source

    def test_function_info_full_name(self):
        """测试 FunctionInfo.full_name 属性。"""
        # 顶层函数
        func = FunctionInfo(name="test", file_path="/a.py", start_line=1, end_line=5)
        assert func.full_name == "test"

        # 类内方法
        method = FunctionInfo(
            name="method", file_path="/a.py", start_line=1, end_line=5, class_name="MyClass"
        )
        assert method.full_name == "MyClass.method"
