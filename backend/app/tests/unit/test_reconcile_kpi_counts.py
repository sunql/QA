"""reconcile 计数扩展：kpi_published↔milvus、kpi_total↔neo4j。

⚠️ `_compareCounts` 是 staticmethod，签名 `(*storeCounts) -> list[str]`，
返回的是**字符串列表**（不是 dict）；每条文案形如
「对账不一致 kpi_published: pg=12, milvus=10」。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.knowledge_compiler_service as kcs
from app.domain.models import KpiCatalog
from app.services.knowledge_compiler_service import KnowledgeCompilerService


def _kpiTexts(result: list[str]) -> list[str]:
    """挑出 KPI 相关的对账告警。"""
    return [t for t in result if "kpi" in t]


async def test_kpi_mismatch_reports_both_pairs() -> None:
    """kpi_published↔milvus、kpi_total↔neo4j 两条都出告警（含两侧数值）。"""
    result = KnowledgeCompilerService._compareCounts(
        {"class": 0, "property": 0, "metric": 0, "kpi_published": 12, "kpi_total": 15},
        {"class": 0, "property": 0, "metric": 0, "kpi": 14},
        {"class": 0, "property": 0, "metric": 0, "kpi": 10},
    )

    texts = _kpiTexts(result)
    assert any("kpi_published" in t and "milvus" in t for t in texts)
    assert any("kpi_total" in t and "neo4j" in t for t in texts)


async def test_kpi_parity_is_silent() -> None:
    """两侧都一致 ⇒ 不出任何 kpi 告警。"""
    result = KnowledgeCompilerService._compareCounts(
        {"class": 0, "property": 0, "metric": 0, "kpi_published": 12, "kpi_total": 15},
        {"class": 0, "property": 0, "metric": 0, "kpi": 15},
        {"class": 0, "property": 0, "metric": 0, "kpi": 12},
    )

    assert not _kpiTexts(result)


async def test_none_store_is_tolerated() -> None:
    """store 不可用（None）⇒ 跳过 kpi 比对：不告警，也不抛。"""
    result = KnowledgeCompilerService._compareCounts(
        {"class": 0, "property": 0, "metric": 0, "kpi_published": 12, "kpi_total": 15},
        None,
        None,
    )

    assert not _kpiTexts(result)


async def test_missing_kpi_key_is_tolerated() -> None:
    """store 可用但没有 kpi 键 ⇒ 跳过，不误报。"""
    result = KnowledgeCompilerService._compareCounts(
        {"class": 0, "property": 0, "metric": 0, "kpi_published": 12, "kpi_total": 15},
        {"class": 0, "property": 0, "metric": 0},
        {"class": 0, "property": 0, "metric": 0},
    )

    assert not _kpiTexts(result)


async def test_count_pg_reports_published_and_total(dbSession: AsyncSession) -> None:
    """_countPg 必须给出 kpi_published（只数 PUBLISHED）与 kpi_total（全表）。"""
    dbSession.add(KpiCatalog(kpi_code="KPI_RC_A", kpi_name="A", status="PUBLISHED"))
    dbSession.add(KpiCatalog(kpi_code="KPI_RC_B", kpi_name="B", status="DRAFT"))
    await dbSession.commit()

    counts = await KnowledgeCompilerService._countPg(dbSession)

    assert counts["kpi_total"] == 2
    assert counts["kpi_published"] == 1


async def test_count_neo4j_includes_kpi(monkeypatch: pytest.MonkeyPatch) -> None:
    """_countNeo4j 必须含 kpi 键（KpiCatalog 节点数，对齐 kpi_total）。"""
    monkeypatch.setattr(kcs.neo4j, "getClassIds", lambda: set())
    monkeypatch.setattr(kcs.neo4j, "getPropertyIds", lambda: set())
    monkeypatch.setattr(kcs.neo4j, "listNodesByLabel", lambda label: [])
    monkeypatch.setattr(kcs.neo4j, "getKpiCatalogIds", lambda: {"uid-1", "uid-2"})

    counts = KnowledgeCompilerService._countNeo4j()

    assert counts["kpi"] == 2


async def test_count_milvus_includes_kpi(monkeypatch: pytest.MonkeyPatch) -> None:
    """_countMilvus 必须含 kpi 键（只读 num_entities；不连真 Milvus）。"""
    monkeypatch.setattr(kcs, "_connect", lambda: None)

    def _fakeCollectionCount(collectionName: str) -> int:
        return {"kpi_catalog_embeddings": 7}.get(collectionName, 0)

    monkeypatch.setattr(kcs, "_milvusCollectionCount", _fakeCollectionCount)

    counts = KnowledgeCompilerService._countMilvus()

    assert counts["kpi"] == 7
