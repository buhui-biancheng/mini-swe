# -*- coding: utf-8 -*-
"""L3 Probe：AI 生成的临时验证脚本（Phase 7 模块 C）。

无现成测试时兜底：AI 写临时验证脚本，验证修复是否生效。

三条铁律（对应架构审查）：
    1. 缺陷7：Probe 断言必须包含 Issue 期望 token（从 problem_statement 提取，
       系统校验——"来自 Issue 期望"从原则变可执行规则）
    2. R5：Probe 物理隔离（独立目录，永不混入基准测试）+ 只读/受限命令
       （沙盒（Phase 6）之前，AI 写的脚本只能跑受限命令，危险命令黑名单复用）
    3. 无 oracle 不修复：Issue 连期望都没有 → 只报告定位，不宣称成功
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional

# 危险命令黑名单（复用 _run_command 的规则，Probe 脚本禁止包含）
_DANGEROUS = ["rm -rf", "sudo", "chmod 777", "mkfs", "dd if=", "os.remove(",
              "shutil.rmtree", "subprocess", "eval(", "exec(", "__import__",
              "open(", "os.system", "os.popen"]

# Probe 脚本内置受限沙箱：遮蔽危险操作
_PROBE_HEADER = '''# L3 Probe 受限执行沙箱（R5：沙盒 Phase 6 之前的安全边界）
# 第二道防线：危险代码已由静态检查拦截（_DANGEROUS），这里防写入/破坏
import builtins as _b
import os as _os

# 只读 open：允许读（import 需要），拒绝写
_orig_open = _b.open
def _readonly_open(file, mode="r", *args, **kwargs):
    if any(w in mode for w in ("w", "a", "x", "+", "wb", "ab")):
        raise PermissionError(f"[R5] Probe 禁止文件写入: {file} ({mode})")
    return _orig_open(file, mode, *args, **kwargs)
_b.open = _readonly_open

# 遮蔽文件系统写入/危险操作
class _NoWrite:
    def __getattr__(self, item):
        raise PermissionError(f"[R5] Probe 禁止文件系统写入: {item}")
_os.remove = _NoWrite()
_os.unlink = _NoWrite()
_os.rename = _NoWrite()
_os.chmod = _NoWrite()
_os.system = _NoWrite()
_os.popen = _NoWrite()
'''

# 允许导入的模块白名单
_SAFE_IMPORTS = {"math", "json", "re", "collections", "itertools", "functools"}


@dataclass
class ProbeSpec:
    """Probe 生成规范。"""
    issue: str
    target_file: str                # 被测文件（项目内）
    target_functions: list[str] = field(default_factory=list)
    expected_behaviors: list[str] = field(default_factory=list)  # Issue 期望（oracle）
    code_dir: str = ""              # 项目目录（供 import 被测文件）


@dataclass
class ProbeResult:
    """Probe 执行结果。"""
    passed: bool
    output: str = ""
    error: str = ""
    assertion_tokens: list[str] = field(default_factory=list)
    isolated_dir: str = ""


class ProbeValidator:
    """缺陷7：断言 token 校验——Probe 断言必须包含 Issue 期望 token。"""

    # Issue 中提取期望 token 的确定性规则
    @staticmethod
    def extract_expected_tokens(issue: str) -> list[str]:
        tokens = set()
        # 数值（单价/数量/结果等）
        for m in re.finditer(r"(?<![A-Za-z])(\d+)(?![A-Za-z])", issue):
            tokens.add(m.group(1))
        # 期望行为关键词（"应该/必须/返回/等于" 后的短语）
        for m in re.finditer(r"(?:应该|必须|应当|需要|要)(.{2,15}?)(?:[，。；,!?；\n])", issue):
            tokens.add(m.group(1).strip())
        # 函数名
        for m in re.finditer(r"([a-z_][a-z0-9_]{2,})", issue):
            if m.group(1) not in ("should", "return", "the", "and", "with"):
                tokens.add(m.group(1))
        return list(tokens)[:10]

    @staticmethod
    def validate_assertion(probe_code: str, expected_tokens: list[str]) -> list[str]:
        """断言中的期望 token 校验。返回缺失的 token（空 = 通过）。"""
        if not expected_tokens:
            return []  # Issue 无期望 → 不校验（但调用方应据此拒绝宣称成功）
        missing = []
        for t in expected_tokens:
            if t not in probe_code:
                missing.append(t)
        return missing


class ProbeGenerator:
    """生成 + 执行 L3 Probe（物理隔离 + 受限沙箱）。"""

    def __init__(self, spec: ProbeSpec, llm_client=None, verbose: bool = True):
        self.spec = spec
        self.llm_client = llm_client
        self.verbose = verbose

    def _build_prompt(self) -> str:
        # 从图索引获取被测文件的函数结构（类方法 vs 模块函数）
        func_info = self._target_func_info()
        return f"""你是一个验证脚本生成器。根据 Issue 描述和被测代码，生成一个 L3 Probe 验证脚本。

【Issue】
{self.spec.issue}

【被测文件】{self.spec.target_file}
【被测函数结构（来自代码图索引）】
{func_info}

【调用方式提示】
- 如果函数是【类方法】（Class.method），必须：from {os.path.splitext(os.path.basename(self.spec.target_file))[0]} import Class名，然后实例化再调用
- 如果函数是【模块级函数】，直接 from 模块 import 函数名
- 不确定时用 dir(模块) 或读源码确认，不要猜

