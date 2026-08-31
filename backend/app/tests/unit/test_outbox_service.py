"""OutboxService.enqueue 单元测试（feat-audit-outbox）。

真实 PG（触 DB 测试规则），fixture 来自 unit/conftest.py 的 seedEngine/dbSession。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import AuditOutbox
from app.services.outbox_service import OutboxService


@pytest.mark.asyncio
async def test_enqueue_persists_pending_row(dbSession: AsyncSession):
    """enqueue 落库一行 pending（processed_at IS NULL, attempts=0）。"""
    svc = OutboxService()

    row = await svc.enqueue(
        dbSession,
        event_type="kpi_created",
        entity_type="kpi_catalog",
        entity_id=1,
        actor="admin",
        payload={"after": {"kpi_name": "回款率"}},
    )
    await dbSession.commit()

    assert row.id is not None
    assert row.processed_at is None
    assert row.attempts == 0
    stored = (
        await dbSession.execute(select(AuditOutbox).where(AuditOutbox.id == row.id))
    ).scalar_one()
    assert stored.event_type == "kpi_created"
    assert stored.entity_id == 1
    assert stored.payload["after"]["kpi_name"] == "回款率"


@pytest.mark.asyncio
async def test_enqueue_converts_departments_tuple_to_list(dbSession: AsyncSession):
    """actor_departments tuple 序列化为 JSONB list（tuple 不可 JSON 序列化）。"""
    svc = OutboxService()

    row = await svc.enqueue(
        dbSession,
        event_type="entity_mapping_updated",
        entity_type="entity_mapping",
        entity_id=7,
        actor="alice",
        actor_departments=("procurement", "finance"),
        payload={"before": {}, "after": {}},
    )
    await dbSession.commit()
    await dbSession.refresh(row)

    assert row.actor_departments == ["procurement", "finance"]


@pytest.mark.asyncio
async def test_enqueue_allows_null_entity_id_for_delete(dbSession: AsyncSession):
    """DELETE 事件 entity_id 允许 NULL（业务行已删）。"""
    svc = OutboxService()

    row = await svc.enqueue(
        dbSession,
        event_type="kpi_deleted",
        entity_type="kpi_catalog",
        entity_id=None,
        actor="admin",
        payload={"before": {"kpi_name": "旧指标"}},
    )
    await dbSession.commit()
    assert row.entity_id is None


@pytest.mark.asyncio
async def test_enqueue_rejects_invalid_event_type(dbSession: AsyncSession):
    """event_type 必须形如 '<entity>_<created|updated|deleted>'，非法值 fail fast。"""
    svc = OutboxService()

    with pytest.raises(ValueError, match="event_type"):
        await svc.enqueue(
            dbSession,
            event_type="kpi_created!!!",
            entity_type="kpi_catalog",
            entity_id=1,
            actor="admin",
            payload={"after": {}},
        )
    with pytest.raises(ValueError, match="event_type"):
        await svc.enqueue(
            dbSession,
            event_type="kpi_upserted",
            entity_type="kpi_catalog",
            entity_id=1,
            actor="admin",
            payload={"after": {}},
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_empty_actor(dbSession: AsyncSession):
    """actor 必填（审计可追溯性底线）。"""
    svc = OutboxService()

    with pytest.raises(ValueError, match="actor"):
        await svc.enqueue(
            dbSession,
            event_type="kpi_created",
            entity_type="kpi_catalog",
            entity_id=1,
            actor="",
            payload={"after": {}},
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_entity_type_too_long(dbSession: AsyncSession):
    """entity_type 超出 50 字符 fail fast（不等 DB 截断）。"""
    svc = OutboxService()

    with pytest.raises(ValueError, match="entity_type"):
        await svc.enqueue(
            dbSession,
            event_type="kpi_created",
            entity_type="x" * 51,
            entity_id=1,
            actor="admin",
            payload={"after": {}},
        )


@pytest.mark.asyncio
async def test_enqueue_rejects_payload_too_large(dbSession: AsyncSession):
    """payload 超出 1 MB fail fast（防止 worker OOM / 存储耗尽）。"""
    svc = OutboxService()
    large_payload = {"data": "x" * 1_000_001}  # > 1 MB JSON

    with pytest.raises(ValueError, match="payload"):
        await svc.enqueue(
            dbSession,
            event_type="kpi_created",
            entity_type="kpi_catalog",
            entity_id=1,
            actor="admin",
            payload=large_payload,
        )
