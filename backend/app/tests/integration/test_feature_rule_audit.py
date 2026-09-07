"""FeatureRuleService outbox → audit_log 集成测试（真实 PG）。

覆盖 4 个场景：CREATE / UPDATE / DELETE / TOGGLE → audit_log 行落地。
参照 feat-agent-tool-config-db 的 AgentToolConfigService 模式（pgSession
独立 TRUNCATE 隔离 + _admin() helper + AuditWorker().drainOnce() flush）。

强制规则（Harness/rules/测试规范.md）：真实 PG（5433/qa_metadata_test），
Alembic upgrade head 自动建表，pgSession fixture 每测试 TRUNCATE 隔离。
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.dependencies import CurrentUser
from app.domain.enums import RuleOperator, Severity
from app.domain.models import AuditLog
from app.domain.schemas import (
    FeatureRuleCreate,
    FeatureRuleThresholdCreate,
    FeatureRuleUpdate,
)
from app.services.feature_rule_service import FeatureRuleService
from app.tests import _pg_support
from app.workers.audit_worker import AuditWorker


def _admin(actor: str = "admin") -> CurrentUser:
    return CurrentUser(
        userId=actor, tenantId="default", roles=["admin"], departments=["IT"]
    )


@pytest.fixture()
async def pgSession() -> AsyncIterator[AsyncSession]:
    """真实 PG 会话：每测试新建引擎 + TRUNCATE 隔离 + Alembic upgrade head。"""
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _makeCreateDto(
    code: str = "test_create",
    *,
    data_object: str = "SUPPLIER",
    data_layer: str = "FEATURE",
    target_level: str = "RISK",
    feature_name: str = "TEST_FEATURE",
    policy_description: str | None = None,
) -> FeatureRuleCreate:
    return FeatureRuleCreate(
        code=code,
        data_object=data_object,
        data_layer=data_layer,
        target_level=target_level,
        feature_name=feature_name,
        policy_description=policy_description,
        thresholds=[
            FeatureRuleThresholdCreate(
                severity=Severity.HIGH,
                operator=RuleOperator.LT,
                threshold_value=Decimal("10"),
                unit="score",
            )
        ],
    )


@pytest.mark.asyncio
async def test_create_rule_emits_audit(pgSession: AsyncSession) -> None:
    """createRule → outbox 'feature_rule_created' → drainOnce → AuditLog CREATE 行。"""
    svc = FeatureRuleService()
    dto = _makeCreateDto(code="test_create", policy_description="initial")
    row = await svc.createRule(pgSession, dto, _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "feature_rule",
                AuditLog.entity_id == row.id,
                AuditLog.action == "CREATE",
            )
        )
    ).scalar_one()
    assert audit.actor == "admin"
    assert audit.before_json is None
    assert audit.after_json["code"] == "test_create"
    assert audit.after_json["feature_name"] == "TEST_FEATURE"
    assert audit.after_json["policy_description"] == "initial"
    assert audit.after_json["enabled"] is True
    assert audit.after_json["version"] == 1
    # thresholds 1:N 序列化进 after_json
    assert len(audit.after_json["thresholds"]) == 1
    assert audit.after_json["thresholds"][0]["severity"] == "HIGH"


@pytest.mark.asyncio
async def test_update_rule_emits_audit(pgSession: AsyncSession) -> None:
    """updateRule → outbox 'feature_rule_updated' → AuditLog UPDATE 行（before + after）。"""
    svc = FeatureRuleService()
    row = await svc.createRule(
        pgSession,
        _makeCreateDto(code="test_update", policy_description="v1 desc"),
        _admin(),
    )
    await pgSession.commit()

    upd = FeatureRuleUpdate(
        policy_description="v2 desc",
        priority=200,
        version=row.version,
    )
    await svc.updateRule(pgSession, "test_update", upd, _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "feature_rule",
                AuditLog.entity_id == row.id,
                AuditLog.action == "UPDATE",
            )
        )
    ).scalar_one()
    assert audit.actor == "admin"
    assert audit.before_json["policy_description"] == "v1 desc"
    assert audit.before_json["version"] == 1
    assert audit.before_json["priority"] == 100
    assert audit.after_json["policy_description"] == "v2 desc"
    assert audit.after_json["priority"] == 200
    assert audit.after_json["version"] == 2


@pytest.mark.asyncio
async def test_delete_rule_emits_audit(pgSession: AsyncSession) -> None:
    """deleteRule → outbox 'feature_rule_deleted' → AuditLog DELETE 行（before only）。"""
    svc = FeatureRuleService()
    row = await svc.createRule(
        pgSession, _makeCreateDto(code="test_delete"), _admin()
    )
    await pgSession.commit()

    await svc.deleteRule(pgSession, "test_delete", _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "feature_rule",
                AuditLog.entity_id == row.id,
                AuditLog.action == "DELETE",
            )
        )
    ).scalar_one()
    assert audit.actor == "admin"
    assert audit.after_json is None
    assert audit.before_json["code"] == "test_delete"
    assert audit.before_json["enabled"] is True
    assert audit.before_json["version"] == 1


@pytest.mark.asyncio
async def test_toggle_rule_emits_audit(pgSession: AsyncSession) -> None:
    """toggleEnabled → outbox 'feature_rule_updated' → AuditLog UPDATE 行。

    toggleEnabled 复用 feature_rule_updated 事件（worker verb 映射只认
    created/updated/deleted），因此 audit_log.action == 'UPDATE'。
    """
    svc = FeatureRuleService()
    row = await svc.createRule(
        pgSession, _makeCreateDto(code="test_toggle"), _admin()
    )
    await pgSession.commit()

    await svc.toggleEnabled(pgSession, "test_toggle", False, _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "feature_rule",
                AuditLog.entity_id == row.id,
                AuditLog.action == "UPDATE",
            )
            .order_by(AuditLog.id.desc())
        )
    ).scalars().first()
    assert audit is not None
    assert audit.actor == "admin"
    # toggle 之前 enabled=True → 之后 enabled=False
    assert audit.before_json["enabled"] is True
    assert audit.after_json["enabled"] is False
    # 乐观锁版本 +1
    assert audit.after_json["version"] == 2
