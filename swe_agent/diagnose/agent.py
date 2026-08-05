# -*- coding: utf-8 -*-
"""模块B: diagnose 定位 Agent — 从自然语言 Issue 定位 bug 候选。

流程（确定性围栏 + LLM 理解）：
    1. 建图（GraphManager）
    2. L-1 文件级先验 + 锚点提取（确定性：函数名/文件名/错误关键词）
    3. LLM 单轮定位：Issue + 先验 + 锚点 → 结构化候选（Pydantic 校验）
    4. 输出 DiagnoseResult（候选集按置信度排序 + 测试锚点）

核心原则（Phase 7 辩论定稿）：
    - 图负责找到（结构导航），LLM 负责理解（运行时推理）
    - AI 语义备注不落盘（不污染确定性图）
    - 定位是启发式，会被测试验证层拦下（L1/L3）
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import DiagnoseCandidate, DiagnoseResult


@dataclass
class DiagnoseAgent:
    """diagnose 定位 Agent：Issue → 结构化 bug 候选。"""

    project_dir: str
    llm_client: object = None
    max_rounds: int = 2          # 最多几轮定位（锚点不足时可深度读取）
    verbose: bool = True

    def __post_init__(self):
        from swe_agent.graph.manager import GraphManager
        self.graph_mgr = GraphManager(self.project_dir)
        self.index = self.graph_mgr.build()
        self._plain_task: Optional[str] = None

    # ── 确定性锚点提取 ──
    def _extract_anchors(self, issue: str) -> dict:
        """从 Issue 提取锚点：函数名 / 文件名 / 错误类型 / 行号。纯确定性。

        函数名锚点 = 图中真实函数名 ∩ Issue 文本（零噪音，只认图里存在的）；
        另兼容 "name(" 调用写法。文件名/错误类型/行号用正则。
        """
        anchors = {
            "functions": set(),
            "files": set(),
            "errors": set(),
            "linenos": set(),
        }
        # 函数名：图中真实函数名（短名）出现在 Issue 文本中
        for n in self.index.graph.nodes.values():
            if n.node_type.value != "function":
                continue
            short = n.name.split(".")[-1]  # 方法名去掉类前缀
            if short in issue or n.name in issue:
                anchors["functions"].add(short)
        # 兼容 "name(" 写法（含图中没有的）
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", issue):
            name = m.group(1)
            if len(name) >= 3 and name not in (
                "def", "class", "print", "return", "if", "for", "while",
                "not", "and", "or", "in", "is", "with", "as", "try",
                "except", "raise", "import", "from", "assert", "len",
                "str", "int", "float", "list", "dict", "set", "get",
            ):
                anchors["functions"].add(name)
        # 文件名模式：xxx.py
        for m in re.finditer(r"([A-Za-z0-9_./\\-]+\.py)", issue):
            anchors["files"].add(os.path.basename(m.group(1)))
        # 错误类型：XxxError
        for m in re.finditer(r"([A-Z][A-Za-z0-9_]*Error)", issue):
            anchors["errors"].add(m.group(1))
        # 行号：line N / 第 N 行
        for m in re.finditer(r"(?:line|第)\s*(\d+)", issue, re.IGNORECASE):
            anchors["linenos"].add(int(m.group(1)))
        return anchors

    def _match_anchors(self, anchors: dict) -> list[dict]:
        """用锚点在图上匹配节点（确定性，不调 LLM）。"""
        hits = []
        seen = set()
        for fn in anchors["functions"]:
            for node in self.index.search_nodes(fn, limit=5):
                if node.node_id in seen:
                    continue
                seen.add(node.node_id)
                hits.append({
                    "node": node.node_id,
                    "file": node.file,
                    "kind": "function",
                    "anchor": fn,
                })
        for f in anchors["files"]:
            for node in self.index.graph.nodes.values():
                if os.path.basename(node.file) == f and node.node_id not in seen:
                    seen.add(node.node_id)
                    hits.append({
                        "node": node.node_id,
                        "file": node.file,
                        "kind": "file",
                        "anchor": f,
                    })
        return hits[:20]

    def _l1_prior_text(self) -> str:
        """L-1 文件级先验（复用图索引方法，与 FSM 同一份数据）。"""
        return self.index.file_level_prior_text()

    def _anchor_context(self, hits: list[dict]) -> str:
        """锚点命中结果的图上下文（1 跳邻域 + 影响面）。"""
        if not hits:
            return "（Issue 中未提取到图锚点）"
        lines = ["# Issue 锚点在图中的命中："]
        for h in hits[:15]:
            node = self.index.get_node(h["node"])
            if node is None:
                continue
            impact = self.index.compute_impact(h["node"])
            lines.append(
                f"- {h['node']} (锚点={h['anchor']}, 入度={node.in_degree}, "
                f"影响面={impact:.3f})"
            )
        return "\n".join(lines)

    # ── LLM 定位 ──
    def _build_prompt(self, issue: str, anchors: dict, hits: list[dict]) -> str:
        from swe_agent.prompts.prompt_manager import PromptManager
        pm = PromptManager()
        base = pm.load("diagnose.md")
        return (
            f"{base}\n\n"
            f"【Issue 描述】\n{issue}\n\n"
            f"【代码库结构（L-1 文件级先验）】\n{self._l1_prior_text()}\n\n"
            f"【Issue 锚点命中】\n{self._anchor_context(hits)}"
        )

    def _parse_result(self, text: str) -> Optional[DiagnoseResult]:
        """解析 LLM 输出为结构化结果（Pydantic 校验）。"""
        # 提取 JSON（容忍 markdown 代码块包裹）
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        try:
            return DiagnoseResult(**data)
        except Exception:
            return None

    def diagnose(self, issue: str) -> DiagnoseResult:
        """主入口：Issue → DiagnoseResult。"""
        if self.llm_client is None:
            from swe_agent.llm.client import LLMClient
            self.llm_client = LLMClient()

        anchors = self._extract_anchors(issue)
        hits = self._match_anchors(anchors)
        if self.verbose:
            print(f"[DIAGNOSE] 锚点: 函数={sorted(anchors['functions'])[:8]} "
                  f"文件={sorted(anchors['files'])[:5]} 错误={sorted(anchors['errors'])[:5]}")
            print(f"[DIAGNOSE] 图命中 {len(hits)} 个节点")

        prompt = self._build_prompt(issue, anchors, hits)

        for round_i in range(self.max_rounds):
            if self.verbose:
                print(f"[DIAGNOSE] LLM 定位第 {round_i + 1} 轮...")
            response = self.llm_client.chat(
                messages=[
                    {"role": "system", "content": "你是代码定位助手，只输出 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=800,
                temperature=0.2,
            )
            text = response.content if hasattr(response, "content") else str(response)
            result = self._parse_result(text)
            if result is not None:
                if self.verbose:
                    print(f"[DIAGNOSE] 定位完成: {len(result.candidates)} 个候选, "
                          f"置信度={result.confidence}")
                return result
            # 解析失败 → 下一轮（提示词补一句要求严格 JSON）
            prompt += "\n\n（上一轮输出不是合法 JSON，请只输出一个 JSON 对象，不要多余文字。）"

        # 兜底：无候选时给出空结果（FSM 不会启动）
        if self.verbose:
            print("[DIAGNOSE] 未能产出合法结构化候选")
        return DiagnoseResult(issue=issue, candidates=[], confidence=0.0)

    def best_candidate(self, result: DiagnoseResult) -> Optional[DiagnoseCandidate]:
        """取置信度最高的候选（衔接 FSM 用）。"""
        if not result.candidates:
            return None
        return result.candidates[0]
