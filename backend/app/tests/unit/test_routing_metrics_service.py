"""Task 5.2: RoutingMetricsService unit tests (TDD RED -> GREEN)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

from app.domain.models import SessionMessage
from app.services.routing_metrics_service import (
    LayerMetric,
    RoutingMetricsService,
    RoutingMetricsSnapshot,
)


@pytest_asyncio.fixture
async def seeded_messages(dbSession):
    """Seed 4 layers x 2 messages + 1 message 10 days old (outside time window)."""
    now = datetime_now_utc()
    base = now - timedelta(hours=1)

    layers = ["L1", "L2", "L3", "L4"]
    for layer_idx, layer in enumerate(layers):
        for i in range(2):
            dbSession.add(SessionMessage(
                session_id=f"sess-{layer}-{i}",
                role="assistant",
                content=f"A {layer} {i}",
                question=f"Q {layer} {i}",
                routing_layer=layer,
                latency_ms=100 + layer_idx * 10 + i * 10,
                token_cost_usd=0.001 * (i + 1),
                created_time=base + timedelta(minutes=i),
            ))

    # 10-day-old message (should be excluded by since=now-3d)
    dbSession.add(SessionMessage(
        session_id="sess-old",
        role="assistant",
        content="old A",
        question="old Q",
        routing_layer="L2",
        latency_ms=200,
        token_cost_usd=0.005,
        created_time=now - timedelta(days=10),
    ))

    await dbSession.commit()
    return dbSession


def datetime_now_utc():
    return datetime.now(UTC)


async def test_layer_distribution_aggregates_correctly(seeded_messages):
    """4 layers x 2 messages -> each LayerMetric has hit_count=2."""
    service = RoutingMetricsService(seeded_messages)
    snapshot = await service.get_snapshot(
        since=datetime_now_utc() - timedelta(days=1),
    )

    assert snapshot.total_queries == 8  # 4 layers * 2 messages
    assert len(snapshot.layer_distribution) == 4
    by_layer = {m.layer: m for m in snapshot.layer_distribution}
    assert by_layer["L1"].hit_count == 2
    assert by_layer["L2"].hit_count == 2
    assert by_layer["L3"].hit_count == 2
    assert by_layer["L4"].hit_count == 2


async def test_snapshot_respects_time_range(seeded_messages):
    """since=now-3d excludes the 10-day-old message."""
    service = RoutingMetricsService(seeded_messages)
    snapshot = await service.get_snapshot(
        since=datetime_now_utc() - timedelta(days=3),
    )
    # 4 layers * 2 messages = 8; the 10-day-old message is outside the window
    assert snapshot.total_queries == 8


async def test_layer_distribution_sorted_by_layer_name(seeded_messages):
    """layer_distribution returns layers in L1/L2/L3/L4 order."""
    service = RoutingMetricsService(seeded_messages)
    snapshot = await service.get_snapshot(since=datetime_now_utc() - timedelta(days=1))
    layers = [m.layer for m in snapshot.layer_distribution]
    assert layers == ["L1", "L2", "L3", "L4"]


async def test_snapshot_handles_empty_data(dbSession):
    """No messages -> total=0 and all 4 layers have hit_count=0."""
    service = RoutingMetricsService(dbSession)
    snapshot = await service.get_snapshot(since=datetime_now_utc() - timedelta(days=1))
    assert snapshot.total_queries == 0
    assert all(m.hit_count == 0 for m in snapshot.layer_distribution)


async def test_snapshot_computes_weighted_avg_duration(seeded_messages):
    """avg_total_duration_ms is the weighted average across all layers."""
    service = RoutingMetricsService(seeded_messages)
    snapshot = await service.get_snapshot(since=datetime_now_utc() - timedelta(days=1))
    # L1: latency 100/110, avg=105; L2: 110/120, avg=115
    # L3: 120/130, avg=125; L4: 130/140, avg=135
    # total=8, weighted = (105*2 + 115*2 + 125*2 + 135*2) / 8 = 120.0
    assert snapshot.avg_total_duration_ms == pytest.approx(120.0, rel=0.01)


async def test_snapshot_respects_until_boundary(seeded_messages):
    """Messages created after `until` are excluded."""
    service = RoutingMetricsService(seeded_messages)
    # Use a very narrow window that only includes messages up to base time
    base = datetime_now_utc() - timedelta(hours=1)
    snapshot = await service.get_snapshot(since=base - timedelta(minutes=5), until=base)
    # Only messages with created_time <= until are included
    assert snapshot.total_queries < 8


async def test_layer_metric_dataclasses_are_frozen(dbSession):
    """LayerMetric and RoutingMetricsSnapshot are immutable (frozen=True)."""
    service = RoutingMetricsService(dbSession)
    snapshot = await service.get_snapshot(since=datetime_now_utc() - timedelta(days=1))
    with pytest.raises(AttributeError):
        snapshot.total_queries = 999
    with pytest.raises(AttributeError):
        snapshot.layer_distribution[0].hit_count = 999


async def test_snapshot_includes_all_four_layers_even_when_no_data(dbSession):
    """Empty data still returns exactly 4 LayerMetric entries (L1-L4)."""
    service = RoutingMetricsService(dbSession)
    snapshot = await service.get_snapshot(since=datetime_now_utc() - timedelta(days=1))
    assert len(snapshot.layer_distribution) == 4
    layers = [m.layer for m in snapshot.layer_distribution]
    assert layers == ["L1", "L2", "L3", "L4"]
