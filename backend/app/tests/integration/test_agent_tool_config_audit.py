"""AgentToolConfigService outbox → audit_log 集成测试（真实 PG）。

覆盖 6 个场景：CREATE / UPDATE / DELETE / TOGGLE → audit_log 行落地，
actor_departments 注入，flush 失败无 audit（事务原子性）。

强制规则（Harness/rules/测试规范.md）：真实 PG（5433/qa_metadata_test），
Alembic upgrade head 自动建表，pgSession fixture 每测试 TRUNCATE 隔离。
"""
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.domain.exceptions import ConflictError
from app.domain.models import AgentDefinition, AgentToolConfig, AuditLog
from app.domain.schemas import AgentToolConfigCreate, AgentToolConfigUpdate
from app.dependencies import CurrentUser
from app.services.agent_tool_config_service import AgentToolConfigService
from app.tests import _pg_support
from app.workers.audit_worker import AuditWorker


_ADMIN = {"X-User-Id": "admin", "X-User-Roles": "admin"}


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


@pytest.mark.asyncio
async def test_create_emits_agent_tool_created(pgSession: AsyncSession) -> None:
    svc = AgentToolConfigService()
    dto = AgentToolConfigCreate(
        name="test_create",
        description="x",
        data_object="SUPPLIER",
        data_layers=["DIM"],
        handler_kind="BUILTIN",
        handler_ref="supplier_360",
    )
    row = await svc.createTool(pgSession, dto, _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_tool_config",
                AuditLog.entity_id == row.id,
                AuditLog.action == "CREATE",
            )
        )
    ).scalar_one()
    assert audit.actor == "admin"
    assert audit.after_json["name"] == "test_create"
    assert audit.before_json is None


@pytest.mark.asyncio
async def test_update_emits_agent_tool_updated(pgSession: AsyncSession) -> None:
    svc = AgentToolConfigService()
    row = await svc.createTool(
        pgSession,
        AgentToolConfigCreate(
            name="test_update",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        ),
        _admin(),
    )
    await pgSession.commit()

    upd = AgentToolConfigUpdate(version=row.version, description="updated")
    await svc.updateTool(pgSession, "test_update", upd, _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_tool_config",
                AuditLog.entity_id == row.id,
                AuditLog.action == "UPDATE",
            )
        )
    ).scalar_one()
    assert audit.before_json["description"] in (None, "x")
    assert audit.after_json["description"] == "updated"
    assert audit.after_json["version"] == 2


@pytest.mark.asyncio
async def test_delete_emits_agent_tool_deleted(pgSession: AsyncSession) -> None:
    svc = AgentToolConfigService()
    row = await svc.createTool(
        pgSession,
        AgentToolConfigCreate(
            name="test_delete",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        ),
        _admin(),
    )
    await pgSession.commit()

    await svc.deleteTool(pgSession, "test_delete", _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_tool_config",
                AuditLog.entity_id == row.id,
                AuditLog.action == "DELETE",
            )
        )
    ).scalar_one()
    assert audit.before_json["name"] == "test_delete"
    assert audit.after_json is None


@pytest.mark.asyncio
async def test_toggle_emits_update_audit(pgSession: AsyncSession) -> None:
    """toggleEnabled 复用 agent_tool_updated 事件（worker verb 映射只认 created/updated/deleted）。"""
    svc = AgentToolConfigService()
    row = await svc.createTool(
        pgSession,
        AgentToolConfigCreate(
            name="test_toggle",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        ),
        _admin(),
    )
    await pgSession.commit()

    await svc.toggleEnabled(pgSession, "test_toggle", False, _admin())
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_tool_config",
                AuditLog.entity_id == row.id,
                AuditLog.action == "UPDATE",
            )
            .order_by(AuditLog.id.desc())
        )
    ).scalars().first()
    assert audit is not None
    assert audit.after_json["enabled"] is False


@pytest.mark.asyncio
async def test_audit_records_actor_departments(pgSession: AsyncSession) -> None:
    svc = AgentToolConfigService()
    actor = CurrentUser(
        userId="u1", tenantId="default", roles=["admin"],
        departments=["采购部", "IT"],
    )
    row = await svc.createTool(
        pgSession,
        AgentToolConfigCreate(
            name="test_actor_dept",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        ),
        actor,
    )
    await pgSession.commit()
    await AuditWorker().drainOnce(pgSession)

    audit = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_tool_config",
                AuditLog.entity_id == row.id,
            )
        )
    ).scalar_one()
    # worker 把 tuple 转 ',' 拼接字符串（AuditLog.actor_departments 是 VARCHAR）
    assert "采购部" in (audit.actor_departments or "")
    assert "IT" in (audit.actor_departments or "")


@pytest.mark.asyncio
async def test_create_failed_flush_no_audit(pgSession: AsyncSession) -> None:
    """createTool 在 flush 失败时不应 emit audit（事务原子性：rollback 同时回滚 outbox 行）。"""
    svc = AgentToolConfigService()
    dto = AgentToolConfigCreate(
        name="test_fail",
        data_object="SUPPLIER",
        handler_kind="BUILTIN",
        handler_ref="supplier_360",
    )
    row = await svc.createTool(pgSession, dto, _admin())
    await pgSession.commit()
    first_id = row.id

    # 再次同名插入 → IntegrityError → ConflictError；outbox 行随事务回滚
    with pytest.raises(ConflictError):
        await svc.createTool(pgSession, dto, _admin())
    await pgSession.rollback()
    await AuditWorker().drainOnce(pgSession)

    audits = (
        await pgSession.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "agent_tool_config",
                AuditLog.entity_id == first_id,
                AuditLog.action == "CREATE",
            )
        )
    ).scalars().all()
    assert len(audits) == 1  # 只有第一次 create 落地 audit
