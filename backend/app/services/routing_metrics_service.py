"""4-layer routing monitoring metrics aggregation - Task 5.2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SessionMessage

_ROUTING_LAYERS = ("L1", "L2", "L3", "L4")


@dataclass(frozen=True)
class LayerMetric:
    """Single-layer routing metric."""
    layer: str
    hit_count: int
    avg_duration_ms: float
    avg_token_cost: float


@dataclass(frozen=True)
class RoutingMetricsSnapshot:
    """Overall routing monitoring snapshot."""
    since: datetime
    until: datetime
    layer_distribution: list[LayerMetric]
    total_queries: int
    avg_total_duration_ms: float


class RoutingMetricsService:
    """Aggregates 4-layer routing hit rate / latency / token cost over a time window."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_snapshot(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
    ) -> RoutingMetricsSnapshot:
        """Aggregate 4-layer routing metrics for the [since, until] time window."""
        if until is None:
            tz = since.tzinfo if since.tzinfo else UTC
            until = datetime.now(tz)
        elif since.tzinfo is None and until.tzinfo is not None:
            until = until.replace(tzinfo=None)

        stmt = (
            select(
                SessionMessage.routing_layer.label("layer"),
                func.count(SessionMessage.id).label("hits"),
                func.avg(SessionMessage.latency_ms).label("avg_ms"),
                func.avg(SessionMessage.token_cost_usd).label("avg_cost"),
            )
            .where(SessionMessage.role == "assistant")
            .where(SessionMessage.created_time >= since)
            .where(SessionMessage.created_time <= until)
            .group_by(SessionMessage.routing_layer)
        )
        rows = (await self._session.execute(stmt)).all()

        metrics_by_layer = {
            row.layer: LayerMetric(
                layer=str(row.layer),
                hit_count=row.hits,
                avg_duration_ms=float(row.avg_ms) if row.avg_ms is not None else 0.0,
                avg_token_cost=float(row.avg_cost) if row.avg_cost is not None else 0.0,
            )
            for row in rows
            if row.layer in _ROUTING_LAYERS
        }

        layer_distribution = [
            metrics_by_layer.get(
                layer,
                LayerMetric(layer=layer, hit_count=0, avg_duration_ms=0.0, avg_token_cost=0.0),
            )
            for layer in _ROUTING_LAYERS
        ]

        total = sum(m.hit_count for m in layer_distribution)
        if total > 0:
            avg_total_ms = sum(m.avg_duration_ms * m.hit_count for m in layer_distribution) / total
        else:
            avg_total_ms = 0.0

        return RoutingMetricsSnapshot(
            since=since,
            until=until,
            layer_distribution=layer_distribution,
            total_queries=total,
            avg_total_duration_ms=avg_total_ms,
        )
