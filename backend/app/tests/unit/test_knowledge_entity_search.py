"""M2-2 采购实体解析（`GET /procurement/knowledge/entities/search`）。

覆盖三层（设计稿 §8.3 契约）：
1. `buildEntityMatches` 纯函数：分组（enterprise_key）/ 打分（精确 key=1.0 >
   编码全等 0.9 > 前缀 0.8 > 中缀 0.6）/ 排序（分数降序 + key 兜底）；
2. 路由 handler 直调（真实 PG）：service 检索 → builder → DTO 契约（camelCase）；
3. **双接线守卫**：main.py 与 _testapp.py 必须同在 `enableKnowledgeFacade` 块内
   注册本路由（两套 wiring 手工平行维护，漏一边 ⇒ 404 的已知坑）。

打分依据源数据：`searchMappings` 命中语义（全数字精确 key + ILIKE 兜底）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.domain.enums import MatchRule, SourceSystem
from app.domain.knowledge_schemas import (
    ProcurementEntityMatchRead,
    ProcurementEntitySourceMappingRead,
)
from app.domain.schemas import EntityMappingCreate
from app.services.knowledge.entity_search import buildEntityMatches


def _row(
    enterpriseKey: int,
    enterpriseCode: str,
    *,
    sourceSystem: str = "ERP",
    sourceCode: str | None = None,
    name: str | None = None,
    entityType: str = "SUPPLIER",
) -> SimpleNamespace:
    return SimpleNamespace(
        enterprise_key=enterpriseKey,
        enterprise_code=enterpriseCode,
        source_system=sourceSystem,
        source_key=f"{sourceSystem}-{enterpriseKey}",
        source_code=sourceCode or enterpriseCode,
        match_rule=MatchRule.MDM_MASTER,
        effective_date=date(2026, 1, 1),
        expiry_date=None,
        name=name,
        entity_type=entityType,
    )


# ── 1. 纯函数 ─────────────────────────────────────────────────────────────


def test_buildEntityMatches_groupsByEnterpriseKey() -> None:
    rows = [
        _row(101, "SUP001", sourceSystem="ERP"),
        _row(101, "SUP001", sourceSystem="SRM", sourceCode="S-SUP001"),
    ]
    matches = buildEntityMatches(rows, q="SUP001")
    assert len(matches) == 1, "同一 enterprise_key 的多源行必须合并为一实体"
    assert matches[0].match_score == 0.9, "编码全等（大小写不敏感）= 0.9"
    assert [m.source_system for m in matches[0].source_mappings] == ["ERP", "SRM"], (
        "sourceMappings 必须携带全部源系统映射"
    )
    assert isinstance(matches[0], ProcurementEntityMatchRead)
    assert isinstance(matches[0].source_mappings[0], ProcurementEntitySourceMappingRead)


def test_buildEntityMatches_scoresDeterministically() -> None:
    rows = [
        _row(7, "XSUP001"),          # 中缀命中（ILIKE）⇒ 0.6
        _row(8, "SUP0012"),          # 前缀命中 ⇒ 0.8
        _row(9, "SUP001"),           # 全等 ⇒ 0.9
        _row(10, "SUP001", sourceCode="X"),  # 全等（另一源系统）⇒ 0.9
    ]
    matches = buildEntityMatches(rows, q="SUP001")
    assert [(m.enterprise_key, m.match_score) for m in matches] == [
        (9, 0.9), (10, 0.9), (8, 0.8), (7, 0.6),
    ], "分数降序 + enterprise_key 升序兜底（确定性）"


def test_buildEntityMatches_exactKeyScoresOne() -> None:
    rows = [_row(12345, "SUP-X", name="泰鸿钢铁")]
    matches = buildEntityMatches(rows, q="12345")
    assert matches[0].match_score == 1.0, "全数字 enterprise_key 精确命中 = 1.0"
    assert matches[0].name == "泰鸿钢铁"


def test_buildEntityMatches_emptyInput() -> None:
    assert buildEntityMatches([], q="x") == []


def test_buildEntityMatches_doesNotMutateInput() -> None:
    rows = [_row(1, "A"), _row(2, "B")]
    snapshot = [r.enterprise_key for r in rows]
    buildEntityMatches(rows, q="A")
    assert [r.enterprise_key for r in rows] == snapshot, "入参不变（返回新列表）"


# ── 2. handler 直调（真实 PG）─────────────────────────────────────────────


async def test_searchProcurementEntitiesHandlerReturnsContract(dbSession) -> None:
    """真实 PG：落两源映射 → 直调 handler ⇒ 契约字段齐全（camelCase 由 DTO 保证）。"""
    from app.api.v1.procurement_knowledge_entities import searchProcurementEntities
    from app.services.entity_mapping_service import EntityMappingService

    svc = EntityMappingService()
    actor = SimpleNamespace(userId="m2-2-test", departments=())  # type: ignore[arg-type]
    for dto in (
        EntityMappingCreate(
            entity_type="SUPPLIER", enterprise_key=900001,
            enterprise_code="PRCSUP001", source_system=SourceSystem.ERP,
            source_key="ERP-900001", source_code="ERP-PRCSUP001",
            match_rule=MatchRule.MDM_MASTER, name="测试供应商甲",
        ),
        EntityMappingCreate(
            entity_type="SUPPLIER", enterprise_key=900001,
            enterprise_code="PRCSUP001", source_system=SourceSystem.SRM,
            source_key="SRM-900001", source_code="SRM-PRCSUP001",
            match_rule=MatchRule.BUSINESS_KEY, name="测试供应商甲",
        ),
    ):
        await svc.createMapping(dbSession, dto, actor=actor)  # type: ignore[arg-type]
    await dbSession.commit()

    response = SimpleNamespace(headers={})
    result = await searchProcurementEntities(
        response,  # type: ignore[arg-type]
        q="PRCSUP001",
        entityType="SUPPLIER",
        limit=20,
        user=SimpleNamespace(userId="m2-2-test", departments=()),  # type: ignore[arg-type]
        session=dbSession,
    )

    assert len(result) == 1
    match = result[0]
    assert match.enterprise_key == 900001
    assert match.enterprise_code == "PRCSUP001"
    assert match.name == "测试供应商甲"
    assert match.match_score == 0.9, "编码全等 ⇒ 0.9"
    assert {m.source_system for m in match.source_mappings} == {"ERP", "SRM"}, (
        "sourceMappings 必须覆盖全部源系统"
    )
    assert match.source_mappings[0].match_rule in ("MDM_MASTER", "BUSINESS_KEY")
    dumped = match.model_dump(by_alias=True)
    assert "enterpriseKey" in dumped and "matchScore" in dumped and "sourceMappings" in dumped
    assert dumped["sourceMappings"][0]["sourceSystem"] == "ERP", "camelCase 线格式"


# ── 3. 双接线守卫 ─────────────────────────────────────────────────────────


def test_entities_search_registered_in_both_wirings() -> None:
    """main.py 与 _testapp.py 必须同在 enableKnowledgeFacade 块内注册本路由。

    两套 wiring 手工平行维护（_testapp.py 注释自陈「漏掉 ⇒ 集成测试 404」）
    ⇒ 用源码级断言钉住，防止只加一边。
    """
    backendRoot = Path(__file__).resolve().parents[3]
    for rel in ("app/main.py", "app/tests/_testapp.py"):
        code = (backendRoot / rel).read_text(encoding="utf-8")
        assert "procurement_knowledge_entities" in code, f"{rel} 未导入新路由模块"
        assert "procurement_knowledge_entities.router" in code, (
            f"{rel} 未注册 entities/search 路由（漏一边 ⇒ 那一侧 404）"
        )
