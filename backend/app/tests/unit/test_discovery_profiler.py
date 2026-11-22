"""发现剖析器（WP-ONT-AI A2 / G3-1）：纯 IO 编排 + 真实 PG 落表。

钉住五件事：

1. **只读**：剖析只走 `execute_read_only`，SQL 全是 SELECT（不采样、不写业务库）；
2. **LLM 计量为 0**：G3-1 还没有 LLM 调用 ⇒ `prompt_tokens/completion_tokens/cost`
   必须是 0（G3-2 引入 LLM 时这条断言会变成「必须有计量」）；
3. **失败不留半份证据**：任一张表查询失败 ⇒ run=FAILED + 友好异常，**不写** profile 行
   （半份证据比没有更糟：它会让人以为「查过一半」）；
4. **可复现**：同一快照两次剖析 ⇒ 每个 (表, 列) 的 `sql_digest` 完全相同；
5. **域边界**：剖析结果只写 `discovery_*` 表。
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.discovery_pipeline_models import DiscoveryProfile, DiscoveryRun
from app.domain.exceptions import DataSourceError, ValidationError
from app.domain.models import DataSource
from app.infrastructure.security.crypto import encryptApiKey
from app.services.discovery_profiler import DiscoveryProfiler

_SNAPSHOT: dict[str, Any] = {
    "schemaVersion": "v2",
    "tables": [
        {
            "tableName": "SUPPLIER",
            "owner": "THBI",
            "columns": [
                {"columnName": "BPSNUM_0", "dataType": "VARCHAR2(20)"},
                {"columnName": "QTY", "dataType": "NUMBER(18,4)"},
            ],
        },
        {
            "tableName": "ITEM",
            "owner": "THBI",
            "columns": [{"columnName": "ITMREF_0", "dataType": "VARCHAR2(30)"}],
        },
    ],
}


class _FakeAdapter:
    """按 SQL 里的表名给答案；记录所有发出去的 SQL。"""

    datasourceId = 1

    def __init__(self, *, failOnTable: str | None = None) -> None:
        self.sqls: list[str] = []
        self._failOnTable = failOnTable

    async def execute_read_only(
        self, sql: str, *, maxRows: int | None = None
    ) -> list[dict[str, Any]]:
        self.sqls.append(sql)
        if self._failOnTable is not None and self._failOnTable in sql:
            raise RuntimeError(f"ORA-00942: table or view does not exist（{self._failOnTable}）")
        if "COUNT(DISTINCT" in sql:
            return [
                {
                    "N0": 1,
                    "D0": 2,
                    "MIN0": "A",
                    "MAX0": "Z",
                    "N1": 3,
                    "D1": 4,
                    "MIN1": "1.5000",
                    "MAX1": "9.0000",
                }
            ]
        if "SUPPLIER" in sql:
            return [{"TOTAL_ROWS": 10}]
        return [{"TOTAL_ROWS": 5}]

    async def dispose(self) -> None:  # pragma: no cover - 接口完整性
        return None


async def _seed(
    session: Any, *, status: str = "ACTIVE", suffix: str = ""
) -> tuple[int, int]:
    datasource = DataSource(
        name=f"PROF_SRC{suffix}",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted=encryptApiKey("secret"),
        is_active=True,
    )
    session.add(datasource)
    await session.flush()
    source = DiscoverySource(
        name=f"剖析源{suffix}",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
        status=status,
    )
    session.add(source)
    await session.flush()
    snapshot = MetadataSnapshot(
        source_id=source.id,
        schema_hash="a" * 64,
        snapshot_json=_SNAPSHOT,
        table_count=2,
        column_count=3,
    )
    session.add(snapshot)
    await session.commit()
    return source.id, snapshot.id


def _profiler(adapter: _FakeAdapter) -> DiscoveryProfiler:
    return DiscoveryProfiler(adapterProvider=lambda datasourceId, ds: adapter)


async def _runRows(session: Any, runId: int) -> list[DiscoveryProfile]:
    return list(
        (
            await session.execute(
                select(DiscoveryProfile)
                .where(DiscoveryProfile.run_id == runId)
                .order_by(DiscoveryProfile.table_name, DiscoveryProfile.column_name)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_profiles_tables_and_writes_one_row_per_table_and_column(
    dbSession: Any,
) -> None:
    sourceId, snapshotId = await _seed(dbSession)
    adapter = _FakeAdapter()

    result = await _profiler(adapter).profileTables(
        dbSession,
        sourceId=sourceId,
        snapshotId=snapshotId,
        tables=["SUPPLIER", "ITEM"],
        createdBy="tester",
    )

    assert result.status == "SUCCEEDED"
    assert result.tableCount == 2
    assert result.profileRowCount == 5  # 2 表级 + 3 列级
    rows = await _runRows(dbSession, result.runId)
    tableLevel = [row for row in rows if row.column_name is None]
    columnLevel = [row for row in rows if row.column_name is not None]
    assert {(row.table_name, row.row_count) for row in tableLevel} == {
        ("SUPPLIER", 10),
        ("ITEM", 5),
    }
    assert {row.column_name for row in columnLevel} == {"BPSNUM_0", "QTY", "ITMREF_0"}
    qty = next(row for row in columnLevel if row.column_name == "QTY")
    assert (qty.null_count, qty.distinct_count) == (3, 4)
    assert (qty.min_value, qty.max_value) == ("1.5000", "9.0000")
    assert all(len(row.sql_digest) == 64 for row in rows)


async def test_run_row_meters_no_llm_usage(dbSession: Any) -> None:
    """G3-1 还没有 LLM：token/cost 必须为 0（G3-2 会改这条断言）。"""
    sourceId, snapshotId = await _seed(dbSession)
    result = await _profiler(_FakeAdapter()).profileTables(
        dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=["ITEM"]
    )

    run = await dbSession.get(DiscoveryRun, result.runId)

    assert run.prompt_tokens == 0
    assert run.completion_tokens == 0
    assert float(run.cost) == 0.0
    assert run.status == "SUCCEEDED"
    assert run.snapshot_id == snapshotId
    assert run.finished_at is not None


async def test_defaults_to_every_table_in_the_snapshot(dbSession: Any) -> None:
    sourceId, snapshotId = await _seed(dbSession)
    adapter = _FakeAdapter()

    result = await _profiler(adapter).profileTables(
        dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=None
    )

    assert result.tableCount == 2
    assert any('"SUPPLIER"' in sql for sql in adapter.sqls)
    assert any('"ITEM"' in sql for sql in adapter.sqls)


async def test_sql_is_read_only_and_never_samples_rows(dbSession: Any) -> None:
    """只发 COUNT / 聚合；不出现 LIMIT / SAMPLE / 裸列选择。"""
    sourceId, snapshotId = await _seed(dbSession)
    adapter = _FakeAdapter()

    await _profiler(adapter).profileTables(
        dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=["SUPPLIER"]
    )

    assert adapter.sqls
    for sql in adapter.sqls:
        assert sql.upper().startswith("SELECT")
        assert "SAMPLE" not in sql.upper()
        assert "LIMIT" not in sql.upper()


async def test_same_snapshot_produces_identical_digests(dbSession: Any) -> None:
    """证据可复算：两次剖析的 (表, 列) → sql_digest 完全相同。"""
    sourceId, snapshotId = await _seed(dbSession)
    first = await _profiler(_FakeAdapter()).profileTables(
        dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=["SUPPLIER"]
    )
    second = await _profiler(_FakeAdapter()).profileTables(
        dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=["SUPPLIER"]
    )

    def _digests(rows: list[DiscoveryProfile]) -> dict[tuple[str, str | None], str]:
        return {(row.table_name, row.column_name): row.sql_digest for row in rows}

    assert _digests(await _runRows(dbSession, first.runId)) == _digests(
        await _runRows(dbSession, second.runId)
    )


# ---------------------------------------------------------------------------
# 拒绝 / 失败路径
# ---------------------------------------------------------------------------


async def test_unknown_table_is_rejected_before_any_query(dbSession: Any) -> None:
    sourceId, snapshotId = await _seed(dbSession)
    adapter = _FakeAdapter()

    with pytest.raises(ValidationError):
        await _profiler(adapter).profileTables(
            dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=["NO_SUCH_TABLE"]
        )

    assert adapter.sqls == []
    assert (
        await dbSession.execute(select(DiscoveryRun))
    ).scalars().first() is None


async def test_snapshot_of_another_source_is_rejected(dbSession: Any) -> None:
    sourceId, snapshotId = await _seed(dbSession)
    otherSourceId, _ = await _seed(dbSession, suffix="-other")

    with pytest.raises(ValidationError):
        await _profiler(_FakeAdapter()).profileTables(
            dbSession,
            sourceId=otherSourceId,
            snapshotId=snapshotId,
            tables=["ITEM"],
        )


async def test_disabled_source_is_rejected(dbSession: Any) -> None:
    sourceId, snapshotId = await _seed(dbSession, status="DISABLED")

    with pytest.raises(ValidationError):
        await _profiler(_FakeAdapter()).profileTables(
            dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=["ITEM"]
        )


async def test_adapter_failure_marks_the_run_failed_and_leaves_no_rows(
    dbSession: Any,
) -> None:
    """失败不留半份证据：run=FAILED，profile 行 0 条，异常向上抛。"""
    sourceId, snapshotId = await _seed(dbSession)

    with pytest.raises(DataSourceError):
        await _profiler(_FakeAdapter(failOnTable="ITEM")).profileTables(
            dbSession, sourceId=sourceId, snapshotId=snapshotId, tables=None
        )

    run = (
        await dbSession.execute(select(DiscoveryRun))
    ).scalars().one()
    assert run.status == "FAILED"
    assert run.error_message
    assert await _runRows(dbSession, run.id) == []
