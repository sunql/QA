"""KPI Catalog 历史快照服务（Phase 4.5 governance hardening，遗留 #68）。

按 revision 顺序保留 kpi_catalog 每次变更的完整快照。`kpi_id` FK ON DELETE SET NULL：
删除 KPI 时历史保留，仅 kpi_id 置 NULL。

与 audit_log 的区别：
- audit_log：通用审计，捕获任意实体的变更；只存差异维度（before/after）
- kpi_catalog_history：实体专用历史，存完整快照；可按 revision 回放

事务语义：`snapshot()` 只 session.add，不 commit。调用方需 commit。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, KpiCatalogHistory

logger = logging.getLogger(__name__)


class HistoryService:
    """KPI Catalog 历史快照服务。"""

    async def snapshot(
        self,
        session: AsyncSession,
        kpi: KpiCatalog,
        changed_by: str | None = None,
    ) -> KpiCatalogHistory:
        """捕获当前 KPI 状态到历史表。revision 与 kpi.revision_count 对齐。

        调用前应已 kpi.revision_count += 1（service.updateKpi 内）。POST 时
        revision_count 还是 0（初次创建），此时传 revision=0 即可。
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