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


@pytest.mark.asyncio
async def test_wiki_disagree_fires_with_two_relevant_pages() -> None:
    wiki = [
        {"pageId": 1, "title": "台账治理", "snippet": "s1", "score": 0.6},
        {"pageId": 2, "title": "别名治理", "snippet": "s2", "score": 0.5},
    ]
    result = await _layer(wiki=wiki).extract("供应商")
    conflict = next(c for c in result.conflicts if c.kind == "wiki_disagree")
    # 文案只声明"共主题、建议人工核对"，不断言内容冲突（相关性触发 ≠ 内容分歧）
    assert conflict.detail == "2 篇高相关 wiki 共主题，建议人工核对表述是否冲突"


@pytest.mark.asyncio
async def test_wiki_disagree_silent_below_threshold() -> None:
    wiki = [
        {"pageId": 1, "title": "A", "snippet": "s1", "score": 0.6},
        {"pageId": 2, "title": "B", "snippet": "s2", "score": 0.49},
    ]
    result = await _layer(wiki=wiki).extract("供应商")
    assert all(c.kind != "wiki_disagree" for c in result.conflicts)


@pytest.mark.asyncio
async def test_snippet_truncated_to_280_chars() -> None:
    wiki = [{"pageId": 9, "title": "长文", "snippet": "x" * 300, "score": 0.4}]
    result = await _layer(wiki=wiki).extract("供应商")
    assert len(result.knowledge[0].snippet) == 280


@pytest.mark.asyncio
async def test_metric_ambiguous_order_independent() -> None:
    # 升序传入，验证排序无关：candidates 必须按置信度降序输出
    kpi = [
        {"metricId": 1, "kpiCode": "A", "displayName": "收货量", "formula": None, "confidence": 0.71},
        {"metricId": 2, "kpiCode": "B", "displayName": "发货量", "formula": None, "confidence": 0.80},
    ]
    result = await _layer(kpi=kpi).extract("量")
    ambiguous = [c for c in result.conflicts if c.kind == "metric_ambiguous"]
    assert len(ambiguous) == 1
    assert [c["confidence"] for c in ambiguous[0].candidates] == [0.80, 0.71]


@pytest.mark.asyncio
async def test_metric_exact_boundary_gap_does_not_conflict() -> None:
    # 精确分差 = 0.1（IEEE-754: 0.80-0.70 = 0.10000000000000009），
    # 严格 < 0.1 语义下边界不触发；round(gap, 10) 保证判定与十进制语义一致
    kpi = [
        {"metricId": 1, "kpiCode": "A", "displayName": "收货量", "formula": None, "confidence": 0.70},
        {"metricId": 2, "kpiCode": "B", "displayName": "发货量", "formula": None, "confidence": 0.80},
    ]
    result = await _layer(kpi=kpi).extract("量")
    assert all(c.kind != "metric_ambiguous" for c in result.conflicts)


@pytest.mark.asyncio
async def test_metric_no_conflict_when_gap_exceeds_threshold() -> None:
    kpi = [
        {"metricId": 1, "kpiCode": "A", "displayName": "收货量", "formula": None, "confidence": 0.70},
        {"metricId": 2, "kpiCode": "B", "displayName": "发货量", "formula": None, "confidence": 0.81},
    ]
    result = await _layer(kpi=kpi).extract("量")
    assert all(c.kind != "metric_ambiguous" for c in result.conflicts)


@pytest.mark.asyncio
async def test_malformed_bo_item_skipped_other_arms_intact() -> None:
    bo = [{"className": "供应商", "sourceTable": "DIM_SUPPLIER",
           "matchedAlias": "供应商", "confidence": 0.9}]
    result = await _layer(bo=bo).extract("供应商收货量")
    assert result.businessObjects == []
    assert result.metrics[0].kpiCode == "KPI_RCPT"
    assert result.confidenceByArm["business_object"] == 0.0
