import docker
import os
import sys
import hashlib
import tempfile
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: int


DANGEROUS_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "sudo ",
    "chmod 777",
    "mkfs",
    "dd if=",
    ":(){:|:&};:",
    "> /dev/sda",
    "shutdown",
    "reboot",
    "halt",
    "init 0",
]


# ========== 容器复用池（2026-08-13）：常驻容器 + exec——省容器启动 + 减少 docker 操作 ==========
import threading as _threading
_CONTAINER_POOL: dict = {}
_POOL_LOCK = _threading.Lock()
_DOCKER_CLIENT = None


def _get_docker_client():
    """缓存 docker client（2026-08-13：版本探测只一次——评测中重复 from_env 曾触发
    'maximum recursion depth exceeded'）。"""
    global _DOCKER_CLIENT
    if _DOCKER_CLIENT is None:
        import docker as _docker
        _DOCKER_CLIENT = _docker.from_env()
    return _DOCKER_CLIENT


def _pool_cleanup():
    """进程退出时清理常驻容器（2026-08-13：堆积会导致 daemon 过载递归超限）。"""
    for _cid in list(_CONTAINER_POOL.values()):
        try:
            _cid.remove(force=True)
        except Exception:
            pass
    _CONTAINER_POOL.clear()


import atexit as _atexit
_atexit.register(_pool_cleanup)


def _pool_get(image: str, code_dir: str, network: bool, links, timeout: int):
    """获取（或创建）常驻容器——exec 模式复用。"""
    _client = _get_docker_client()
    key = (image, os.path.abspath(code_dir))
    with _POOL_LOCK:
        c = _CONTAINER_POOL.get(key)
        if c is not None:
            try:
                c.reload()
                if c.status == "running":
                    return c
            except Exception:
                pass
        container = _client.containers.run(
            image=image,
            command=["sleep", "infinity"],
            volumes={os.path.abspath(code_dir): {"bind": "/workspace", "mode": "ro"}},
            working_dir="/workspace",
            mem_limit="1g",
            network_disabled=not network,
            links=[tuple(lnk.split(":", 1)) for lnk in (links or [])] or None,
            read_only=True,
            privileged=False,
            tmpfs={"/tmp": "size=200m"},
            detach=True,
            stderr=True,
            stdout=True,
        )
        _CONTAINER_POOL[key] = container
        return container


def _pool_exec(container, command: str, timeout: int):
    """exec 跑命令（线程超时——SDK exec_run 无 timeout）。超时 → 容器重建。"""
    import docker as _docker
    result = {}

    def _run():
        try:
            res = container.exec_run(["sh", "-c", command], workdir="/workspace")
            result["exit"] = res.exit_code
            out = res.output
            result["out"] = out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)
        except Exception as e:
            result["err"] = str(e)

    _th = _threading.Thread(target=_run, daemon=True)
    _th.start()
    _th.join(timeout)
    if _th.is_alive():
        try:
            container.remove(force=True)
            _CONTAINER_POOL.pop(next((k for k, v in _CONTAINER_POOL.items() if v.id == container.id), None), None)
        except Exception:
            pass
        return ExecutionResult(stdout="", stderr="容器执行超时（容器已重建）", exit_code=-1)
    if "err" in result:
        return ExecutionResult(stdout="", stderr=result["err"], exit_code=-1)
    return ExecutionResult(stdout=result.get("out", ""), stderr="", exit_code=result.get("exit", -1))


def is_dangerous_command(command: str) -> bool:
    cmd_lower = command.lower().strip()
    for pattern in DANGEROUS_COMMANDS:
        if pattern.lower() in cmd_lower:
            return True
    return False


def _image_tag(python_version: str, packages: list[str]) -> str:
    """根据 Python 版本和包列表生成唯一的镜像标签。"""
    pkg_str = ",".join(sorted(packages))
    short_hash = hashlib.md5(f"{python_version}:{pkg_str}".encode()).hexdigest()[:8]
    return f"swe-agent:py{python_version}-{short_hash}"


