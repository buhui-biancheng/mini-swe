# -*- coding: utf-8 -*-
"""模块 F：start_fix 意图路由核心（Phase 7）。

原则（2026-08-03 辩论定稿）：
    - 自然语言是否触发修复由 LLM 判断（start_fix 工具调用，非关键词规则）
    - 非确定性只出现在"进不进门"这个决策点，进门后全是确定性围栏（FSM）
    - 误判兜底：start_fix 前 LLM 已探索、用户可取消（复用 Phase 2 取消事件）
    - 表面无关：TUI 是权威壳，web 是可视化壳，共享同一路由核心

本实现：路由核心（LLM 判断 + 提取修复请求 → run_diagnose_fix）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from .schemas import DiagnoseResult
from .bridge import run_diagnose_fix, DiagnoseFixResult


@dataclass
class IntentDecision:
    """意图路由决策。"""
    is_fix_request: bool          # LLM 判断是否修复请求
    issue: str = ""               # 提取的 bug 描述
    reason: str = ""              # 判断理由
    raw: str = ""

    def to_dict(self) -> dict:
        return {"is_fix_request": self.is_fix_request,
                "issue": self.issue, "reason": self.reason}


class IntentRouter:
    """start_fix 意图路由：LLM 判断 → 修复请求 → FSM。"""

    def __init__(self, llm_client=None, verbose: bool = True):
        self.llm_client = llm_client
        self.verbose = verbose

    def _build_prompt(self, user_input: str) -> str:
        return f"""你是意图路由。判断用户输入是否是"请求修复代码 bug"。

【判断标准】
- 是修复请求：提到 bug/错误/异常/结果不对/应该XX实际YY/测试失败等
- 不是修复请求：闲聊、问问题、讲方案、描述需求但没提"出错了"

【用户输入】
{user_input}

只输出 JSON（不要多余文字）：
{{"is_fix_request": true/false, "issue": "提取出的 bug 描述（若是修复请求；否则空串）", "reason": "一句话判断理由"}}"""

    def decide(self, user_input: str) -> IntentDecision:
        """LLM 判断意图。"""
        if self.llm_client is None:
            from swe_agent.llm.client import LLMClient
            self.llm_client = LLMClient()
        response = self.llm_client.chat(
            messages=[
                {"role": "system", "content": "你是意图路由，只输出 JSON。"},
                {"role": "user", "content": self._build_prompt(user_input)},
            ],
            max_tokens=300,
            temperature=0.0,  # 路由判断要确定性
        )
        text = response.content if hasattr(response, "content") else str(response)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return IntentDecision(is_fix_request=False, raw=text,
                                  reason="JSON 解析失败")
        try:
            data = json.loads(m.group(0))
            return IntentDecision(
                is_fix_request=bool(data.get("is_fix_request", False)),
                issue=data.get("issue", ""),
                reason=data.get("reason", ""),
                raw=text,
            )
        except Exception as e:
            return IntentDecision(is_fix_request=False, raw=text,
                                  reason=f"JSON 解析异常: {e}")

    def route(self, user_input: str, project_dir: str,
              verbose: bool = True) -> Optional[DiagnoseFixResult]:
        """完整路由：判断 → diagnose+FSM 修复。

        非修复请求返回 None（不触发任何修复）。
        """
        decision = self.decide(user_input)
        if self.verbose:
            print(f"[ROUTE] is_fix_request={decision.is_fix_request} "
                  f"reason={decision.reason}")
        if not decision.is_fix_request:
            return None
        issue = decision.issue or user_input  # 兜底：用原始输入
        return run_diagnose_fix(issue, project_dir, verbose=verbose)
