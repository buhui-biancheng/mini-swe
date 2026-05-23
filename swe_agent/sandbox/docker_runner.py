import docker
import os
import tempfile
import shutil
from dataclasses import dataclass


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


# 危险命令黑名单
DANGEROUS_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "sudo ",
    "chmod 777",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",  # fork bomb
    "> /dev/sda",
    "shutdown",
    "reboot",
    "halt",
    "init 0",
]


def is_dangerous_command(command: str) -> bool:
    cmd_lower = command.lower().strip()
    for pattern in DANGEROUS_COMMANDS:
        if pattern.lower() in cmd_lower:
            return True
    return False


def run_in_docker(
    code_dir: str,
    command: str,
    image: str = "python:3.11-slim",
    timeout: int = 60,
) -> ExecutionResult:
    """在隔离的 Docker 容器中执行命令。

    Args:
        code_dir: 要挂载到容器中的代码目录路径
        command: 要在容器内执行的命令
        image: Docker 镜像名称
        timeout: 命令执行超时时间（秒）

    Returns:
        ExecutionResult 包含 stdout, stderr, exit_code
    """
    if is_dangerous_command(command):
        return ExecutionResult(
            stdout="",
            stderr=f"命令被拒绝：检测到危险命令模式 '{command}'",
            exit_code=-1,
        )

    if not os.path.isdir(code_dir):
        return ExecutionResult(
            stdout="",
            stderr=f"代码目录不存在：{code_dir}",
            exit_code=-1,
        )

    client = docker.from_env()

    try:
        container = client.containers.run(
            image=image,
            command=["sh", "-c", command],
            volumes={
                os.path.abspath(code_dir): {
                    "bind": "/workspace",
                    "mode": "ro",
                }
            },
            working_dir="/workspace",
            mem_limit="1g",
            network_disabled=True,
            read_only=True,
            privileged=False,
            detach=True,
            stderr=True,
            stdout=True,
        )

        try:
            result = container.wait(timeout=timeout)
            exit_code = result.get("StatusCode", -1)
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
        except Exception as e:
            stdout = ""
            stderr = f"容器执行超时或异常：{e}"
            exit_code = -1
        finally:
            try:
                container.remove(force=True)
            except Exception:
                pass

        return ExecutionResult(stdout=stdout, stderr=stderr, exit_code=exit_code)

    except docker.errors.ImageNotFound:
        return ExecutionResult(
            stdout="",
            stderr=f"Docker 镜像未找到：{image}",
            exit_code=-1,
        )
    except docker.errors.APIError as e:
        return ExecutionResult(
            stdout="",
            stderr=f"Docker API 错误：{e}",
            exit_code=-1,
        )
    except Exception as e:
        return ExecutionResult(
            stdout="",
            stderr=f"未知错误：{e}",
            exit_code=-1,
        )
