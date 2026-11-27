"""发现域：**不可剖析对象**（Oracle 回收站等）的过滤与跳过。

回归来历（2026-11-27，用户实测）：本体发现触发运行时选择表，反复报
``ProfileSqlError: 表名不是合法标识符：'BIN$W81vJmasuczgY0bNqMBvog==$0'``，
**一次就废掉整轮运行**（Oracle 回收站对象含 `=` ⇒ 不在标识符白名单内）。

本文件钉住四件事：

1. 标识符白名单是**单一事实源**（`domain.sql_identifier`），SQL 护栏与内省过滤同源；
2. 内省**出口**过滤掉不可作标识符的表 ⇒ 新快照/缓存/选择器都干净；
3. 剖析层**单表跳过**（不是整轮失败）⇒ 老快照也安全；
4. 快照读接口给出「不可剖析表名」清单 ⇒ 前端不显示它们。

只标识符类失败才跳过；其它 `ProfileSqlError` 仍整轮失败（跳过等于静默产出空结果）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.exceptions import ValidationError
from app.domain.models import DataSource
from app.domain.sql_identifier import isValidIdentifier
from app.services.discovery_profile_sql import (
    ProfileIdentifierError,
    ProfileSqlError,
    buildColumnStatsSql,
    profileColumnsFromSnapshot,
)
from app.services.discovery_profiler import DiscoveryProfiler
from app.services.discovery_snapshot_service import unprofilableTableNames
from app.services.schema_introspection_service import SchemaIntrospectionService

#: Oracle 回收站对象（drop 后的表）：名字含 `=` ⇒ 必然被护栏拒拼。
RECYCLE_BIN_TABLE = "BIN$W81vJmasuczgY0bNqMBvog==$0"

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
            "tableName": RECYCLE_BIN_TABLE,
            "owner": "THBI",
            "columns": [{"columnName": "COL1", "dataType": "VARCHAR2(30)"}],
        },
    ],
}


# ---------------------------------------------------------------------------
# 1. 白名单口径
# ---------------------------------------------------------------------------


def test_recycle_bin_name_is_not_a_valid_identifier() -> None:
    """回收站对象名不是合法标识符（这是问题的根，不是随机故障）。"""
    assert isValidIdentifier(RECYCLE_BIN_TABLE) is False


def test_normal_names_are_valid_identifiers() -> None:
    """正常表名 / 列名 / 带 `$` `#` 的 Oracle 名（Oracle 允许）仍然合法。"""
    assert isValidIdentifier("SUPPLIER") is True
    assert isValidIdentifier("DWD_PURCHASE_ORDER_DTL") is True
    assert isValidIdentifier("T$BLD#X") is True
    assert isValidIdentifier("_PRIVATE") is True


def test_invalid_identifier_shapes_are_rejected() -> None:
    """空串 / 非字符串 / 超长 / 含引号或空格的 ⇒ 一律非法。"""
    assert isValidIdentifier("") is False
    assert isValidIdentifier(None) is False
    assert isValidIdentifier(123) is False
    assert isValidIdentifier("A" * 129) is False
    assert isValidIdentifier('A"B') is False
    assert isValidIdentifier("A B") is False
    assert isValidIdentifier("A.B") is False


def test_identifier_error_is_a_subclass_of_profile_sql_error() -> None:
    """`ProfileIdentifierError` 是 `ProfileSqlError` 的子类 ⇒ 既有处理器行为不变。"""
    assert issubclass(ProfileIdentifierError, ProfileSqlError)


# ---------------------------------------------------------------------------
# 2. 内省出口过滤
# ---------------------------------------------------------------------------


def test_introspection_filters_out_non_identifier_tables() -> None:
    """内省出口把不可作标识符的表过滤掉（schema 缓存 / 快照 / 选择器都干净）。"""
    merged = {
        "SUPPLIER": {
            "owner": "THBI",
            "columns": [{"column_name": "QTY", "data_type": "NUMBER", "nullable": True}],
            "primary_keys": [],
            "foreign_keys": [],
        },
        RECYCLE_BIN_TABLE: {
            "owner": "THBI",
            "columns": [{"column_name": "COL1", "data_type": "VARCHAR2", "nullable": True}],
            "primary_keys": [],
            "foreign_keys": [],
        },
    }

    tables = SchemaIntrospectionService._assembleTables(merged)

    assert [table.table_name for table in tables] == ["SUPPLIER"]


# ---------------------------------------------------------------------------
# 2.5 整列不可聚合（ORA-00932，CLOB 连 COUNT 都不允许）
# ---------------------------------------------------------------------------


def test_clob_column_is_wholly_unaggregatable() -> None:
    """CLOB 列连 `COUNT(col)` 都不被 Oracle 允许 ⇒ 整列不进剖析 SQL。

    回归来历（2026-11-27 用户实测）：旧裁剪只去掉 `COUNT(DISTINCT)/MIN/MAX`，
    留下了空值计数 `COUNT(*) - COUNT(col)` ⇒ 含 CLOB 列的表全量剖析必炸
    （THBI 实测 38 个 CLOB 列；炸点 `ODS_SRM_*.REMARK`）。
    """
    columns = profileColumnsFromSnapshot(
        {"columns": [{"columnName": "REMARK", "dataType": "CLOB"}]}
    )
    assert columns[0].supportsNullCount is False
    assert columns[0].supportsDistinct is False
    assert columns[0].supportsRange is False


def test_undefined_type_is_wholly_unaggregatable() -> None:
    """`UNDEFINED`（Oracle 19c 对不可识别类型的占位）一并整列跳过。"""
    columns = profileColumnsFromSnapshot(
        {"columns": [{"columnName": "MYSTERY", "dataType": "UNDEFINED"}]}
    )
    assert columns[0].supportsNullCount is False


def test_normal_types_keep_null_count() -> None:
    """普通类型（VARCHAR2/NUMBER/DATE）空值计数保留。"""
    columns = profileColumnsFromSnapshot(
        {
            "columns": [
                {"columnName": "NAME", "dataType": "VARCHAR2(100)"},
                {"columnName": "QTY", "dataType": "NUMBER(18,4)"},
                {"columnName": "DAY", "dataType": "DATE"},
            ]
        }
    )
    assert all(column.supportsNullCount for column in columns)


def test_build_column_stats_sql_rejects_unaggregatable_column() -> None:
    """构造层防御：不可聚合列混进来 ⇒ 抛错（不静默产出会炸的 SQL）。"""
    column = profileColumnsFromSnapshot(
        {"columns": [{"columnName": "REMARK", "dataType": "CLOB"}]}
    )[0]
    with pytest.raises(ProfileSqlError, match="整列不可聚合"):
        buildColumnStatsSql("T", None, [column], dialect="oracle")


def test_null_count_only_column_produces_count_only_sql() -> None:
    """supportsDistinct=False 但 supportsNullCount=True ⇒ 只有空值计数。

    什么类型落在这个区间：**结构化但可 COUNT 的类型**（PG 的
    `JSON`/`JSONB`/`BYTEA` 在 `_UNSUPPORTED_DISTINCT` 里 ⇒ 不可 DISTINCT，
    但不在 `_UNAGGREGATABLE_COLUMNS` 之外……）——实际上本仓把 JSON 家族也归入
    「整列不可聚合」（Oracle 侧 CLOB 同族；PG 侧 `COUNT(jsonb)` 也有歧义）⇒
    这个区间的现成样本是 **BOOLEAN**（不可 MIN/MAX，但 `COUNT(col)` 合法）。
    """
    column = profileColumnsFromSnapshot(
        {"columns": [{"columnName": "FLAG", "dataType": "BOOLEAN"}]}
    )[0]
    assert column.supportsNullCount is True
    assert column.supportsDistinct is True
    assert column.supportsRange is False
    sql = buildColumnStatsSql("T", None, [column], dialect="postgresql")
    assert 'COUNT(*) - COUNT("FLAG")' in sql
    assert 'COUNT(DISTINCT "FLAG")' in sql
    assert "MIN(" not in sql and "MAX(" not in sql


# ---------------------------------------------------------------------------
# 3. 快照读接口的「不可剖析表名」清单
# ---------------------------------------------------------------------------


def test_unprofilable_table_names_lists_recycle_bin_objects() -> None:
    """清单含回收站表；正常表不在清单里。"""
    names = unprofilableTableNames(_SNAPSHOT)
    assert names == [RECYCLE_BIN_TABLE]


def test_unprofilable_table_names_covers_owner_and_column_violations() -> None:
    """属主 / 列名非法也算不可剖析（它们同样会被护栏拒拼）。"""
    snapshot = {
        "tables": [
            {"tableName": "OK_TABLE", "owner": "THBI", "columns": []},
            {"tableName": "BAD_OWNER_TABLE", "owner": "OW=NER", "columns": []},
            {
                "tableName": "BAD_COLUMN_TABLE",
                "owner": "THBI",
                "columns": [{"columnName": "COL=1"}],
            },
        ]
    }
    assert unprofilableTableNames(snapshot) == ["BAD_OWNER_TABLE", "BAD_COLUMN_TABLE"]


def test_unprofilable_table_names_is_empty_for_normal_snapshot() -> None:
    """正常快照 ⇒ 空清单（前端不显示任何「已隐藏」提示）。"""
    assert unprofilableTableNames({"tables": [{"tableName": "SUPPLIER", "owner": "THBI"}]}) == []
    assert unprofilableTableNames({}) == []
    assert unprofilableTableNames({"tables": "not-a-list"}) == []


# ---------------------------------------------------------------------------
# 4. 剖析层单表跳过（老快照也安全）
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """按 SQL 里的表名给答案；记录所有发出去的 SQL。"""

    datasourceId = 1

    def __init__(self) -> None:
        self.sqls: list[str] = []

    async def execute_read_only(
        self, sql: str, *, maxRows: int | None = None
    ) -> list[dict[str, Any]]:
        self.sqls.append(sql)
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


async def _seed(session: Any) -> tuple[int, int]:
    datasource = DataSource(
        name="UNPROFILABLE_SRC",
        type="oracle",
        host="h",
        port=1521,
        database_name="THBI",
        username="THBI",
        password_encrypted="dummy-ciphertext-not-decrypted-in-tests",
        is_active=True,
    )
    session.add(datasource)
    await session.flush()
    source = DiscoverySource(
        name="不可剖析源",
        datasource_id=datasource.id,
        owner="data-owner",
        access_policy={"mode": "READ_ONLY"},
        status="ACTIVE",
    )
    session.add(source)
    await session.flush()
    snapshot = MetadataSnapshot(
        source_id=source.id,
        schema_hash="b" * 64,
        snapshot_json=_SNAPSHOT,
        table_count=2,
        column_count=3,
    )
    session.add(snapshot)
    await session.commit()
    return source.id, snapshot.id


@pytest.mark.asyncio
async def test_profiler_fails_loudly_when_every_table_is_unprofilable(
    dbSession: Any,
) -> None:
    """选中的表**全部**不可剖析 ⇒ 明确失败，**不报成功**。

    否则会「零证据成功」：用户以为跑过了，实际一条 profile 都没落 ⇒ 比报错更糟。
    """
    sourceId, snapshotId = await _seed(dbSession)
    adapter = _FakeAdapter()

    with pytest.raises(ValidationError) as excInfo:
        await DiscoveryProfiler(
            adapterProvider=lambda datasourceId, ds: adapter
        ).profileTables(
            dbSession,
            sourceId=sourceId,
            snapshotId=snapshotId,
            tables=[RECYCLE_BIN_TABLE],
            createdBy="tester",
        )

    assert "不可剖析" in str(excInfo.value)


@pytest.mark.asyncio
async def test_profiler_skips_unprofilable_table_and_keeps_going(dbSession: Any) -> None:
    """一张不可剖析的表 ⇒ **跳过它**，其余表照常剖析，整轮**成功**。

    回归前：一张回收站表就 `ProfileSqlError` ⇒ 整轮 FAILED ⇒ 用户反复触发反复报错。
    """
    sourceId, snapshotId = await _seed(dbSession)
    adapter = _FakeAdapter()

    result = await DiscoveryProfiler(
        adapterProvider=lambda datasourceId, ds: adapter
    ).profileTables(
        dbSession,
        sourceId=sourceId,
        snapshotId=snapshotId,
        tables=["SUPPLIER", RECYCLE_BIN_TABLE],
        createdBy="tester",
    )

    assert result.status == "SUCCEEDED"
    assert result.tableCount == 2
    # 正常表 SUPPLIER：1 条表级 + 2 条列级；回收站表：0 条（被跳过）
    assert result.profileRowCount == 3
    assert all("BIN$" not in sql for sql in adapter.sqls)