def _host_python_version() -> str:
    """获取宿主机 Python 版本的主次版本号，如 '3.12'。"""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _check_packages_in_image(image: str, packages: list[str]) -> bool:
    """检查镜像中是否已安装指定的包。"""
    client = docker.from_env()
    try:
        pkg_check = " ".join(packages)
        container = client.containers.run(
            image=image,
            command=["python3", "-c", f"import {', '.join(packages)}"],
            detach=True,
            stderr=True,
            stdout=True,
        )
        result = container.wait(timeout=10)
        exit_code = result.get("StatusCode", -1)
        container.remove(force=True)
        return exit_code == 0
    except Exception:
        return False


def ensure_image(
    python_version: str | None = None,
    packages: list[str] | None = None,
) -> str:
    """确保 Docker 镜像可用，自动检测和构建。

    策略：
    1. 指定了 python_version → 用该版本，不存在则自动构建
    2. 未指定 → 先检测 swe-agent-sandbox 是否可用
    3. 不可用 → 自动检测宿主机 Python 版本并构建

    Args:
        python_version: Python 版本，如 "3.11"、"3.12"，None 表示自动检测
        packages: 需要预装的包列表，如 ["pytest", "numpy"]

    Returns:
        可用的镜像标签
    """
    if packages is None:
        packages = ["pytest"]

    client = _get_docker_client()

    # 策略 1：指定了版本 → 直接用
    if python_version is not None:
        tag = _image_tag(python_version, packages)
        try:
            client.images.get(tag)
            return tag
        except docker.errors.ImageNotFound:
            return _build_image(python_version, packages)

    # 策略 2：未指定 → 先试 swe-agent-sandbox
    try:
        client.images.get("swe-agent-sandbox")
        if _check_packages_in_image("swe-agent-sandbox", packages):
            return "swe-agent-sandbox"
    except docker.errors.ImageNotFound:
        pass

    # 策略 3：swe-agent-sandbox 不可用 → 用宿主机 Python 版本自动构建
    detected_version = _host_python_version()
    tag = _image_tag(detected_version, packages)
    try:
        client.images.get(tag)
        return tag
    except docker.errors.ImageNotFound:
        return _build_image(detected_version, packages)


def _build_image(python_version: str, packages: list[str]) -> str:
    """自动构建 Docker 镜像。"""
    tag = _image_tag(python_version, packages)
    pkg_line = " ".join(packages)
    dockerfile_content = f"""\
FROM python:{python_version}-slim
RUN pip install --no-cache-dir {pkg_line}
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        dockerfile_path = os.path.join(tmpdir, "Dockerfile")
        with open(dockerfile_path, "w") as f:
            f.write(dockerfile_content)

        print(f"[SANDBOX] 自动构建镜像 {tag} (Python {python_version}, 包: {pkg_line})")
        client = docker.from_env()
        client.images.build(path=tmpdir, tag=tag, rm=True)
        print(f"[SANDBOX] 镜像构建完成: {tag}")

    return tag


def run_in_docker(
    code_dir: str,
    command: str,
    image: str | None = None,
    python_version: str | None = None,
    packages: list[str] | None = None,
    timeout: int = 60,
    network: bool = False,
    links: Optional[list] = None,
    reuse: bool = False,
) -> ExecutionResult:
    """在隔离的 Docker 容器中执行命令。

    Args:
        code_dir: 要挂载到容器中的代码目录路径
        command: 要在容器内执行的命令
        image: 指定镜像名称（为 None 时自动检测/构建）
        python_version: 指定 Python 版本（为 None 时自动检测）
        packages: 需要预装的包（默认 ["pytest"]）
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

    # 自动确保镜像可用
    if image is None:
        image = ensure_image(python_version, packages)

    client = _get_docker_client()

    # 2026-08-13 容器复用：常驻容器 + exec（省启动 + 少 docker 操作）
    if reuse:
        try:
            _c = _pool_get(image, code_dir, network, links, timeout)
            return _pool_exec(_c, command, timeout)
        except Exception as _e:
            return ExecutionResult(stdout="", stderr=f"容器复用失败（回退新建）: {_e}", exit_code=-1)

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
            network_disabled=not network,
            links=[tuple(lnk.split(":", 1)) for lnk in (links or [])] or None,
            read_only=True,
            privileged=False,
            tmpfs={"/tmp": "size=200m"},
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