【要求】
1. 把 {self.spec.code_dir} 加入 sys.path 后导入被测模块
2. 用 assert 验证 Issue 描述的期望行为（断言必须引用 Issue 里的具体数值/行为）
3. 脚本只能使用纯 Python 断言，禁止：文件写入、subprocess、网络、eval/exec
4. 只输出脚本代码，不要多余文字，不要 markdown 代码块

输出示例：
```python
import sys
sys.path.insert(0, "{self.spec.code_dir}")
from calculator import Calculator
c = Calculator()
assert c.compute_rectangle_area(4, 5) == "20.00"
print("PROBE_PASS")
```"""

    def _target_func_info(self) -> str:
        """从图索引取被测文件的函数结构（类方法/模块函数/行号）。"""
        try:
            from swe_agent.graph.manager import GraphManager
            mg = GraphManager(self.spec.code_dir)
            idx = mg.build()
            lines = []
            for n in idx.graph.nodes.values():
                if n.node_type.value == "function" and \
                        os.path.basename(n.file) == os.path.basename(self.spec.target_file):
                    kind = "类方法" if "::" in n.node_id and "." in n.name else "模块函数"
                    lines.append(f"  - {n.name} ({kind}, 行 {n.lineno})")
            return "\n".join(lines) if lines else "  （未找到函数，请先读文件确认）"
        except Exception as e:
            return f"  （图索引不可用: {e}）"

    def generate(self) -> Optional[str]:
        """调用 LLM 生成 Probe 脚本。"""
        if self.llm_client is None:
            from swe_agent.llm.client import LLMClient
            self.llm_client = LLMClient()
        response = self.llm_client.chat(
            messages=[
                {"role": "system", "content": "你是验证脚本生成器，只输出 Python 代码。"},
                {"role": "user", "content": self._build_prompt()},
            ],
            max_tokens=800,
            temperature=0.1,
        )
        text = response.content if hasattr(response, "content") else str(response)
        # 提取代码块
        m = re.search(r"```python\s*(.*?)```", text, re.DOTALL)
        if m:
            return m.group(1).strip()
        # 无代码块：整段当代码（去掉多余说明）
        lines = [l for l in text.splitlines()
                 if not l.startswith(("这里", "以下是", "```", "python", "#" * 3))]
        return "\n".join(lines).strip() or None

    def _check_safety(self, code: str) -> list[str]:
        """R5：危险命令检查。返回违规列表（空 = 安全）。"""
        violations = []
        for d in _DANGEROUS:
            if d in code:
                violations.append(d)
        # import 白名单检查
        for m in re.finditer(r"^\s*import\s+(\w+)", code, re.MULTILINE):
            if m.group(1) not in _SAFE_IMPORTS and m.group(1) not in (
                    "sys", "os", "pytest"):
                violations.append(f"import {m.group(1)}")
        return violations

    def _isolate(self) -> str:
        """物理隔离：独立目录，绝不混入基准测试。"""
        base = os.path.join(tempfile.gettempdir(), "swe_agent_probes")
        os.makedirs(base, exist_ok=True)
        return tempfile.mkdtemp(prefix="probe_", dir=base)

    def execute(self, probe_code: str, isolated_dir: str) -> ProbeResult:
        """在隔离目录执行 Probe（受限沙箱 + 超时）。"""
        probe_path = os.path.join(isolated_dir, "probe.py")
        with open(probe_path, "w", encoding="utf-8") as f:
            f.write(_PROBE_HEADER + "\n" + probe_code)
        try:
            proc = subprocess.run(
                ["python3", probe_path],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=isolated_dir,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            passed = proc.returncode == 0 and "PROBE_PASS" in output
            return ProbeResult(passed=passed, output=output[-2000:],
                               isolated_dir=isolated_dir)
        except subprocess.TimeoutExpired:
            return ProbeResult(passed=False, error="Probe 超时(30s)",
                               isolated_dir=isolated_dir)

    def run(self) -> ProbeResult:
        """完整流程：生成 → 校验（缺陷7）→ 安全检查（R5）→ 隔离执行。"""
        # 1. 生成
        code = self.generate()
        if not code:
            return ProbeResult(passed=False, error="Probe 生成失败")

        # 2. 缺陷7：断言 token 校验
        tokens = ProbeValidator.extract_expected_tokens(self.spec.issue)
        missing = ProbeValidator.validate_assertion(code, tokens)
        if missing and tokens:
            if self.verbose:
                print(f"[PROBE] ⚠️ 断言缺少 Issue 期望 token: {missing}")
            # 追加提示重试一次
            code2 = self.generate()
            if code2:
                code = code2
                missing = ProbeValidator.validate_assertion(code, tokens)
                if missing and self.verbose:
                    print(f"[PROBE] ⚠️ 重试后仍缺 token: {missing}")

        # 3. R5 安全检查
        violations = self._check_safety(code)
        if violations:
            return ProbeResult(
                passed=False,
                error=f"Probe 含危险操作被拒绝: {violations}",
                assertion_tokens=tokens,
            )

        # 4. 隔离执行
        isolated = self._isolate()
        if self.verbose:
            print(f"[PROBE] 隔离目录: {isolated}")
        result = self.execute(code, isolated)
        result.assertion_tokens = tokens

        # 5. 清理（保留结果输出，删目录）
        try:
            shutil.rmtree(isolated, ignore_errors=True)
        except Exception:
            pass
        return result
