"""研究 ESL 三臂适配器（`api/v1/research.py` 的 `_searchBusinessObjects` / `_matchMetrics` /
`_searchKnowledge`）。

Task 7.5-3：三条适配器此前**零执行覆盖**——签名漂移（既有服务改名/改参）会被 ESL 的
per-arm `except` 吞成空臂，静默降级为空 scope 或空检索，测试全绿但功能已死。本文件对
**种子数据各调一次真实适配器**，断言返回 dict 的键集与取值和 ESL 契约
（`enterprise_semantic_layer.py:87-175` 的 `_extractArm` / `_toBoRef` / `_toMetricRef` /
`_toKnowledgeRef`）逐字段对齐，再经真实 `EnterpriseSemanticLayer` 走一遍，证明三臂**不降级为空**。

依赖边界 mock 说明（brief 允许：种子成本高的服务可 mock 依赖边界，适配器函数本身必须真跑）：
- BO 臂：mock Milvus 近邻检索 + embedding 生成（无网络 / 无 key）；
  `OntologyService.searchByKeyword` 与适配器的 `source_table` 回填**真跑**（真实 PG）。
- Metric 臂：**零 mock** —— 真实 `KpiCatalog` 种子 + 真实缓存 warmUp + 真实 `matchAll`。
- Knowledge 臂：mock Milvus wiki chunk 检索 + embedding；真实
  `WikiVectorService.searchSemantic` 与 PG 标题回填**真跑**（真实 PG）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import research as researchModule
from app.domain.models import KpiCatalog, OntologyClass
from app.domain.wiki_models import WikiPage
from app.services.kpi_match_cache import kpi_match_cache

pytestmark = pytest.mark.asyncio

# 种子常量：与 mock 的检索命中一一对应
CLASS_ID = 987001
CLASS_NAME = "供应商"
CLASS_TABLE = "DIM_SUPPLIER"
CLASS_ALIAS = "supplier"
KPI_CODE = "KPI_RECEIPT_QTY"
KPI_NAME = "收货量"
KPI_KEYWORDS = ["收货", "货量", "收货量"]
PAGE_ID = "RULE-SUPPLIER-RECEIPT-V1"
PAGE_TITLE = "供应商收货量复盘"
CHUNK_TEXT = "供应商收货量下降的复盘结论"
QUESTION = "收货量"  # KPI 臂的 Jaccard 命中面（2/3 字 ngram 恰等于 semantic_keywords）
_EMBEDDING = [0.25] * 8


async def _fakeGenerateEmbedding(self, text: str) -> list[float]:
    """EmbeddingService.generateEmbedding 替身：固定向量，零网络零 key。"""
    return list(_EMBEDDING)


def _fakeRoutedSearch(embedding, topK, typeFilter=None) -> list[dict]:
    """Milvus 类型路由检索替身：恒返回一条 class 命中（距离 0 ⇒ 相似度 1.0）。"""
    return [
        {
            "ontology_id": CLASS_ID,
            "type": "class",
            "name": CLASS_NAME,
            "alias": CLASS_ALIAS,
            "description": "供应商主数据",
            "distance": 0.0,
        }
    ]


def _fakeWikiChunkSearch(embedding, *, dimension=None, topK=10) -> list[dict]:
    """Milvus wiki chunk 检索替身：恒返回一条 chunk（距离 0 ⇒ 相似度 1.0）。"""
    return [
        {
            "page_id": PAGE_ID,
            "chunk_id": f"{PAGE_ID}:0",
            "chunk_text": CHUNK_TEXT,
            "chunk_sequence": 0,
            "title": "Milvus 陈旧快照标题",  # 真实实现必须用 PG 最新标题覆盖它
            "dimension": None,
            "status": "EFFECTIVE",
            "distance": 0.0,
        }
    ]


class _FakeEmbSvc:
    """Wiki embedding 替身：只有 generateEmbedding（无 embedWithUsage ⇒ 计量 token 记 0）。"""

    async def generateEmbedding(self, text: str) -> list[float]:
        return list(_EMBEDDING)


@pytest.fixture()
async def seededAdapters(dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """种子数据 + 依赖边界 mock：让三条**真实适配器**在真实 PG 上跑通。"""
    import app.services.ontology_service as ontologyModule
    import app.services.wiki_vector_service as wikiModule

    dbSession.add(
        OntologyClass(
            id=CLASS_ID,
            class_name=CLASS_NAME,
            class_alias=CLASS_ALIAS,
            source_table=CLASS_TABLE,
            version=1,
        )
    )
    dbSession.add(
        KpiCatalog(
            kpi_code=KPI_CODE,
            kpi_name=KPI_NAME,
            status="PUBLISHED",
            semantic_keywords=KPI_KEYWORDS,
            match_threshold=Decimal("0.5"),
        )
    )
    dbSession.add(
        WikiPage(page_id=PAGE_ID, title=PAGE_TITLE, content="供应商收货量下降的复盘。")
    )
    await dbSession.commit()
    # 缓存是 DB-backed 的：种完 KPI 必须重跑 warmUp，否则 matchAll 看不到它
    kpi_match_cache.onKpiChanged()
    await kpi_match_cache.warmUp(dbSession)
    await dbSession.commit()

    monkeypatch.setattr(ontologyModule.EmbeddingService, "generateEmbedding", _fakeGenerateEmbedding)
    monkeypatch.setattr(
        ontologyModule.milvus, "searchEmbeddingsByTypeRouted", _fakeRoutedSearch
    )
    monkeypatch.setattr(wikiModule, "_getEmbeddingService", lambda: _FakeEmbSvc())
    monkeypatch.setattr(wikiModule.milvus, "searchWikiPageChunks", _fakeWikiChunkSearch)


async def test_adapter_dicts_match_esl_contract(seededAdapters) -> None:
    """三条真实适配器返回的 dict 键集与取值必须落在 ESL 契约上。"""
    bos = await researchModule._searchBusinessObjects(QUESTION, topK=5)
    assert bos == [
        {
            "classId": CLASS_ID,
            "className": CLASS_NAME,
            "sourceTable": CLASS_TABLE,  # 回填自 PG（真实查询）
            "matchedAlias": CLASS_ALIAS,
            "confidence": 1.0,
        }
    ]

    metrics = await researchModule._matchMetrics(QUESTION, topK=5)
    assert metrics and set(metrics[0]) == {
        "metricId",
        "kpiCode",
        "displayName",
        "formula",
        "confidence",
    }
    assert metrics[0]["kpiCode"] == KPI_CODE
    assert metrics[0]["displayName"] == KPI_NAME
    assert metrics[0]["confidence"] > 0

    knowledge = await researchModule._searchKnowledge(QUESTION, topK=5)
    assert knowledge == [
        {
            "pageId": PAGE_ID,
            "title": PAGE_TITLE,  # PG 最新标题覆盖 Milvus 快照
            "snippet": CHUNK_TEXT,
            "score": 1.0,
        }
    ]


async def test_real_adapters_feed_esl_without_empty_arm(seededAdapters) -> None:
    """真实适配器接进真实 ESL：三臂都非空（签名漂移不再被静默吞成空臂）。"""
    extraction = await researchModule.buildEnterpriseSemanticLayer().extract(QUESTION)

    assert [obj.classId for obj in extraction.businessObjects] == [CLASS_ID]
    assert extraction.businessObjects[0].sourceTable == CLASS_TABLE
    assert extraction.businessObjects[0].className == CLASS_NAME
    assert [m.kpiCode for m in extraction.metrics] == [KPI_CODE]
    assert [k.pageId for k in extraction.knowledge] == [PAGE_ID]
    assert extraction.knowledge[0].title == PAGE_TITLE
    assert extraction.confidenceByArm["business_object"] == 1.0
    assert extraction.confidenceByArm["knowledge"] == 1.0
