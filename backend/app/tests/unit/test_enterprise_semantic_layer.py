"""ESL 三臂 + 冲突/降级语义（纯 unit，fake 检索器注入）。"""
import pytest

from app.services.enterprise_semantic_layer import (
    EnterpriseSemanticLayer, EmptyResearchScopeError,
)


def _layer(*, bo=None, kpi=None, wiki=None) -> EnterpriseSemanticLayer:
    async def boSearcher(q, topK=5):
        if bo is None:
            return [{"classId": 1, "className": "供应商", "sourceTable": "DIM_SUPPLIER",
                     "matchedAlias": "供应商", "confidence": 0.9}]
        return bo
    async def kpiMatcher(q, topK=5):
        if kpi is None:
            return [{"metricId": 7, "kpiCode": "KPI_RCPT", "displayName": "收货量",
                     "formula": "COUNT(RCV_LINE_NO)", "confidence": 0.8}]
        return kpi
    async def wikiSearcher(q, topK=5):
        if wiki is None:
            return []
        return wiki
    return EnterpriseSemanticLayer(boSearcher=boSearcher, kpiMatcher=kpiMatcher, wikiSearcher=wikiSearcher)


@pytest.mark.asyncio
async def test_three_arms_populated() -> None:
    result = await _layer().extract("供应商收货量为什么下降")
    assert result.businessObjects[0].sourceTable == "DIM_SUPPLIER"
    assert result.metrics[0].kpiCode == "KPI_RCPT"
    assert 0.0 <= result.confidenceByArm["business_object"] <= 1.0


@pytest.mark.asyncio
async def test_metric_ambiguous_conflict_when_close_scores() -> None:
    kpi = [
        {"metricId": 1, "kpiCode": "A", "displayName": "收货量", "formula": None, "confidence": 0.80},
        {"metricId": 2, "kpiCode": "B", "displayName": "发货量", "formula": None, "confidence": 0.75},
    ]
    result = await _layer(kpi=kpi).extract("量")
    kinds = [c.kind for c in result.conflicts]
    assert "metric_ambiguous" in kinds


@pytest.mark.asyncio
async def test_all_empty_raises() -> None:
    with pytest.raises(EmptyResearchScopeError):
        await _layer(bo=[], kpi=[], wiki=[]).extract("乱码问题")


@pytest.mark.asyncio
async def test_wiki_timeout_degrades_to_empty_arm() -> None:
    async def wikiFail(q, topK=5):
        raise TimeoutError("wiki down")
    layer = _layer(wiki=None)
    # 构造后替换 wiki 依赖为抛超时的 fake
    layer._wikiSearcher = wikiFail
    result = await layer.extract("供应商收货量")
    assert result.knowledge == []
    assert result.confidenceByArm["knowledge"] == 0.0
