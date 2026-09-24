"""feat-layer-priority: 端到端 schema 段集成测（真实 PG）。

走真实 PostgreSQL + ChatService._selectRelevantClasses（mock ontology.searchByKeyword
让其返回所有 seed 类命中）。验证：

1. 默认问题（无 ``ODS_*`` 表名）→ ``ODS_BUSINESS`` 不出现在 ``relevant``。
2. 显式 ``ODS_*`` 问题 → ``ODS_BUSINESS`` 出现在 ``relevant``。

只覆盖「召回 + 排序 + 显式 ODS 解锁」三段；LLM/embedding/Milvus 等下游全部 mock。
mock 只发生在 ontology.searchByKeyword；system_config 读取 / DIM_* 拉取仍走真实 PG，
以验证「DB 集成而非纯单测」。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass
from app.services.chat_service import ChatService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CLASS_LAYER_KEYS = ("ADS_X", "DWS_Y", "DWD_Z", "DIM_W", "ODS_V")
_EXPLICIT_CLASS_LAYER_KEYS = ("ADS_X2", "DWS_Y2", "DWD_Z2", "DIM_W2", "ODS_V")


def _makeClass(*, class_name: str, source_table: str) -> OntologyClass:
    """最小可用的 OntologyClass（让 _getClassLayer 识别层前缀）。"""
    return OntologyClass(
        class_name=class_name,
        source_table=source_table,
    )


class _FakeOntology:
    """Fake OntologyService：searchByKeyword 返回所有 seed 类命中。

    让 ``_selectRelevantClasses`` 的召回入口通过，验证下游排序/过滤逻辑。
    listClasses / listJoins 保持实现最小（实际不会用到，因为相关字段已从外部传入）。
    """

    def __init__(self, classes: list[OntologyClass]) -> None:
        self._classes = list(classes)

    async def searchByKeyword(self, query, *, topK=5, typeFilter=None) -> list:
        return [
            SimpleNamespace(id=c.id, score=1.0)
            for c in self._classes
            if c.id is not None
        ]

    async def listClasses(self, session):
        return list(self._classes)

    async def listJoins(self, session):
        return []


async def _seedLayerClasses(
    dbSession: AsyncSession, layerKeys: tuple[str, ...]
) -> list[OntologyClass]:
    """seed 每层一个本体类；返回持久化后的对象列表（含自增 id）。"""
    seeds = [
        _makeClass(class_name=f"LP_{k}", source_table=k) for k in layerKeys
    ]
    for c in seeds:
        dbSession.add(c)
    await dbSession.commit()

    rows = (
        await dbSession.execute(
            select(OntologyClass).where(
                OntologyClass.source_table.in_(list(layerKeys))
            )
        )
    ).scalars().all()
    return list(rows)


def _srcTables(classes: list[OntologyClass]) -> list[str]:
    return [c.source_table for c in classes if c.source_table]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestChatPipelineLayerPriority:
    """feat-layer-priority: 召回 + 排序 + 显式 ODS 解锁的端到端契约。"""

    async def test_default_question_excludes_ods_business(
        self, dbSession: AsyncSession
    ) -> None:
        """默认问题（无 ``ODS_*`` 表名）→ 召回不含 ODS_BUSINESS。

        - question 含「供应商编号」→ dimension_hint=True（拉 DIM_* 全量）
        - 不含 ``ODS_*`` → explicit_ods=False → 召回入口过滤 ODS_BUSINESS
        - 排序后 ADS>DWS>DWD>DIM 入候选；ODS_V 缺席。
        """
        # Arrange：seed 5 个类（每层一个）
        seeded = await _seedLayerClasses(dbSession, _DEFAULT_CLASS_LAYER_KEYS)
        assert len(seeded) == 5, "seed 必须包含 ADS/DWS/DWD/DIM/ODS 各一个"

        # Act：mock searchByKeyword → 全部进 relevant；问题不含 ODS_*
        service = ChatService(ontologyService=_FakeOntology(seeded))
        question = "B019 圣特供应商编号是多少"
        result, recall = await service._selectRelevantClasses(
            dbSession, question, list(seeded)
        )

        # Assert：ODS_BUSINESS 被排除；其它 4 层都在
        tables = _srcTables(result)
        assert "ODS_V" not in tables, (
            f"默认问题不应出现 ODS_BUSINESS，实际: {tables}"
        )
        assert "ADS_X" in tables, f"ADS 层应保留: {tables}"
        assert "DWS_Y" in tables, f"DWS 层应保留: {tables}"
        assert "DWD_Z" in tables, f"DWD 层应保留: {tables}"
        assert "DIM_W" in tables, f"DIM 层应保留（dimension_hint 触发）: {tables}"

        # Assert：层优先排序 ADS>DWS>DWD>DIM（mock 无 ODS 故 ODS 不在结果）
        layer_order = [
            c.source_table for c in result
            if c.source_table in {"ADS_X", "DWS_Y", "DWD_Z", "DIM_W"}
        ]
        assert layer_order == ["ADS_X", "DWS_Y", "DWD_Z", "DIM_W"], (
            f"层优先排序失败: {layer_order}"
        )

        # Assert：召回诊断透出（recall 或 expanded 都行，不强制）
        assert recall is not None
        assert recall.hitCount >= 4

    async def test_explicit_ods_question_includes_ods_business(
        self, dbSession: AsyncSession
    ) -> None:
        """显式 ODS 问题（含 ``ODS_V`` 表名）→ ODS_BUSINESS 进入 relevant。

        - ``ODS_V`` 命中 ``\\bODS_[A-Z][A-Z0-9_]*\\b`` → explicit_ods=True
        - 召回入口不再过滤 ODS_BUSINESS；ODS_V 进 relevant；按层排序落底。
        """
        # Arrange：seed 5 个类（同样每层一个）
        seeded = await _seedLayerClasses(dbSession, _EXPLICIT_CLASS_LAYER_KEYS)
        assert len(seeded) == 5

        # Act：问题显式含 ``ODS_V`` 表名
        service = ChatService(ontologyService=_FakeOntology(seeded))
        question = "ODS_V 里有什么供应商信息"
        result, recall = await service._selectRelevantClasses(
            dbSession, question, list(seeded)
        )

        # Assert：ODS_V 在结果中
        tables = _srcTables(result)
        assert "ODS_V" in tables, (
            f"显式 ODS_* 问题必须放行 ODS_BUSINESS，实际: {tables}"
        )

        # Assert：其它层也都在
        for k in ("ADS_X2", "DWS_Y2", "DWD_Z2", "DIM_W2"):
            assert k in tables, f"mock 全命中下其它层应保留: {tables}"

        # Assert：排序 ODS_BUSINESS 落底（layer_rank=5，其它 0..3）
        layer_order = [c.source_table for c in result]
        ods_idx = layer_order.index("ODS_V")
        # ODS_V 之前的都是 ADS/DWS/DWD/DIM
        for before in layer_order[:ods_idx]:
            assert not before.startswith("ODS_"), (
                f"ODS_BUSINESS 不应排在其它 ODS 之前: {layer_order}"
            )
