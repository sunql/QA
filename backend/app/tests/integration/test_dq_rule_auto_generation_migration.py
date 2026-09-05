"""迁移 0044 契约：新列存在、存量行 derivation_type=MANUAL、allowed_values 可写。"""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def test_dq_rule_derivation_columns_exist(dbSession):
    rows = (await dbSession.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'data_quality_rule' "
        "AND column_name IN ('source_class_id','source_property_id','derivation_type')"
    ))).scalars().all()
    assert set(rows) == {"source_class_id", "source_property_id", "derivation_type"}


async def test_dq_rule_derivation_type_defaults_manual(dbSession):
    from app.domain.models import DataQualityRule, DataSource

    ds = DataSource(
        name="dq-migration-test-ds",
        type="postgresql",
        host="localhost",
        port=5433,
        database_name="test",
        username="u",
        password_encrypted="enc",
        is_active=True,
        is_default=False,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)

    rule = DataQualityRule(
        rule_name="t", rule_code="T001", datasource_id=ds.id, target_table="T",
        rule_type="COMPLETENESS", target_column="C",
    )
    dbSession.add(rule)
    await dbSession.commit()
    await dbSession.refresh(rule)
    assert rule.derivation_type == "MANUAL"
    assert rule.source_class_id is None


async def test_ontology_property_allowed_values_roundtrip(dbSession):
    from app.domain.models import OntologyClass, OntologyProperty

    oc = OntologyClass(class_name="dq_allowed_values_test_class")
    dbSession.add(oc)
    await dbSession.commit()
    await dbSession.refresh(oc)

    prop = OntologyProperty(
        class_id=oc.id,
        property_name="status",
        data_type="STRING",
        allowed_values=["A", "B", "C"],
    )
    dbSession.add(prop)
    await dbSession.commit()
    await dbSession.refresh(prop)
    assert prop.allowed_values == ["A", "B", "C"]
