"""历史快照服务（Phase 4.5 governance hardening，遗留 #68）。

承载两类历史快照：
1. kpi_catalog_history：KpiCatalog 专用，revision 与 kpi.revision_count 对齐
2. feature_definition_history：FeatureDefinition 专用，每条记录存完整快照

与 audit_log 的区别：
- audit_log：通用审计，捕获变更前后差异（before/after JSON）
- history 表：实体专用完整快照，可按时间回放

事务语义：`snapshot()` / `snapshotFeature()` 只 session.add，不 commit。
调用方需 commit。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    FeatureDefinition,
    FeatureDefinitionHistory,
    KpiCatalog,
    KpiCatalogHistory,
)

logger = logging.getLogger(__name__)


class HistoryService:
    """历史快照服务（KPI + FeatureDefinition）。"""

    # ------------------------------------------------------------------
    # KPI Catalog 历史
    # ------------------------------------------------------------------

    async def snapshot(
        self,
        session: AsyncSession,
        kpi: KpiCatalog,
        changed_by: str | None = None,
    ) -> KpiCatalogHistory:
        """捕获当前 KPI 状态到历史表。revision 与 kpi.revision_count 对齐。

        调用前应已 kpi.revision_count += 1（service.updateKpi 内）。
        POST 时 revision_count 还是 0（初次创建），此时传 revision=0 即可。
        """
        snapshot_data = _snapshotDict(kpi)
        row = KpiCatalogHistory(
            kpi_id=kpi.id,
            revision=kpi.revision_count,
            snapshot_json=snapshot_data,
            changed_by=changed_by,
        )
        session.add(row)
        logger.debug(
            "KPI 历史快照写入：kpi_id=%s revision=%s by %s",
            kpi.id,
            kpi.revision_count,
            changed_by,
        )
        return row

    # ------------------------------------------------------------------
    # FeatureDefinition 历史
    # ------------------------------------------------------------------

    async def snapshotFeature(
        self,
        session: AsyncSession,
        feature: FeatureDefinition,
        changed_by: str | None = None,
    ) -> FeatureDefinitionHistory:
        """捕获当前 FeatureDefinition 状态到历史表（每次 create/update 写入）。

        feature_id FK ON DELETE SET NULL：删除特征时历史保留。
        """
        snapshot_data = _snapshotFeatureDict(feature)
        row = FeatureDefinitionHistory(
            feature_id=feature.id,
            snapshot_json=snapshot_data,
            changed_by=changed_by,
        )
        session.add(row)
        logger.debug(
            "FeatureDefinition 历史快照写入：feature_id=%s by %s",
            feature.id,
            changed_by,
        )
        return row


# ---------------------------------------------------------------------------
# Dict 构造工具（JSON 序列化安全）
# ---------------------------------------------------------------------------

def _snapshotDict(kpi: KpiCatalog) -> dict[str, Any]:
    """从 ORM 实体构造可 JSON 序列化的快照 dict（用于 JSONB 列）。

    datetime → ISO 字符串；Decimal → float；其余原样保留。
    PG JSONB 写入要求 Python json 编码成功；datetime/Decimal 原样放入会抛
    `Object of type datetime is not JSON serializable`。
    """
    out: dict[str, Any] = {}
    for col in kpi.__table__.columns.keys():
        v = getattr(kpi, col)
        if isinstance(v, datetime):
            out[col] = v.isoformat()
        elif isinstance(v, Decimal):
            out[col] = float(v)
        else:
            out[col] = v
    return out


def _snapshotFeatureDict(feature: FeatureDefinition) -> dict[str, Any]:
    """FeatureDefinition → JSON 可序列化 dict（与 _snapshotDict 同模式）。"""
    out: dict[str, Any] = {}
    for col in feature.__table__.columns.keys():
        v = getattr(feature, col)
        if isinstance(v, datetime):
            out[col] = v.isoformat()
        elif isinstance(v, Decimal):
            out[col] = float(v)
        else:
            out[col] = v
    return out
