"""apply_dim_supplier_metadata.py 数据修复脚本集成测试（真实 PG）。

背景（2026-09-18 诊断）：问「公司有多少供应商」时 LLM 概率性倒向
DWD_BUSINESS_PARTNER 而非 DIM_SUPPLIER——DIM_SUPPLIER 的 229 列全是
Sage X3 裸物理码（BPSNUM_0…）无别名，而 DWD 表 8 列语义清晰且类描述
自夸「是供应商和客户的汇总数据」。

覆盖：
- DIM_SUPPLIER 核心列别名经 updateProperty 落库（audit + Neo4j fake 同步）
- DWD_BUSINESS_PARTNER 类描述经 updateClass 收敛（引导 LLM 优先 DIM 层）
- 幂等：重复执行全部 skipped，不重复写
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import app.services.ontology_service as ontology_service_module
from app.dependencies import CurrentUser
from app.domain.models import OntologyClass, OntologyProperty
from app.services.acl_service import ADMIN_ROLE
from app.services.ontology_service import OntologyService
from scripts.apply_dim_supplier_metadata import (
    ALIAS_MAP,
    PARTNER_DESCRIPTION,
    PARTNER_CLASS_NAME,
    SUPPLIER_SOURCE_TABLE,
    apply,
)

_ADMIN = CurrentUser(userId="t-admin", roles=(ADMIN_ROLE,), departments=())


class _FakeNeo4j:
    def upsertPropertyNode(self, **kw):  # noqa: N802
        pass

    def linkClassHasProperty(self, classId, propId):  # noqa: N803
        pass

    def upsertClassNode(self, **kw):  # noqa: N802
        pass

    def reconcileClassSubclassOf(self, classId, parentId):  # noqa: N803
        pass


async def _noopSync(self, entity) -> None:
    return None


@pytest.fixture()
def _installFakes(monkeypatch):
    monkeypatch.setattr(ontology_service_module, "neo4j", _FakeNeo4j())
    # updateClass 会把类向量同步丢进后台任务（create_task 需要协程对象）
    monkeypatch.setattr(
        ontology_service_module.OntologyService,
        "_syncClassEmbeddingBestEffort",
        _noopSync,
    )


async def _seedClasses(session) -> tuple[int, int]:
    """直接落两行类 + DIM_SUPPLIER 裸物理列属性（绕过嵌入同步，验证脚本本身）。"""
    now = datetime.now(timezone.utc)
    dim = OntologyClass(
        class_name="DIM_SUPPLIER",
        source_table=SUPPLIER_SOURCE_TABLE,
        description="供应商主数据表",
        created_time=now,
        updated_time=now,
    )
    session.add(dim)
    await session.flush()
    for pn in ("BPSNUM_0", "BPSNAM_0", "BPSSHO_0", "BPSTYP_0", "UPDTICK_0"):
        session.add(
            OntologyProperty(
                class_id=dim.id,
                property_name=pn,
                source_column=pn,
                data_type="varchar",
                created_time=now,
                updated_time=now,
            )
        )
    partner = OntologyClass(
        class_name=PARTNER_CLASS_NAME,
        source_table=PARTNER_CLASS_NAME,
        description="公司合作伙伴的表，是供应商和客户的汇总数据",
        created_time=now,
        updated_time=now,
    )
    session.add(partner)
    await session.commit()
    return dim.id, partner.id


async def _propsByClassId(session, classId: int) -> dict[str, str]:
    rows = await session.execute(
        OntologyProperty.__table__.select().where(OntologyProperty.class_id == classId)
    )
    return {r.property_name: r.property_alias for r in rows}


class TestApplyDimSupplierMetadata:
    async def test_aliases_and_description_applied(self, _installFakes, dbSession) -> None:
        dimId, partnerId = await _seedClasses(dbSession)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        # 别名全部落库；UPDTICK_0（乐观锁）不在 ALIAS_MAP，不受影响
        assert sorted(summary["aliased"]) == sorted(ALIAS_MAP)
        assert summary["missing"] == []
        assert summary["descriptionUpdated"] is True

        props = await _propsByClassId(dbSession, dimId)
        assert props["BPSNUM_0"] == ALIAS_MAP["BPSNUM_0"]
        assert props["BPSNAM_0"] == ALIAS_MAP["BPSNAM_0"]
        assert props["BPSSHO_0"] == ALIAS_MAP["BPSSHO_0"]
        assert props["UPDTICK_0"] is None

        partner = await dbSession.get(OntologyClass, partnerId)
        await dbSession.refresh(partner)
        assert partner.description == PARTNER_DESCRIPTION

    async def test_idempotent_rerun_all_skipped(self, _installFakes, dbSession) -> None:
        await _seedClasses(dbSession)
        await apply(dbSession, OntologyService(), actor=_ADMIN)

        summary = await apply(dbSession, OntologyService(), actor=_ADMIN)

        assert summary["aliased"] == []
        assert sorted(summary["skipped"]) == sorted(ALIAS_MAP)
        assert summary["missing"] == []
        assert summary["descriptionUpdated"] is False

    async def test_missing_property_reported_not_fatal(self, _installFakes, dbSession) -> None:
        """目标列不存在时记入 missing 并继续处理其余列（不抛异常中断）。"""
        now = datetime.now(timezone.utc)
        dim = OntologyClass(
            class_name="DIM_SUPPLIER",
            source_table=SUPPLIER_SOURCE_TABLE,
            created_time=now,
            updated_time=now,
        )
        session = dbSession
        session.add(dim)
        await session.flush()
        session.add(
            OntologyProperty(
                class_id=dim.id,
                property_name="BPSNUM_0",
                source_column="BPSNUM_0",
                data_type="varchar",
                created_time=now,
                updated_time=now,
            )
        )
        await session.commit()

        summary = await apply(session, OntologyService(), actor=_ADMIN)

        assert summary["aliased"] == ["BPSNUM_0"]
        assert set(summary["missing"]) == set(ALIAS_MAP) - {"BPSNUM_0"}
