import os
import tempfile
import pytest
from swe_agent.sandbox.docker_runner import run_in_docker, is_dangerous_command, ExecutionResult


@pytest.fixture
def sample_code_dir():
    """创建一个临时代码目录，包含一个简单的 Python 文件。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        code = 'print("hello from sandbox")\n'
        with open(os.path.join(tmpdir, "test.py"), "w") as f:
            f.write(code)
        yield tmpdir


@pytest.fixture
def error_code_dir():
    """创建一个包含语法错误代码的临时目录。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        code = 'def foo(\n    pass\n'
        with open(os.path.join(tmpdir, "bad.py"), "w") as f:
            f.write(code)
        yield tmpdir


class TestIsDangerousCommand:
    def test_normal_commands_are_safe(self):
        assert is_dangerous_command("python test.py") is False
        assert is_dangerous_command("echo hello") is False
        assert is_dangerous_command("ls -la") is False

    def test_rm_rf_root_is_dangerous(self):
        assert is_dangerous_command("rm -rf /") is True
        assert is_dangerous_command("rm -rf /*") is True

    def test_sudo_is_dangerous(self):
        assert is_dangerous_command("sudo apt install foo") is True

    def test_chmod_777_is_dangerous(self):
        assert is_dangerous_command("chmod 777 /etc/passwd") is True

    def test_fork_bomb_is_dangerous(self):
        assert is_dangerous_command(":(){:|:&};:") is True


class TestRunInDocker:
    def test_basic_execution(self, sample_code_dir):
        result = run_in_docker(sample_code_dir, "python test.py")
        assert result.exit_code == 0
        assert "hello from sandbox" in result.stdout

    def test_returns_exit_code(self, error_code_dir):
        result = run_in_docker(error_code_dir, "python bad.py")
        assert result.exit_code != 0
        assert result.stderr != ""

    def test_dangerous_command_rejected(self, sample_code_dir):
        result = run_in_docker(sample_code_dir, "rm -rf /")
        assert result.exit_code == -1
        assert "危险命令" in result.stderr

    def test_nonexistent_dir(self):
        result = run_in_docker("/nonexistent/path", "echo hello")
        assert result.exit_code == -1
        assert "不存在" in result.stderr

    def test_result_is_execution_result(self, sample_code_dir):
        result = run_in_docker(sample_code_dir, "echo hello")
        assert isinstance(result, ExecutionResult)
        assert hasattr(result, "stdout")
        assert hasattr(result, "stderr")
        assert hasattr(result, "exit_code")

    def test_read_only_filesystem(self, sample_code_dir):
        """验证容器内文件系统是只读的。"""
        result = run_in_docker(sample_code_dir, "touch /workspace/newfile.txt")
        assert result.exit_code != 0

    def test_network_disabled(self, sample_code_dir):
        """验证容器内网络被禁用。"""
        result = run_in_docker(sample_code_dir, "python -c \"import urllib.request; urllib.request.urlopen('http://example.com')\"")
        assert result.exit_code != 0
