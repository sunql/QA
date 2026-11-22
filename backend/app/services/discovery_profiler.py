"""本体发现：确定性剖析器（WP-ONT-AI A2 / G3-1）。

一次剖析 = 一条 `discovery_run` + 每张表一行（表级）+ 每列一行（列级）`discovery_profile`。

## 边界（红线）

- **只读**：全部查询经 `BusinessDbAdapter.execute_read_only`（SQL Guard + 库侧只读事务）；
  本模块**不写业务库**。
- **不采样**：只发 `COUNT(*)` / `COUNT(col)` / `COUNT(DISTINCT col)` / `MIN` / `MAX`，
  不取行级样本（D-3「只收结构不收样本」；样本要等 Q3 采样授权口径定下来）。
- **不调 LLM**：G3-1 没有任何模型调用 ⇒ `discovery_run` 的 token/cost 恒为 0。
  G3-2 引入候选生成后，计量由那边写（守卫测试会从「llmFiles == 0」改成「必须带计量」）。
- **只写 `discovery_*`**：治理表一行不碰。

## 失败不留半份证据

先跑完**所有**查询、把行攒在内存里，最后一次性落库；任何一步失败 ⇒ 只留
`discovery_run(status=FAILED, error_message=…)`，**不写** profile 行。
半份剖析比没有更糟：它会让人以为「查过一半」。

## 已知限制（显式拒绝而不是悄悄合并）

`discovery_profile` 的唯一键是 `(run_id, table_name, COALESCE(column_name,''))`，
**没有 owner 维度**。同一快照里若两个 owner 有同名表，本模块直接拒绝
（`MSG_PROFILE_DUPLICATE_TABLE`），要求按 owner 拆成两个发现源 —— 不猜「你指的是哪张」。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import DiscoveryProfile, DiscoveryRun
from app.domain.enums import DataSourceType
from app.domain.exceptions import DataSourceError, NotFoundError, ValidationError
from app.domain.models import DataSource
from app.infrastructure import business_db_pool
from app.services.discovery_profile_sql import (
    ProfileSqlError,
    buildColumnStatsSql,
    buildTableCountSql,
    chunkColumns,
    parseColumnStats,
    parseTableCount,
    profileColumnsFromSnapshot,
    sqlDigest,
)
from app.services.discovery_snapshot_service import (
    MSG_DATASOURCE_INACTIVE,
    STATUS_ACTIVE,
)

logger = logging.getLogger(__name__)

MSG_SOURCE_NOT_FOUND = "发现源 {id} 不存在"
MSG_SOURCE_DISABLED_FOR_PROFILE = "发现源已停用，不允许剖析：{id}"
MSG_RUN_NOT_FOUND = "发现运行 {runId} 不存在"
MSG_RUN_CONTEXT_MISMATCH = "发现运行 {runId} 的源/快照与本次剖析不一致"
MSG_RUN_NOT_RUNNING = "发现运行 {runId} 状态为 {status}，只有 RUNNING 可以写入剖析结果"
MSG_SNAPSHOT_NOT_FOUND = "元数据快照 {id} 不存在"
MSG_SNAPSHOT_SOURCE_MISMATCH = "元数据快照 {snapshotId} 不属于发现源 {sourceId}"
MSG_PROFILE_UNKNOWN_TABLE = "快照里没有这些表：{tables}"
MSG_PROFILE_DUPLICATE_TABLE = (
    "同一快照里存在同名表（不同 owner）：{tables} ⇒ 请按 owner 拆分发现源后再剖析"
)
MSG_PROFILE_FAILED = "剖析失败（发现源 {sourceId}）：详情见服务端日志"
MSG_PROFILE_INVALID_SNAPSHOT = "快照内容不合法，无法构造只读 SQL：{detail}"

#: 数据源类型 → 剖析 SQL 方言（只影响标识符引号）。
_DIALECT_BY_TYPE = {
    DataSourceType.ORACLE.value: "oracle",
    DataSourceType.POSTGRESQL.value: "postgresql",
    DataSourceType.MYSQL.value: "mysql",
}

_MAX_ERROR_MESSAGE = 400


@dataclass(frozen=True, slots=True)
class ProfileRunResult:
    """一次剖析的结果（不可变）。"""

    runId: int
    sourceId: int
    snapshotId: int
    tableCount: int
    profileRowCount: int
    status: str


class DiscoveryProfiler:
    """确定性剖析编排（IO 层；SQL 构造在 `discovery_profile_sql`）。"""

    def __init__(self, *, adapterProvider=business_db_pool.get_adapter) -> None:
        self._adapterProvider = adapterProvider

    async def profileTables(
        self,
        session: AsyncSession,
        *,
        sourceId: int,
        snapshotId: int,
        tables: Sequence[str] | None = None,
        createdBy: str | None = None,
        trigger: str = "MANUAL",
        runId: int | None = None,
    ) -> ProfileRunResult:
        """对快照里的表（或指定子集）做确定性剖析。

        `tables=None` ⇒ 快照里的**全部**表。表名大小写不敏感匹配，但落库用快照里的原名。

        `runId`：由调用方（异步 Worker）**预先建好**的 RUNNING 行 ⇒ 直接往里写，
        不再新建。给了 `runId` 就必须与 run 的源/快照一致且状态为 RUNNING
        （防止把剖析结果写进别人的运行）。
        """
        source, snapshot, datasource = await self._loadContext(
            session, sourceId=sourceId, snapshotId=snapshotId
        )
        selected = _selectTables(snapshot.snapshot_json, tables)
        dialect = _dialectOf(datasource)
        run = await self._resolveRun(
            session,
            runId=runId,
            sourceId=sourceId,
            snapshotId=snapshotId,
            trigger=trigger,
            createdBy=createdBy,
        )

        try:
            rows = await self._collect(
                datasource, selected, dialect=dialect, sourceId=sourceId, runId=run.id,
                snapshotId=snapshotId,
            )
        except ProfileSqlError as exc:
            await self._fail(session, run, exc, logDetails=False)
            raise ValidationError(
                MSG_PROFILE_INVALID_SNAPSHOT.format(detail=str(exc))
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 记录后统一翻译为领域异常
            await self._fail(session, run, exc, logDetails=True)
            raise DataSourceError(
                MSG_PROFILE_FAILED.format(sourceId=sourceId)
            ) from exc

        session.add_all(rows)
        run.status = "SUCCEEDED"
        run.finished_at = datetime.now(UTC)
        await session.commit()
        return ProfileRunResult(
            runId=run.id,
            sourceId=sourceId,
            snapshotId=snapshotId,
            tableCount=len(selected),
            profileRowCount=len(rows),
            status=run.status,
        )

    # ------------------------------------------------------------ 内部

    async def _resolveRun(
        self,
        session: AsyncSession,
        *,
        runId: int | None,
        sourceId: int,
        snapshotId: int,
        trigger: str,
        createdBy: str | None,
    ) -> DiscoveryRun:
        if runId is not None:
            run = await session.get(DiscoveryRun, runId)
            if run is None:
                raise NotFoundError(MSG_RUN_NOT_FOUND.format(runId=runId))
            if run.source_id != sourceId or run.snapshot_id != snapshotId:
                raise ValidationError(
                    MSG_RUN_CONTEXT_MISMATCH.format(runId=runId)
                )
            if run.status != "RUNNING":
                raise ValidationError(
                    MSG_RUN_NOT_RUNNING.format(runId=runId, status=run.status)
                )
            return run
        run = DiscoveryRun(
            source_id=sourceId,
            snapshot_id=snapshotId,
            trigger=trigger,
            status="RUNNING",
            started_at=datetime.now(UTC),
            created_by=createdBy,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run

    async def _loadContext(
        self, session: AsyncSession, *, sourceId: int, snapshotId: int
    ) -> tuple[DiscoverySource, MetadataSnapshot, DataSource]:
        source = await session.get(DiscoverySource, sourceId)
        if source is None:
            raise NotFoundError(MSG_SOURCE_NOT_FOUND.format(id=sourceId))
        if source.status != STATUS_ACTIVE:
            raise ValidationError(MSG_SOURCE_DISABLED_FOR_PROFILE.format(id=sourceId))
        snapshot = await session.get(MetadataSnapshot, snapshotId)
        if snapshot is None:
            raise NotFoundError(MSG_SNAPSHOT_NOT_FOUND.format(id=snapshotId))
        if snapshot.source_id != sourceId:
            raise ValidationError(
                MSG_SNAPSHOT_SOURCE_MISMATCH.format(
                    snapshotId=snapshotId, sourceId=sourceId
                )
            )
        datasource = await session.get(DataSource, source.datasource_id)
        if datasource is None or not datasource.is_active:
            raise ValidationError(
                MSG_DATASOURCE_INACTIVE.format(id=source.datasource_id)
            )
        return source, snapshot, datasource

    async def _collect(
        self,
        datasource: DataSource,
        selected: Sequence[Mapping[str, Any]],
        *,
        dialect: str,
        sourceId: int,
        runId: int,
        snapshotId: int,
    ) -> list[DiscoveryProfile]:
        """跑完全部查询，返回待落库的行（失败时一行都不返回）。"""
        adapter = self._adapterProvider(datasource.id, datasource)
        rows: list[DiscoveryProfile] = []
        for table in selected:
            tableName = str(table["tableName"])
            owner = str(table.get("owner") or "") or None
            countSql = buildTableCountSql(tableName, owner, dialect=dialect)
            rowCount = parseTableCount(
                await adapter.execute_read_only(countSql)
            )
            rows.append(
                _profileRow(
                    runId=runId,
                    sourceId=sourceId,
                    snapshotId=snapshotId,
                    tableName=tableName,
                    columnName=None,
                    rowCount=rowCount,
                    digest=sqlDigest(countSql),
                )
            )
            rows.extend(
                await self._columnRows(
                    adapter,
                    table,
                    tableName=tableName,
                    owner=owner,
                    dialect=dialect,
                    sourceId=sourceId,
                    runId=runId,
                    snapshotId=snapshotId,
                    rowCount=rowCount,
                )
            )
        return rows

    async def _columnRows(
        self,
        adapter: Any,
        table: Mapping[str, Any],
        *,
        tableName: str,
        owner: str | None,
        dialect: str,
        sourceId: int,
        runId: int,
        snapshotId: int,
        rowCount: int,
    ) -> list[DiscoveryProfile]:
        columns = profileColumnsFromSnapshot(table)
        rows: list[DiscoveryProfile] = []
        for chunk in chunkColumns(columns):
            sql = buildColumnStatsSql(tableName, owner, chunk, dialect=dialect)
            chunkStats = parseColumnStats(await adapter.execute_read_only(sql), chunk)
            digest = sqlDigest(sql)
            rows.extend(
                _columnRow(
                    runId=runId,
                    sourceId=sourceId,
                    snapshotId=snapshotId,
                    tableName=tableName,
                    stats=item,
                    rowCount=rowCount,
                    digest=digest,
                )
                for item in chunkStats
            )
        return rows

    async def _fail(
        self,
        session: AsyncSession,
        run: DiscoveryRun,
        exc: Exception,
        *,
        logDetails: bool,
    ) -> None:
        """把失败写进 run（证据先行），再让调用方抛领域异常。"""
        if logDetails:
            logger.error("发现剖析失败（run=%s）: %s", run.id, exc, exc_info=True)
        run.status = "FAILED"
        run.finished_at = datetime.now(UTC)
        run.error_message = _shortError(exc)
        await session.commit()


# ---------------------------------------------------------------------------
# 内部：选择与构造
# ---------------------------------------------------------------------------


def _selectTables(
    snapshotJson: Mapping[str, Any], tables: Sequence[str] | None
) -> list[Mapping[str, Any]]:
    """按名字挑表（大小写不敏感），并拒绝「同名不同 owner」。"""
    allTables = [
        table
        for table in (snapshotJson.get("tables") or [])
        if isinstance(table, Mapping) and table.get("tableName")
    ]
    if not allTables:
        raise ValidationError("快照里没有任何表，无法剖析")
    byName: dict[str, list[Mapping[str, Any]]] = {}
    for table in allTables:
        byName.setdefault(str(table["tableName"]).lower(), []).append(table)
    duplicates = sorted(name for name, items in byName.items() if len(items) > 1)
    if duplicates:
        owners = sorted(
            f"{table.get('owner') or '?'}.{table.get('tableName')}"
            for name in duplicates
            for table in byName[name]
        )
        raise ValidationError(MSG_PROFILE_DUPLICATE_TABLE.format(tables="、".join(owners)))

    if tables is None:
        selected = list(allTables)
    else:
        # 去重保序：同一张表点名两次会在唯一索引上撞（同一 run 同一 (表, 列)）。
        wanted = list(
            dict.fromkeys(
                str(name).strip().lower() for name in tables if str(name).strip()
            )
        )
        missing = [name for name in wanted if name not in byName]
        if missing:
            raise ValidationError(
                MSG_PROFILE_UNKNOWN_TABLE.format(tables="、".join(missing))
            )
        selected = [byName[name][0] for name in wanted]
    return sorted(selected, key=lambda table: (str(table.get("owner") or ""), str(table["tableName"])))


def _dialectOf(datasource: DataSource) -> str:
    # 列是 String ⇒ 读回来是 str；但调用方也可能传枚举实例
    # （`class DataSourceType(str, Enum)` 的 `str()` 是 `DataSourceType.ORACLE`）⇒ 取 `.value`。
    rawType = getattr(datasource.type, "value", datasource.type)
    datasourceType = str(rawType).lower()
    dialect = _DIALECT_BY_TYPE.get(datasourceType)
    if dialect is None:
        raise ValidationError(f"不支持的数据源类型：{datasource.type}")
    return dialect


def _profileRow(
    *,
    runId: int,
    sourceId: int,
    snapshotId: int,
    tableName: str,
    columnName: str | None,
    rowCount: int,
    digest: str,
    nullCount: int | None = None,
    distinctCount: int | None = None,
    minValue: str | None = None,
    maxValue: str | None = None,
) -> DiscoveryProfile:
    return DiscoveryProfile(
        run_id=runId,
        source_id=sourceId,
        snapshot_id=snapshotId,
        table_name=tableName,
        column_name=columnName,
        row_count=rowCount,
        null_count=nullCount,
        distinct_count=distinctCount,
        min_value=minValue,
        max_value=maxValue,
        sample_values=None,
        sql_digest=digest,
    )


def _columnRow(
    *,
    runId: int,
    sourceId: int,
    snapshotId: int,
    tableName: str,
    stats: Any,
    rowCount: int,
    digest: str,
) -> DiscoveryProfile:
    return _profileRow(
        runId=runId,
        sourceId=sourceId,
        snapshotId=snapshotId,
        tableName=tableName,
        columnName=stats.column,
        rowCount=rowCount,
        digest=digest,
        nullCount=stats.nullCount,
        distinctCount=stats.distinctCount,
        minValue=stats.minValue,
        maxValue=stats.maxValue,
    )


def _shortError(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {text}"[:_MAX_ERROR_MESSAGE]
