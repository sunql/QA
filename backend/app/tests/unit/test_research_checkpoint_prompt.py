"""检查点问句构造器单测（feat-research-entry-ux-fixes W1-a）。

纯函数、零 DB、零状态机 —— 这正是把文案从 `research_agent_service.py` 抽到
`research_agent_stages.py` 的目的：文案可被钉住，且不必驱动整条研究链路。

契约：问句必须带**对象**（条数 / 冲突种类），否则用户只看到泛问句不知在决策什么；
未知冲突种类回落通用词、不抛错（ESL 未来新增 kind 时不得崩）。
"""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio

from app.services.research_agent_stages import (
    ambiguityPrompt,
    conflictKindLabel,
    hypothesisPrompt,
)

# ---------------------------------------------------------------------------
# 本文件是纯函数测试，不需要 DB。
# 覆盖 conftest 的 autouse DB fixtures —— 否则每个用例都会 TRUNCATE 整个测试库
# （seedEngine 里 _truncateAll），既慢又会抹掉集成测试依赖的迁移种子数据。
# ---------------------------------------------------------------------------


@pytest.fixture()
def dbSession() -> Any:
    """覆盖 conftest autouse dbSession —— 本文件不用 DB。"""
    return None


@pytest_asyncio.fixture()
async def seedEngine() -> Any:
    """覆盖 conftest autouse seedEngine —— 不建引擎、不 truncate。"""
    return None


@pytest_asyncio.fixture()
async def warmBusinessObjectRegistry() -> Any:
    """覆盖 conftest autouse warmBusinessObjectRegistry —— 无需 DB。"""
    yield


def test_conflict_kind_labels_are_human_readable() -> None:
    assert conflictKindLabel("metric_ambiguous") == "指标歧义"
    assert conflictKindLabel("wiki_disagree") == "知识冲突"


def test_unknown_conflict_kind_falls_back_without_raising() -> None:
    assert conflictKindLabel("brand_new_kind") == "待确认项"
    assert conflictKindLabel("") == "待确认项"


def test_ambiguity_prompt_carries_count_and_kinds() -> None:
    prompt = ambiguityPrompt(
        [{"kind": "metric_ambiguous"}, {"kind": "wiki_disagree"}]
    )
    assert prompt == "检测到 2 处语义歧义（指标歧义、知识冲突），请确认采用哪一项？"


def test_ambiguity_prompt_dedups_repeated_kinds() -> None:
    """同类多条目：种类只报一次，条数照实报。"""
    prompt = ambiguityPrompt(
        [{"kind": "metric_ambiguous"}, {"kind": "metric_ambiguous"}]
    )
    assert prompt == "检测到 2 处语义歧义（指标歧义），请确认采用哪一项？"


def test_ambiguity_prompt_falls_back_when_kind_unresolvable() -> None:
    assert ambiguityPrompt([{"kind": ""}]) == "检测到语义歧义，请确认采用哪一项？"
    assert ambiguityPrompt([]) == "检测到语义歧义，请确认采用哪一项？"


def test_hypothesis_prompt_carries_candidate_count() -> None:
    prompt = hypothesisPrompt([{"statement": "a"}, {"statement": "b"}, {"statement": "c"}])
    assert prompt == "共 3 条候选假设，请选择要验证的（可多选）："


def test_hypothesis_prompt_explains_empty_candidates() -> None:
    """降级路径（无 LLM / 解析失败）候选本就是 [] —— 必须给下一步指引，不能只报 0 条。"""
    prompt = hypothesisPrompt([])
    assert prompt == "本轮未生成候选假设（模型不可用或解析失败），可点「修改」补充研究方向。"
