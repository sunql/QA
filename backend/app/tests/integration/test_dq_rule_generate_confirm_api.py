"""confirm 批量落库 + outbox 审计 + 幂等跳过集成测试（dq-rule-auto-generation Task 5）。

强制规则（Harness/rules/测试规范.md）：真实 PG（5433/qa_metadata_test），
Alembic upgrade head 自动建表，每测试 TRUNCATE 隔离。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import DerivationType, RuleType, Severity

pytestmark = pytest.mark.asyncio
GEN_BASE = "/api/v1/data-quality/rules/generate"


async def ensureClassWithProperty(
    dbSession: AsyncSession, *, source_table: str = "PORDER"
) -> int:
    """造一个本体类 + 一个 PK 属性，返回 class_id。"""
    from app.domain.models import OntologyClass, OntologyProperty
    import uuid

    suffix = uuid.uuid4().hex[:8]
    cls = OntologyClass(
        class_name=f"GenTestClass{suffix}",
        source_table=source_table,
        object_type="Transaction",
    )
    dbSession.add(cls)
    await dbSession.flush()
    dbSession.add(
        OntologyProperty(
            class_id=cls.id,
            property_name="po_key",
            source_column="PO_KEY",
            data_type="STRING",
            is_primary_key=True,
            is_foreign_key=False,
        )
    )
    await dbSession.commit()
    return cls.id


async def ensureDataSourceAndSchema(dbSession, *, tables: dict) -> int:
    """造数据源 + schema_cache（tables: {表名: [(列名, 类型, nullable)]}）。"""
    from app.domain.models import DataSource, SchemaCache
    import uuid

    ds = DataSource(
        name=f"gen-{uuid.uuid4().hex[:8]}",
        type="POSTGRESQL",
        host="localhost",
        port=5433,
        database_name="qa_metadata_test",
        username="qa_user",
        password_encrypted="x",
    )
    dbSession.add(ds)
    await dbSession.flush()
    schemaData = [
        {
            "table_name": t,
            "owner": "",
            "columns": [
                {"column_name": c, "data_type": ty, "nullable": nul}
                for c, ty, nul in cols
            ],
            "primary_keys": [],
            "foreign_keys": [],
        }
        for t, cols in tables.items()
    ]
    dbSession.add(
        SchemaCache(
            datasource_id=ds.id, schema_data=schemaData, schema_version="v1"
        )
    )
    await dbSession.commit()
    return ds.id


async def test_confirm_creates_rules_with_owner_and_audit(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """首次 confirm → 批量落库 + source_class_id 溯源 + outbox 审计行。"""
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(
        dbSession, tables={"PORDER": [("PO_KEY", "varchar", False)]}
    )
    # preview 获取建议
    previewRes = await client.post(
        f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId}
    )
    assert previewRes.status_code == 200
    suggestions = previewRes.json()["suggestions"]
    newRules = [dict(s) for s in suggestions if s["status"] == "NEW"]
    assert len(newRules) > 0

    # confirm 写入
    confirmRes = await client.post(
        f"{GEN_BASE}/confirm",
        json={"datasourceId": dsId, "rules": newRules},
    )
    assert confirmRes.status_code == 200, confirmRes.text
    body = confirmRes.json()
    assert len(body["created"]) == len(newRules)
    assert body["skippedCodes"] == []

    # 验库：source_class_id 溯源正确
    ruleCodes = [r["ruleCode"] for r in newRules]
    rows = (
        await dbSession.execute(
            text(
                "SELECT rule_code, source_class_id, derivation_type "
                "FROM data_quality_rule WHERE rule_code = ANY(:codes)"
            ),
            {"codes": ruleCodes},
        )
    ).fetchall()
    assert len(rows) == len(newRules)
    assert all(r.source_class_id == classId for r in rows)
    # derivation_type 应为 generator 产生的非 MANUAL 值
    assert all(r.derivation_type != DerivationType.MANUAL.value for r in rows)

    # outbox 行数 == created 数
    outboxCount = (
        await dbSession.execute(
            text(
                "SELECT count(*) FROM audit_outbox "
                "WHERE event_type = 'data_quality_rule_created' "
                "AND entity_type = 'data_quality_rule'"
            )
        )
    ).scalar()
    assert outboxCount == len(newRules)


async def test_confirm_repeat_skips_existing(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """同一 preview 结果连续 confirm 两次，第二次 created=[] skippedCodes 非空。"""
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(
        dbSession, tables={"PORDER": [("PO_KEY", "varchar", False)]}
    )
    previewRes = await client.post(
        f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId}
    )
    suggestions = previewRes.json()["suggestions"]
    newRules = [dict(s) for s in suggestions if s["status"] == "NEW"]

    # 第一次 confirm
    r1 = await client.post(
        f"{GEN_BASE}/confirm", json={"datasourceId": dsId, "rules": newRules}
    )
    assert r1.status_code == 200
    assert len(r1.json()["created"]) == len(newRules)
    assert r1.json()["skippedCodes"] == []

    # 第二次 confirm 同一批
    r2 = await client.post(
        f"{GEN_BASE}/confirm", json={"datasourceId": dsId, "rules": newRules}
    )
    assert r2.status_code == 200
    assert r2.json()["created"] == []
    assert len(r2.json()["skippedCodes"]) == len(newRules)


async def test_confirm_duplicate_code_in_batch_counts_skipped(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """rules 列表内含已在 DB 的 rule_code → 该条记入 skippedCodes，不阻塞其余。"""
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(
        dbSession, tables={"PORDER": [("PO_KEY", "varchar", False)]}
    )
    previewRes = await client.post(
        f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId}
    )
    suggestions = previewRes.json()["suggestions"]
    newRules = [dict(s) for s in suggestions if s["status"] == "NEW"]
    assert len(newRules) >= 2

    # 先手动创建一个（故意重复第一个 rule_code）
    from app.domain.models import DataQualityRule

    existingCode = newRules[0]["ruleCode"]
    dbSession.add(
        DataQualityRule(
            rule_name="既有规则",
            rule_code=existingCode,
            datasource_id=dsId,
            target_table="PORDER",
            target_column="PO_KEY",
            rule_type=RuleType.UNIQUENESS,
        )
    )
    await dbSession.commit()

    # confirm（含重复 code 的批次）
    r = await client.post(
        f"{GEN_BASE}/confirm", json={"datasourceId": dsId, "rules": newRules}
    )
    assert r.status_code == 200
    body = r.json()
    # 除了第一条被跳过，其余成功
    assert len(body["skippedCodes"]) == 1
    assert existingCode in body["skippedCodes"]
    assert len(body["created"]) == len(newRules) - 1


async def test_confirm_threshold_and_severity_preserved(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """confirm body 传入的 threshold / severity 原样落库。"""
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(
        dbSession, tables={"PORDER": [("PO_KEY", "varchar", False)]}
    )
    previewRes = await client.post(
        f"{GEN_BASE}/preview", json={"classId": classId, "datasourceId": dsId}
    )
    suggestions = previewRes.json()["suggestions"]
    newRules = [dict(s) for s in suggestions if s["status"] == "NEW"]

    # 修改 threshold / severity
    newRules[0]["threshold"] = "88.50"
    newRules[0]["severity"] = Severity.HIGH.value

    r = await client.post(
        f"{GEN_BASE}/confirm", json={"datasourceId": dsId, "rules": newRules}
    )
    assert r.status_code == 200
    created = r.json()["created"]
    assert len(created) == len(newRules)

    ruleCode = newRules[0]["ruleCode"]
    row = (
        await dbSession.execute(
            text(
                "SELECT threshold, severity FROM data_quality_rule WHERE rule_code = :code"
            ),
            {"code": ruleCode},
        )
    ).fetchone()
    assert row is not None
    assert str(row.threshold) == "88.50"
    assert row.severity == Severity.HIGH.value


async def test_confirm_empty_rules_422(client: AsyncClient, dbSession: AsyncSession) -> None:
    """rules=[] → 422。"""
    dsId = (await ensureDataSourceAndSchema(dbSession, tables={}))  # 空 schema 不影响
    r = await client.post(
        f"{GEN_BASE}/confirm", json={"datasourceId": dsId, "rules": []}
    )
    assert r.status_code == 422


async def test_confirm_invalid_rule_code_422(client: AsyncClient, dbSession: AsyncSession) -> None:
    """rule_code 不符格式 → 422。"""
    classId = await ensureClassWithProperty(dbSession)
    dsId = await ensureDataSourceAndSchema(
        dbSession, tables={"PORDER": [("PO_KEY", "varchar", False)]}
    )
    r = await client.post(
        f"{GEN_BASE}/confirm",
        json={
            "datasourceId": dsId,
            "rules": [
                {
                    "ruleCode": "lowercase",  # 必须大写开头
                    "ruleName": "Test",
                    "targetTable": "PORDER",
                    "ruleType": RuleType.UNIQUENESS.value,
                    "threshold": "95.00",
                    "severity": Severity.MEDIUM.value,
                }
            ],
        },
    )
    assert r.status_code == 422


async def test_confirm_datasource_not_found_404(client: AsyncClient) -> None:
    """datasourceId 不存在 → 显式 404（service 层 raise NotFoundError）。"""
    r = await client.post(
        f"{GEN_BASE}/confirm",
        json={
            "datasourceId": 999999,
            "rules": [
                {
                    "ruleCode": "TEST_ABCD",
                    "ruleName": "Test",
                    "targetTable": "T",
                    "ruleType": RuleType.UNIQUENESS.value,
                    "threshold": "95.00",
                    "severity": Severity.MEDIUM.value,
                }
            ],
        },
    )
    assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"
    body = r.json()
    assert "error" in body or "message" in body
