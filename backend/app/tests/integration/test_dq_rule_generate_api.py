"""自动生成 preview 集成测试：schema 三态映射 + EXISTS 幂等标记。"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio
GEN_BASE = "/api/v1/data-quality/rules/generate"


async def ensureClassWithProperty(dbSession: AsyncSession, *, sourceTable: str = "PORDER") -> int:
    """造一个本体类 + 一个 PK 属性，返回 class_id。"""
    import uuid

    from app.domain.models import OntologyClass, OntologyProperty
    suffix = uuid.uuid4().hex[:8]
    cls = OntologyClass(class_name=f"GenTestClass{suffix}", source_table=sourceTable,
                        object_type="Transaction")
    dbSession.add(cls)
    await dbSession.flush()
    dbSession.add(OntologyProperty(
        class_id=cls.id, property_name="po_key", source_column="PO_KEY", data_type="STRING",
        is_primary_key=True, is_foreign_key=False))
    await dbSession.commit()
    return cls.id


async def ensureDataSourceAndSchema(dbSession, *, tables: dict) -> int:
    """造数据源 + schema_cache（tables: {表名: [(列名, 类型, nullable)]}）。"""
    import uuid

    from app.domain.models import DataSource, SchemaCache
    ds = DataSource(name=f"gen-{uuid.uuid4().hex[:8]}", type="POSTGRESQL", host="localhost",
                   port=5433, database_name="qa_metadata_test", username="qa_user",
                   password_encrypted="x")
    dbSession.add(ds)
    await dbSession.flush()
    schemaData = [
        {"table_name": t, "owner": "", "columns": [
            {"column_name": c, "data_type": ty, "nullable": nul} for c, ty, nul in cols],
         "primary_keys": [], "foreign_keys": []}
        for t, cols in tables.items()
    ]
    dbSession.add(SchemaCache(datasource_id=ds.id, schema_data=schemaData, schema_version="v1"))
    await dbSession.commit()
    return ds.id


async def test_preview_matched_yields_suggestions(client: AsyncClient, dbSession: AsyncSession):
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [("PO_KEY", "varchar", False)]})
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    assert res.status_code == 200
    body = res.json()
    assert body["className"].startswith("GenTestClass")
    types = {s["ruleType"] for s in body["suggestions"]}
    assert types == {"UNIQUENESS", "COMPLETENESS"}
    assert all(s["status"] == "NEW" for s in body["suggestions"])
    assert body["blocked"] == []


async def test_preview_missing_column_blocks(client, dbSession):
    classId = await ensureClassWithProperty(dbSession, sourceTable="PORDER")
    dsId = await ensureDataSourceAndSchema(dbSession, tables={"PORDER": [("OTHER", "varchar", True)]})
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    body = res.json()
    assert body["suggestions"] == []
    assert any("PO_KEY" in b["reason"] for b in body["blocked"])


async def test_preview_no_schema_cache_blocks_all(client, dbSession):
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={})
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    body = res.json()
    assert body["suggestions"] == []
    assert all(b["reason"] == "数据源 schema 未缓存" for b in body["blocked"])


async def test_preview_class_not_found(client: AsyncClient):
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": 999999, "datasourceId": 1})
    assert res.status_code == 404


async def test_preview_existing_rule_marked_exists(client, dbSession):
    from app.domain.models import DataQualityRule
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [("PO_KEY", "varchar", False)]})
    # 先 preview 获取实际 rule_code（类名含 uuid 后缀，slug 后已融入 code）
    res = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    suggestions = res.json()["suggestions"]
    uniq = [s for s in suggestions if s["ruleType"] == "UNIQUENESS"][0]
    assert uniq["status"] == "NEW"
    actualCode = uniq["ruleCode"]
    # 用实际 code 创建既有规则
    dbSession.add(DataQualityRule(
        rule_name="既有", rule_code=actualCode, datasource_id=dsId,
        target_table="PORDER", target_column="PO_KEY", rule_type="UNIQUENESS"))
    await dbSession.commit()
    # 再次 preview，UNIQUENESS 应标记为 EXISTS
    res2 = await client.post(f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId})
    uniq2 = [s for s in res2.json()["suggestions"] if s["ruleType"] == "UNIQUENESS"][0]
    assert uniq2["status"] == "EXISTS"


async def test_preview_to_confirm_round_trip_no_422(client, dbSession):
    """preview 返回的 rule_code 必须能被 confirm 接受（避免 lowercase hex 422）。

    回归测试：preview 用 buildRuleCode 生成小写 hex 后缀的 rule_code，
    前端原样回传 → 之前 422 pattern_mismatch。修复后必须 200/201 或预期的 409。
    """
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(dbSession, tables={
        "PORDER": [
            ("PO_KEY", "varchar", False),
            ("STATUS", "varchar", True),
        ]
    })
    # 把 po_key 标成主键以触发 UNIQUENESS+COMPLETENESS 规则
    preview = (await client.post(
        f"{GEN_BASE}/preview",
        json={"classId": classId, "datasourceId": dsId},
    )).json()
    new_rules = [s for s in preview["suggestions"] if s["status"] == "NEW"]
    assert new_rules, "preview 应至少产生 1 条 NEW 规则"
    # 关键断言：rule_code 必须符合 schema pattern（防止 lowercase hex 回归）
    import re

    from app.domain.schemas import GenerateRuleItem

    pattern = next(
        meta.pattern
        for meta in GenerateRuleItem.model_fields["rule_code"].metadata
        if hasattr(meta, "pattern")
    )
    for s in new_rules:
        assert re.match(pattern, s["ruleCode"]), (
            f"rule_code {s['ruleCode']!r} violates schema pattern"
        )
    # 回传 confirm：必须不是 422（200/201 成功，或 409 唯一冲突都可接受）
    confirm = await client.post(
        f"{GEN_BASE}/confirm",
        json={"datasourceId": dsId, "rules": new_rules},
    )
    assert confirm.status_code != 422, (
        f"preview→confirm 出现 422，detail: {confirm.text}"
    )
