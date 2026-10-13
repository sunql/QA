"""规划问题按 mode 注入研究指引（Task 13）：纯函数契约 + 三 mode 指引互异。

背景（本任务要补的真缺口）：报告章节早就按 mode 差异化
（`report_planner._MODE_SECTIONS`），但**计划**链路对 mode 零消费 —— 同一个问题无论
是「研究 / 归因 / 对比」，拆出来的步完全一样。修法是复用既有的**问题文本注入**机制
（与 `planQuestionWithFeedback` 同款），把 mode 指引拼进**规划问题**，
`StepQueryPlanner.plan(question, client, model_name)` 的签名因此一行不动。

本文件钉住纯函数侧的契约：
- 命中 mode ⇒ 返回以原问题开头、含该 mode 指引的**新**串（入参不被改写）；
- 未命中（未知 / 空）⇒ **逐字**原样返回，且**不抛**（规划阶段不得因 mode 崩 ——
  `compose` 对未知 mode 抛 ValueError 是报告阶段的策略，不照搬到计划阶段）；
- 键集合与 `ResearchMode` 取值一致，且三段指引两两不同（防复制粘贴同一个串）。
"""

from __future__ import annotations

from typing import get_args

from app.domain.research_schemas import ResearchMode
from app.services.research_agent_phases import _MODE_GUIDANCE, planQuestionForMode

_QUESTION = "上个月华东区各供应商的收货量"


def test_known_mode_appends_guidance_after_question() -> None:
    """命中 mode ⇒ 新串以原问题开头、含该 mode 指引，且原串未被改写。"""
    out = planQuestionForMode(_QUESTION, "attribution")

    assert out.startswith(_QUESTION)
    assert _MODE_GUIDANCE["attribution"] in out
    assert out != _QUESTION
    assert _QUESTION == "上个月华东区各供应商的收货量", "入参不得被就地改写"


def test_unknown_mode_returns_question_verbatim() -> None:
    """未知 mode ⇒ 逐字原样返回（不抛：规划阶段不得因 mode 崩）。"""
    assert planQuestionForMode(_QUESTION, "nope") == _QUESTION


def test_empty_mode_returns_question_verbatim() -> None:
    """空 mode ⇒ 逐字原样返回（同上，走的是同一条「未命中」分支）。"""
    assert planQuestionForMode(_QUESTION, "") == _QUESTION


def test_guidance_keys_match_declared_modes() -> None:
    """指引键集合 == `ResearchMode` 取值集合（漂移即报，不静默少一个 mode）。"""
    assert set(_MODE_GUIDANCE) == set(get_args(ResearchMode))


def test_each_mode_guidance_is_nonempty() -> None:
    """每段指引都必须有实质内容（空串等于「注入了个寂寞」，测试却会绿）。"""
    for mode, guidance in _MODE_GUIDANCE.items():
        assert guidance.strip(), f"{mode} 的指引是空的"


def test_mode_guidances_are_pairwise_distinct() -> None:
    """三段指引两两不同 —— 防「复制粘贴一个串改个 key」的假差异化。"""
    values = [planQuestionForMode(_QUESTION, mode) for mode in get_args(ResearchMode)]
    assert len(set(values)) == len(values), values
