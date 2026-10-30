"""B3：generateValidatedPlan → generateQueryPlan → plan prompt 的契约透传。

纯单测。隔离手法同 test_property_ref_normalize.TestGenerateValidatedPlanRetryNormalizes
（替换实例方法 + try/finally 恢复）：

- `generateQueryPlan` 替身为 fake，捕获 kwargs 断言 `kpiContractText` 透传；
- `_planIssues` 恒 `[]` / `_finalizePlan` 捕获直返——隔离真实本体 schema 校验
  （本测试只关心契约透传，不关心 plan 语义；classes 传 `[]` 即可）。

用例：
1. session 提供 + 装配器有产出 → generateQueryPlan 收到 kpiContractText；
2. session 缺席（部分调用方无 session）→ 不装配（装配器零调用）；
3. 装配器抛错 → 不阻断 plan（warning 降级，plan 照常产出）。
"""

from __future__ import annotations

from types import SimpleNamespace

import app.services.nl2sql_service as nls_module
from app.domain.query_plan import Aggregation, PlanResult, QueryPlan
from app.services.nl2sql_service import Nl2SqlService


def _goodPlan() -> QueryPlan:
    return QueryPlan(
        target="x",
        selectedClasses=("ReceiptDetail",),
        selectedProperties=("库存数量",),
        aggregations=(
            Aggregation(function="SUM", property="库存数量", alias="TOTAL_QTY"),
        ),
    )


def _patch(service: Nl2SqlService) -> dict:
    """替换 generateQueryPlan / _planIssues / _finalizePlan，captured['_restore'] 备恢复。"""
    captured: dict = {}

    async def gen(*args, **kwargs):
        captured.update(kwargs)
        return PlanResult(plan=_goodPlan(), promptTokens=1, completionTokens=1)

    def noIssues(*args, **kwargs):
        return []

    def captureFinalize(planResult, *args, **kwargs):
        captured["finalizePlan"] = planResult.plan
        return planResult

    originalGen = service.generateQueryPlan
    originalIssues = service._planIssues
    originalFinalize = service._finalizePlan
    service.generateQueryPlan = gen  # type: ignore[method-assign]
    service._planIssues = noIssues  # type: ignore[method-assign]
    service._finalizePlan = captureFinalize  # type: ignore[method-assign]
    captured["_restore"] = (originalGen, originalIssues, originalFinalize, service)
    return captured


def _restore(captured: dict) -> None:
    originalGen, originalIssues, originalFinalize, service = captured["_restore"]
    service.generateQueryPlan = originalGen  # type: ignore[method-assign]
    service._planIssues = originalIssues  # type: ignore[method-assign]
    service._finalizePlan = originalFinalize  # type: ignore[method-assign]


async def test_contract_text_threaded_to_generate_query_plan(monkeypatch) -> None:
    async def fakeBlock(session, question):
        return "- 供货及时率 (KPI_B3)\n  分子: received_qty"

    monkeypatch.setattr(nls_module, "buildKpiContractBlock", fakeBlock)
    service = Nl2SqlService()
    captured = _patch(service)
    try:
        await service.generateValidatedPlan(
            "供货及时率",
            [],
            SimpleNamespace(complete=None),
            SimpleNamespace(model_name="x", temperature=0.0),
            session=SimpleNamespace(),  # 非 None 即视为可用
            maxRetries=0,
        )
    finally:
        _restore(captured)

    assert captured.get("kpiContractText") is not None
    assert "供货及时率" in captured["kpiContractText"]


async def test_no_session_skips_contract(monkeypatch) -> None:
    called = {"n": 0}

    async def fakeBlock(session, question):
        called["n"] += 1
        return "- block"

    monkeypatch.setattr(nls_module, "buildKpiContractBlock", fakeBlock)
    service = Nl2SqlService()
    captured = _patch(service)
    try:
        await service.generateValidatedPlan(
            "供货及时率",
            [],
            SimpleNamespace(complete=None),
            SimpleNamespace(model_name="x", temperature=0.0),
            maxRetries=0,
        )
    finally:
        _restore(captured)

    assert called["n"] == 0  # 装配器根本没被调


async def test_block_builder_failure_degrades(monkeypatch) -> None:
    async def boom(session, question):
        raise RuntimeError("match cache exploded")

    monkeypatch.setattr(nls_module, "buildKpiContractBlock", boom)
    service = Nl2SqlService()
    captured = _patch(service)
    try:
        result = await service.generateValidatedPlan(
            "供货及时率",
            [],
            SimpleNamespace(complete=None),
            SimpleNamespace(model_name="x", temperature=0.0),
            session=SimpleNamespace(),
            maxRetries=0,
        )
    finally:
        _restore(captured)

    assert result.plan is not None  # plan 照常产出（降级不阻断）
