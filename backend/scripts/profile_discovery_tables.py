"""真数据探针：发现域确定性剖析（G3-1 验收）。

背景：G3-1 的剖析器（`app/services/discovery_profiler.py`）只有假适配器单测；
验收要求「在 dev 的 THBI Oracle 上对供应商、物料两表产出剖析行」。
本脚本补齐真数据这一段。

**只读**：所有查询经业务数据源只读 adapter（SQL Guard + 只读账号双闸）；
写面只碰 `discovery_*`（自动登记一个 probe 发现源 + 采集快照 + 写剖析行）。

用法::

    python -m scripts.profile_discovery_tables --tables DIM_SUPPLIER,DIM_IMATERIAL

    # 指定数据源 / 刷新快照 / 只看某几列
    python -m scripts.profile_discovery_tables --datasource 1 --refresh-snapshot \\
        --tables DIM_SUPPLIER

退出码：0 = 剖析成功；2 = 无数据源 / 快照里没有目标表 / 连接失败。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.domain.discovery_models import DiscoverySource  # noqa: E402
from app.domain.discovery_pipeline_models import DiscoveryProfile  # noqa: E402
from app.domain.models import DataSource  # noqa: E402
from app.infrastructure.database import getSessionFactory  # noqa: E402
from app.services.discovery_profiler import DiscoveryProfiler  # noqa: E402
from app.services.discovery_snapshot_service import (  # noqa: E402
    DiscoverySnapshotService,
)

_PROBE_SOURCE_PREFIX = "probe:"


def _parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasource",
        default=None,
        help="业务数据源 id 或名称（默认取 is_default 的活跃数据源）",
    )
    parser.add_argument(
        "--tables",
        required=True,
        help="逗号分隔的表名（必填：避免在几百张表上全量剖析）",
    )
    parser.add_argument("--refresh-snapshot", action="store_true", help="重新采集元数据快照")
    return parser.parse_args()


async def _resolveDatasource(session, spec: str | None) -> DataSource | None:
    statement = select(DataSource).where(DataSource.is_active.is_(True))
    if spec is not None:
        if spec.isdigit():
            statement = statement.where(DataSource.id == int(spec))
        else:
            statement = statement.where(DataSource.name == spec)
    else:
        statement = statement.where(DataSource.is_default.is_(True))
    return (await session.execute(statement.limit(1))).scalars().first()


async def _resolveSource(session, snapshotService, datasource: DataSource) -> DiscoverySource:
    source = (
        await session.execute(
            select(DiscoverySource).where(DiscoverySource.datasource_id == datasource.id)
        )
    ).scalars().first()
    if source is not None:
        print(f"[probe] 复用发现源 id={source.id} name={source.name}")
        return source
    source = await snapshotService.registerSource(
        session,
        name=f"{_PROBE_SOURCE_PREFIX}{datasource.name}"[:100],
        datasourceId=datasource.id,
        owner="probe",
        note="profile_discovery_tables.py 自动登记（真数据探针）",
        createdBy="profile-script",
    )
    print(f"[probe] 新登记发现源 id={source.id} name={source.name}")
    return source


async def _snapshotId(
    session, snapshotService, source: DiscoverySource, *, refresh: bool
) -> int:
    if not refresh:
        latest = await snapshotService.listSnapshots(session, sourceId=source.id, limit=1)
        if latest:
            print(
                f"[probe] 复用快照 id={latest[0].id} "
                f"tables={latest[0].table_count} columns={latest[0].column_count}"
            )
            return latest[0].id
    print("[probe] 采集元数据快照（只读内省）…")
    captured = await snapshotService.captureSnapshot(
        session, sourceId=source.id, createdBy="profile-script"
    )
    print(
        f"[probe] 快照 id={captured.snapshotId} tables={captured.tableCount} "
        f"columns={captured.columnCount} reused={captured.reused}"
    )
    return captured.snapshotId


async def _run(args: argparse.Namespace) -> int:
    tables = [name.strip() for name in args.tables.split(",") if name.strip()]
    if not tables:
        print("[probe] --tables 为空", file=sys.stderr)
        return 2

    factory = getSessionFactory()
    snapshotService = DiscoverySnapshotService()
    async with factory() as session:
        datasource = await _resolveDatasource(session, args.datasource)
        if datasource is None:
            print("[probe] 没有可用的活跃业务数据源", file=sys.stderr)
            return 2
        print(
            f"[probe] 数据源 id={datasource.id} name={datasource.name} "
            f"type={datasource.type}"
        )
        source = await _resolveSource(session, snapshotService, datasource)
        snapshotId = await _snapshotId(
            session, snapshotService, source, refresh=args.refresh_snapshot
        )

        try:
            result = await DiscoveryProfiler().profileTables(
                session,
                sourceId=source.id,
                snapshotId=snapshotId,
                tables=tables,
                createdBy="profile-script",
            )
        except Exception as exc:  # noqa: BLE001 - 探针：打印后以退出码 2 结束
            print(f"[probe] 剖析失败：{exc}", file=sys.stderr)
            return 2

        rows = list(
            (
                await session.execute(
                    select(DiscoveryProfile)
                    .where(DiscoveryProfile.run_id == result.runId)
                    .order_by(
                        DiscoveryProfile.table_name,
                        DiscoveryProfile.column_name.nullsfirst(),
                    )
                )
            )
            .scalars()
            .all()
        )
        print(
            f"[probe] run id={result.runId} status={result.status} "
            f"tables={result.tableCount} profileRows={result.profileRowCount}"
        )
        for row in rows:
            if row.column_name is None:
                print(f"  {row.table_name}: row_count={row.row_count}")
            else:
                print(
                    f"    - {row.column_name}: nulls={row.null_count} "
                    f"distinct={row.distinct_count} "
                    f"range=[{row.min_value}, {row.max_value}]"
                )
    return 0


def main() -> None:
    sys.exit(asyncio.run(_run(_parseArgs())))


if __name__ == "__main__":
    main()
