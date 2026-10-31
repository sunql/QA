"""季度时间口径从编排层透传到 plan/SQL prompt（2026-10-31）。

只测「接线」：解析器与 prompt 渲染各自已有单测；本文件钉死
`generateValidatedPlan` / `generateSql` 必须把服务端解析结果传下去，
避免出现「prompt 函数支持，但生产链路没传」的假绿。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from app.domain.models import OntologyClass
from app.domain.query_plan import Aggregation, PlanResult, QueryPlan
from app.services.nl2sql_service import Nl2SqlService


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


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.modelName = "test-model"
        self.promptTokens = 10
        self.completionTokens = 5


class _FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[tuple[str, str]]] = []

    async def complete(self, messages: list, **kwargs) -> _Resp:
        self.calls.append([(m.role, m.content) for m in messages])
        return _Resp(self._responses.pop(0))


def _llmConfig() -> SimpleNamespace:
    return SimpleNamespace(model_name="test-model", temperature=0.0)


def _buildClass(name: str, table: str) -> OntologyClass:
    return OntologyClass(class_name=name, class_alias=None, source_table=table, properties=[])


def _goodPlan() -> QueryPlan:
    return QueryPlan(
        target="x",
        selectedClasses=("ReceiptDetail",),
        selectedProperties=("库存数量",),
        aggregations=(Aggregation(function="SUM", property="库存数量", alias="TOTAL_QTY"),),
    )


async def test_generate_validated_plan_threads_time_scope_fact() -> None:
    captured: dict = {}

    async def gen(*args, **kwargs):
        captured.update(kwargs)
        return PlanResult(plan=_goodPlan(), promptTokens=1, completionTokens=1)

    service = Nl2SqlService()
    originalGen = service.generateQueryPlan
    originalIssues = service._planIssues
    originalFinalize = service._finalizePlan
    service.generateQueryPlan = gen  # type: ignore[method-assign]
    service._planIssues = lambda *a, **k: []  # type: ignore[method-assign]
    service._finalizePlan = lambda planResult, *a, **k: planResult  # type: ignore[method-assign]
    try:
        await service.generateValidatedPlan(
            "2026年第三季度的采购情况",
            [],
            SimpleNamespace(complete=None),
            _llmConfig(),
            maxRetries=0,
        )
    finally:
        service.generateQueryPlan = originalGen  # type: ignore[method-assign]
        service._planIssues = originalIssues  # type: ignore[method-assign]
        service._finalizePlan = originalFinalize  # type: ignore[method-assign]

    fact = captured.get("timeScopeFact")
    assert fact, "计划阶段必须透传 timeScopeFact"
    assert "2026年第3季度" in fact
    assert "2026-07-01" in fact


async def test_generate_sql_prompt_contains_time_scope_fact() -> None:
    fake = _FakeLlm(["```sql\nSELECT 1 AS val FROM DUAL\n```"])
    service = Nl2SqlService()

    await service.generateSql(
        "2026年第三季度各供应商采购额",
        [_buildClass("PRECEIPT", "PRECEIPT")],
        fake,
        _llmConfig(),
        maxRetries=0,
    )

    systemPrompt = fake.calls[0][0][1]
    assert "2026年第3季度" in systemPrompt
    assert "2026-07-01" in systemPrompt


async def test_generate_sql_uses_scope_question_when_sub_question_lost_time() -> None:
    """多步场景：子问题不带时间，主问题（scopeQuestion）带季度时必须仍注入。"""
    fake = _FakeLlm(["```sql\nSELECT 1 AS val FROM DUAL\n```"])
    service = Nl2SqlService()

    await service.generateSql(
        "各供应商采购额",
        [_buildClass("PRECEIPT", "PRECEIPT")],
        fake,
        _llmConfig(),
        maxRetries=0,
        scopeQuestion="2026年第三季度的采购情况",
    )

    systemPrompt = fake.calls[0][0][1]
    assert "2026年第3季度" in systemPrompt


async def test_generate_sql_with_prior_state_skips_relative_scope() -> None:
    """多轮追问：相对季度按上一轮时间递减，注入今天的解析结果会与继承冲突。"""
    fake = _FakeLlm(["```sql\nSELECT 1 AS val FROM DUAL\n```"])
    service = Nl2SqlService()

    await service.generateSql(
        "上季度采购额",
        [_buildClass("PRECEIPT", "PRECEIPT")],
        fake,
        _llmConfig(),
        maxRetries=0,
        priorState="上一轮问题：2025年第二季度采购额\n继承时间：2025年第2季度",
    )

    systemPrompt = fake.calls[0][0][1]
    assert "服务端按日历边界计算" not in systemPrompt


async def test_generate_sql_with_multistep_prior_state_keeps_relative_scope() -> None:
    """单轮多步的 priorState 只是前序步骤结果（无「继承时间」），不应误抑制。"""
    fake = _FakeLlm(["```sql\nSELECT 1 AS val FROM DUAL\n```"])
    service = Nl2SqlService()

    await service.generateSql(
        "本季度采购额",
        [_buildClass("PRECEIPT", "PRECEIPT")],
        fake,
        _llmConfig(),
        maxRetries=0,
        priorState="前序步骤结果：各供应商采购额 TOP 10（无时间继承）",
    )

    systemPrompt = fake.calls[0][0][1]
    assert "服务端按日历边界计算" in systemPrompt


async def test_generate_validated_plan_with_inherited_time_skips_relative_scope() -> None:
    """计划阶段与 SQL 阶段同一判定：有「继承时间」时相对季度不注入。"""
    captured: dict = {}

    async def gen(*args, **kwargs):
        captured.update(kwargs)
        return PlanResult(plan=_goodPlan(), promptTokens=1, completionTokens=1)

    service = Nl2SqlService()
    originalGen = service.generateQueryPlan
    originalIssues = service._planIssues
    originalFinalize = service._finalizePlan
    service.generateQueryPlan = gen  # type: ignore[method-assign]
    service._planIssues = lambda *a, **k: []  # type: ignore[method-assign]
    service._finalizePlan = lambda planResult, *a, **k: planResult  # type: ignore[method-assign]
    try:
        await service.generateValidatedPlan(
            "上季度采购额",
            [],
            SimpleNamespace(complete=None),
            _llmConfig(),
            maxRetries=0,
            priorState="上一轮问题：2025年第二季度采购额\n继承时间：2025年第2季度",
        )
    finally:
        service.generateQueryPlan = originalGen  # type: ignore[method-assign]
        service._planIssues = originalIssues  # type: ignore[method-assign]
        service._finalizePlan = originalFinalize  # type: ignore[method-assign]

    assert captured.get("timeScopeFact") == ""


async def test_generate_sql_with_prior_state_still_injects_explicit_year() -> None:
    fake = _FakeLlm(["```sql\nSELECT 1 AS val FROM DUAL\n```"])
    service = Nl2SqlService()

    await service.generateSql(
        "2026年第三季度采购额",
        [_buildClass("PRECEIPT", "PRECEIPT")],
        fake,
        _llmConfig(),
        maxRetries=0,
        priorState="上一轮问题：2025年第二季度采购额\n继承时间：2025年第2季度",
    )

    systemPrompt = fake.calls[0][0][1]
    assert "2026年第3季度" in systemPrompt


async def test_generate_sql_without_time_scope_injects_nothing() -> None:
    fake = _FakeLlm(["```sql\nSELECT 1 AS val FROM DUAL\n```"])
    service = Nl2SqlService()

    await service.generateSql(
        "各供应商采购额汇总",
        [_buildClass("PRECEIPT", "PRECEIPT")],
        fake,
        _llmConfig(),
        maxRetries=0,
    )

    systemPrompt = fake.calls[0][0][1]
    assert "服务端按日历边界计算" not in systemPrompt
