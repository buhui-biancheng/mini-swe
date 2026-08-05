# -*- coding: utf-8 -*-
"""缺陷2：测试→文件 覆盖映射（Phase 7 模块 D）。

问题：_find_test_file 是文件名启发式，裁剪回归可能漏测 → 假阳性 SUCCESS。
方案：对每个测试文件单独跑 pytest --cov-report=json，
      记录 测试文件 → 覆盖的被测文件 映射（确定性，不靠文件名猜）。
      L2 裁剪回归 = 只跑覆盖被改文件的测试。
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CoverageMap:
    """测试文件→被测文件 覆盖映射（持久化到 .graph/coverage.json）。"""

    code_dir: str
    data: dict = field(default_factory=dict)   # {测试文件: [被测文件...]}
    _loaded: bool = False

    @property
    def path(self) -> str:
        return os.path.join(self.code_dir, ".graph", "coverage.json")

    def load(self) -> bool:
        if self._loaded:
            return bool(self.data)
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                self._loaded = True
                return True
            except Exception:
                pass
        self._loaded = True
        return False

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    # ── 捕获 ──
    def discover_test_files(self) -> list[str]:
        """发现测试文件（确定性规则，与 GraphIndex.is_test_file 一致）。"""
        result = []
        for root, dirs, files in os.walk(self.code_dir):
            # 跳过隐藏目录/.git/.graph/venv
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in (".git", ".graph", "venv", "__pycache__",
                                     "node_modules", "third_party")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                if f.startswith("test_") or f.endswith("_test.py") \
                        or "tests" in root.replace("\\", "/").split("/"):
                    result.append(os.path.relpath(os.path.join(root, f), self.code_dir))
        return sorted(result)

    def capture(self, test_files: Optional[list[str]] = None, timeout: int = 120) -> dict:
        """逐测试文件捕获覆盖。

        Args:
            test_files: 指定测试文件（None = 自动发现）
            timeout: 单文件超时

        Returns:
            {"mapped": {测试文件: [被测文件]}, "failed": [失败文件]}
        """
        if test_files is None:
            test_files = self.discover_test_files()
        mapped: dict = {}
        failed = []
        for tf in test_files:
            covered = self._capture_one(tf, timeout)
            if covered is None:
                failed.append(tf)
            else:
                mapped[tf] = covered
        if mapped:
            self.data.update(mapped)
            self.save()
        return {"mapped": mapped, "failed": failed}

    def _capture_one(self, test_file: str, timeout: int) -> Optional[list[str]]:
        """跑单个测试文件，返回其覆盖的被测文件列表。"""
        try:
            env = dict(os.environ)
            cov_file = os.path.join(self.code_dir, ".graph", ".coverage")
            env["COVERAGE_FILE"] = cov_file
            cmd = f"python3 -m pytest {test_file} --cov --cov-report=json -q"
            proc = subprocess.run(
                cmd, shell=True, cwd=self.code_dir, env=env,
                capture_output=True, text=True, timeout=timeout,
            )
            cov_json = os.path.join(self.code_dir, "coverage.json")
            if not os.path.exists(cov_json):
                return None
            with open(cov_json, "r", encoding="utf-8") as f:
                cov = json.load(f)
            os.remove(cov_json)
            if os.path.exists(cov_file):
                os.remove(cov_file)
            # 覆盖到的被测文件 = 有 executed_lines 的非测试文件
            covered = []
            for path, info in cov.get("files", {}).items():
                if not path.endswith(".py"):
                    continue
                if not info.get("executed_lines"):
                    continue
                base = os.path.basename(path)
                if base.startswith("test_") or base.endswith("_test.py"):
                    continue
                covered.append(path.replace("\\", "/"))
            return sorted(set(covered))
        except Exception:
            return None

    # ── 查询 ──
    def tests_covering_file(self, file_path: str) -> list[str]:
        """返回覆盖某文件的测试文件列表。"""
        self.load()
        base = os.path.basename(file_path)
        result = []
        for test_file, covered in self.data.items():
            for c in covered:
                if os.path.basename(c) == base:
                    result.append(test_file)
                    break
        return result

    def prune_command(self, test_command: str, edited_files: list[str]) -> str:
        """L2 裁剪：只跑覆盖被改文件的测试（无覆盖信息 → 原命令全量）。"""
        self.load()
        selected = set()
        for f in edited_files:
            for t in self.tests_covering_file(f):
                selected.add(t)
        if not selected:
            return test_command
        return "python3 -m pytest " + " ".join(sorted(selected)) + " -q"
