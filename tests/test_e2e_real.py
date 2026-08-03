"""真实 LLM 冒烟测试（标记 slow，需要真实 API key + Docker）。

默认不运行（pytest.ini addopts: -m "not slow"）。
手动运行：python3 -m pytest tests/test_e2e_real.py -m slow -v

覆盖：真实 DeepSeek + Docker 环境下，FSM 完整修复跨文件 bug
（症状在 cart.py，根因在 pricing.py）——验证图引导跨文件追踪 + Phase 2
全流程（日志解析器/提示词分级/回滚/降级）在真实模型下的兼容性。

⚠️ 会真实调用 LLM API（产生少量费用）并启动 Docker 容器。
"""

import os
import shutil

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E_SRC = os.path.join(REPO_ROOT, "examples", "e2e_multifile")


@pytest.fixture(scope="module")
def real_fsm(tmp_path_factory):
    """复制 demo 项目到临时目录（避免真实修复污染 examples/ 源文件），跑完整 FSM。"""
    from swe_agent.fsm.agent_fsm import AgentFSM

    tmp = tmp_path_factory.mktemp("e2e_real")
    shutil.copytree(E2E_SRC, tmp, dirs_exist_ok=True)
    bug_file = str(tmp / "cart.py")
    fsm = AgentFSM(
        bug_file=bug_file,
        test_command="pytest test_cart.py -v",
        max_retries=1,
        mode="dp",
    )
    fsm.run()
    return fsm, tmp


class TestRealE2E:
    def test_real_multifile_fix(self, real_fsm):
        """真实环境修复跨文件 bug：症状在 cart，根因在 pricing。"""
        fsm, _ = real_fsm
        assert fsm.state == "success", f"真实 E2E 失败，最终状态={fsm.state}"

    def test_real_fix_passes_tests(self, real_fsm):
        """修复后测试真实通过（Docker 再跑一次确认）。"""
        _, tmp = real_fsm
        from swe_agent.sandbox.docker_runner import run_in_docker

        result = run_in_docker(str(tmp), "pytest test_cart.py -v")
        assert result.exit_code == 0, (
            f"修复后测试仍失败: {result.stdout[-500:]}"
        )
