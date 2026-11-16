"""本体发现：不可变快照服务（WP-ONT-AI A1-2）单测。

钉住三组性质：

1. **规范化确定性**：同一份 schema 内容，无论表/列的输入顺序如何，规范化结果与
   内容哈希都**逐字相同**（否则哈希会随查询顺序抖动 ⇒ 内容寻址失效）。
2. **内容寻址幂等**：同一源 + 同一内容 ⇒ 复用既有快照行（`reused=True`），
   **不新增行**；内容变化 ⇒ **新增**一行（旧行不动，只增不改）。
3. **只读与授权边界**：发现源停用 / 策略非只读 ⇒ 拒绝采集（发现域不得写业务库）。

本文件在**真实 PostgreSQL** 上跑（unit 夹具即真实 PG，见 `unit/conftest.py`）；
业务库侧用注入的假内省服务（真实业务库连接由 `scripts/ontology_discovery_realdata.py`
与集成测试覆盖）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot
from app.domain.exceptions import ConflictError, ValidationError
from app.domain.models import DataSource
from app.domain.schemas import ColumnSchemaRead, ForeignKeySchemaRead, TableSchemaRead
from app.domain.semantic_manifest_schema import manifestHash
from app.services.discovery_snapshot_service import (
    ConnectOverride,
    DiscoverySnapshotService,
    computeSnapshotHash,
    normalizeSnapshot,
    withConnectOverride,
)

# ---------------------------------------------------------------- 夹具


def _table(name: str, *, columns=None, pks=None, fks=None, owner: str = "", comment=None):
    return TableSchemaRead(
        table_name=name,
        owner=owner,
        columns=columns or [],
        primary_keys=pks or [],
        foreign_keys=fks or [],
        comment=comment,
    )


def _column(
    name: str,
    dataType: str = "VARCHAR",
    nullable: bool = True,
    comment=None,
    *,
    numeric_precision: int | None = None,
    numeric_scale: int | None = None,
    character_max_length: int | None = None,
):
    return ColumnSchemaRead(
        column_name=name,
        data_type=dataType,
        nullable=nullable,
        comment=comment,
        numeric_precision=numeric_precision,
        numeric_scale=numeric_scale,
        character_max_length=character_max_length,
    )


async def _seedDatasource(
    session, *, name: str = "发现演示源", type: str = "mysql"
) -> DataSource:
    ds = DataSource(
        name=name,
        type=type,
        host="127.0.0.1",
        port=3306,
        database_name="demo",
        username="demo",
        password_encrypted="x",
        is_active=True,
        is_default=False,
    )
    session.add(ds)
    await session.flush()
    return ds


class _FakeIntrospection:
    """假内省服务：返回预置的表清单（业务库侧由真实脚本覆盖）。"""

    def __init__(self, tables: list[TableSchemaRead]) -> None:
        self.tables = tables
        self.calls: list[tuple[int, str | None]] = []
        self.seenEndpoints: list[tuple[str, int]] = []

    async def introspect(self, ds: DataSource, *, owner: str | None = None) -> list[TableSchemaRead]:
        self.calls.append((ds.id, owner))
        self.seenEndpoints.append((ds.host, ds.port))
        return list(self.tables)


def _service(tables: list[TableSchemaRead]) -> DiscoverySnapshotService:
    return DiscoverySnapshotService(introspectionService=_FakeIntrospection(tables))


async def _nativeJson(session, snapshotId: int) -> str:
    """**原生 SQL** 读快照 JSON 文本。

    同一 session 里 `session.get()` 可能拿回 identity map 里的同一个实例 ⇒
    「只增不改」的断言会变成「和它自己比」⇒ 恒过、不可红。原生读永远打到库。
    """

    result = await session.execute(
        text("SELECT snapshot_json::text FROM metadata_snapshot WHERE id = :id"),
        {"id": snapshotId},
    )
    return result.scalar_one()


# ---------------------------------------------------------------- 规范化


def test_规范化输出结构完整():
    tables = [
        _table(
            "订单",
            columns=[_column("id", "BIGINT", nullable=False), _column("金额", "DECIMAL")],
            pks=["id"],
            fks=[ForeignKeySchemaRead(column_name="客户号", ref_table="客户", ref_column="id")],
            owner="THBI",
            comment="订单主表",
        )
    ]

    snapshot = normalizeSnapshot(tables)

    assert snapshot["schemaVersion"] == "v2"
    table = snapshot["tables"][0]
    assert table["tableName"] == "订单"
    assert table["owner"] == "THBI"
    assert table["comment"] == "订单主表"
    assert table["primaryKeys"] == ["id"]
    assert table["columns"][0] == {
        "columnName": "id",
        "dataType": "BIGINT",
        "nullable": False,
        "comment": None,
        "numericPrecision": None,
        "numericScale": None,
        "characterMaxLength": None,
    }
    assert table["foreignKeys"] == [
        {"columnName": "客户号", "refTable": "客户", "refColumn": "id"}
    ]


def test_规范化对输入顺序不敏感():
    columnsA = [_column("a"), _column("b")]
    columnsB = [_column("c")]
    first = normalizeSnapshot(
        [
            _table("T1", columns=columnsA),
            _table("T2", columns=columnsB),
        ]
    )
    second = normalizeSnapshot(
        [
            _table("T2", columns=list(reversed(columnsB))),
            _table("T1", columns=list(reversed(columnsA))),
        ]
    )

    assert first == second
    assert computeSnapshotHash(first) == computeSnapshotHash(second)


def test_规范化保留可空性与注释():
    snapshot = normalizeSnapshot(
        [_table("T", columns=[_column("x", "INT", nullable=False, comment="主键列")])]
    )

    column = snapshot["tables"][0]["columns"][0]
    assert column["nullable"] is False
    assert column["comment"] == "主键列"


def test_规范化空输入得到空表列表():
    snapshot = normalizeSnapshot([])

    assert snapshot["tables"] == []
    assert len(computeSnapshotHash(snapshot)) == 64


def test_快照哈希与manifest同一把尺子():
    """D-2：快照哈希与 Manifest 哈希同口径（同一把尺子）。"""

    snapshot = normalizeSnapshot([_table("T", columns=[_column("a")])])

    assert computeSnapshotHash(snapshot) == manifestHash(snapshot)


def test_快照哈希对内容变化敏感():
    base = normalizeSnapshot([_table("T", columns=[_column("a", "INT")])])
    changed = normalizeSnapshot([_table("T", columns=[_column("a", "VARCHAR")])])

    assert computeSnapshotHash(base) != computeSnapshotHash(changed)


# ---------------------------------------------------------------- 精度 / 长度（L-9）
#
# 旧口径下 `dataType` 只带**类型名**（"NUMBER" / "VARCHAR"）⇒ NUMBER(10,2) 与
# NUMBER(10,4)、VARCHAR(10) 与 VARCHAR(50) 采集出的快照**逐字相同** ⇒ 哈希相同
# ⇒ 发现引擎看不见「精度 / 长度变了」这类变更。
# ⇒ 精度与长度独立成字段进快照：可分别判「类型变了」与「精度变了」。


def test_仅数值精度不同哈希不同():
    base = normalizeSnapshot(
        [_table("T", columns=[_column("a", "NUMBER", numeric_precision=10, numeric_scale=2)])]
    )
    changed = normalizeSnapshot(
        [_table("T", columns=[_column("a", "NUMBER", numeric_precision=10, numeric_scale=4)])]
    )

    assert computeSnapshotHash(base) != computeSnapshotHash(changed)


def test_仅字符长度不同哈希不同():
    base = normalizeSnapshot(
        [_table("T", columns=[_column("a", "VARCHAR", character_max_length=10)])]
    )
    changed = normalizeSnapshot(
        [_table("T", columns=[_column("a", "VARCHAR", character_max_length=50)])]
    )

    assert computeSnapshotHash(base) != computeSnapshotHash(changed)


def test_快照列携带精度值():
    snapshot = normalizeSnapshot(
        [_table("T", columns=[_column("a", "NUMBER", numeric_precision=12, numeric_scale=3)])]
    )

    column = snapshot["tables"][0]["columns"][0]
    assert column["numericPrecision"] == 12
    assert column["numericScale"] == 3
    assert column["characterMaxLength"] is None


def test_快照列恒含精度三键():
    """采集不到精度时也要有键（值为 None）⇒ 快照 JSON 形状稳定、跨版本可比。

    与 `comment` 的既有口径一致（无注释 ⇒ 键存在、值为 None）。
    """

    snapshot = normalizeSnapshot([_table("T", columns=[_column("a")])])

    column = snapshot["tables"][0]["columns"][0]
    assert column["numericPrecision"] is None
    assert column["numericScale"] is None
    assert column["characterMaxLength"] is None


# ---------------------------------------------------------------- 服务


async def test_登记发现源落库(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    source = await service.registerSource(
        dbSession,
        name="演示发现源",
        datasourceId=ds.id,
        owner="张业务",
        accessPolicy={"mode": "READ_ONLY"},
        createdBy="admin",
    )
    await dbSession.commit()

    assert source.id is not None
    assert source.datasource_id == ds.id
    assert source.status == "ACTIVE"
    assert source.access_policy == {"mode": "READ_ONLY"}


async def test_发现源名称重复被拒(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    await service.registerSource(
        dbSession, name="重名源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ConflictError):
        await service.registerSource(
            dbSession, name="重名源", datasourceId=ds.id, owner="李业务"
        )


async def test_首次采集新增快照行(dbSession):
    ds = await _seedDatasource(dbSession)
    service = _service([_table("订单", columns=[_column("id", "BIGINT")])])
    source = await service.registerSource(
        dbSession, name="采集源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    result = await service.captureSnapshot(dbSession, sourceId=source.id, createdBy="admin")
    await dbSession.commit()

    assert result.reused is False
    assert result.tableCount == 1
    assert result.columnCount == 1
    row = await dbSession.get(MetadataSnapshot, result.snapshotId)
    assert row is not None
    assert row.schema_hash == result.schemaHash
    assert row.source_id == source.id


async def test_重复采集同内容复用既有行(dbSession):
    """内容寻址幂等：同源同内容 ⇒ 复用同一行，不新增。"""

    ds = await _seedDatasource(dbSession)
    service = _service([_table("订单", columns=[_column("id", "BIGINT")])])
    source = await service.registerSource(
        dbSession, name="幂等源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    first = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()
    second = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    assert second.reused is True
    assert second.snapshotId == first.snapshotId
    assert second.schemaHash == first.schemaHash
    count = await dbSession.scalar(
        select(func.count()).select_from(MetadataSnapshot).where(
            MetadataSnapshot.source_id == source.id
        )
    )
    assert count == 1


async def test_内容变化新增一行且旧行不变(dbSession):
    """只增不改：内容变了是**新增**一行，旧行保持原样。"""

    ds = await _seedDatasource(dbSession)
    fake = _FakeIntrospection([_table("订单", columns=[_column("id", "BIGINT")])])
    service = DiscoverySnapshotService(introspectionService=fake)
    source = await service.registerSource(
        dbSession, name="演进源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    first = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    fake.tables = [_table("订单", columns=[_column("id", "BIGINT"), _column("金额")])]
    second = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    assert second.reused is False
    assert second.snapshotId != first.snapshotId
    assert second.columnCount == 2
    old = await dbSession.get(MetadataSnapshot, first.snapshotId)
    assert old is not None
    assert old.column_count == 1
    assert old.schema_hash == first.schemaHash


async def test_仅精度变化新增一行且旧行不变(dbSession):
    """L-9 的正面答复（DB 层）：只改列精度 ⇒ 新增快照行，旧行逐字不变。

    与 `test_内容变化新增一行且旧行不变` 的差别：那条改的是**列集合**（多一列），
    这条**列集合完全相同**、只有 `DECIMAL(10,2) → DECIMAL(10,4)` 的精度不同
    ⇒ 旧口径下两次采集哈希相同（看不见变更）；现在必须新增一行。
    """

    ds = await _seedDatasource(dbSession)
    fake = _FakeIntrospection(
        [_table("订单", columns=[_column("金额", "DECIMAL", numeric_precision=10, numeric_scale=2)])]
    )
    service = DiscoverySnapshotService(introspectionService=fake)
    source = await service.registerSource(
        dbSession, name="精度演进源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    first = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()
    oldJson = await _nativeJson(dbSession, first.snapshotId)

    fake.tables = [
        _table("订单", columns=[_column("金额", "DECIMAL", numeric_precision=10, numeric_scale=4)])
    ]
    second = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    assert second.reused is False
    assert second.snapshotId != first.snapshotId
    assert second.schemaHash != first.schemaHash
    # 旧行必须逐字不变（原生读 JSON 文本 ⇒ 不受 ORM identity map 影响）
    assert await _nativeJson(dbSession, first.snapshotId) == oldJson
    assert '"numericScale": 2' in oldJson
    newJson = await _nativeJson(dbSession, second.snapshotId)
    assert '"numericScale": 4' in newJson
    count = await dbSession.scalar(
        select(func.count())
        .select_from(MetadataSnapshot)
        .where(MetadataSnapshot.source_id == source.id)
    )
    assert count == 2


async def test_停用的发现源拒绝采集(dbSession):
    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession, name="停用源", datasourceId=ds.id, owner="张业务"
    )
    source.status = "DISABLED"
    await dbSession.commit()

    with pytest.raises(ValidationError):
        await service.captureSnapshot(dbSession, sourceId=source.id)


async def test_非只读策略在登记时被拒(dbSession):
    """发现域只读：策略声明可写时**登记即拒**（红线：不写业务库）。

    安全审查 M-1/M-2：策略非法不该「先落库、采集时才炸」——落库即拒绝，
    避免库里留下一个看起来授权、实则不可用的源。
    """

    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])

    with pytest.raises(ValidationError) as excinfo:
        await service.registerSource(
            dbSession,
            name="可写策略源",
            datasourceId=ds.id,
            owner="张业务",
            accessPolicy={"mode": "READ_WRITE"},
        )

    assert "READ_WRITE" in str(excinfo.value)


async def test_快照列表可按源过滤(dbSession):
    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession, name="列表源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()
    await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    rows = await service.listSnapshots(dbSession, sourceId=source.id)

    assert len(rows) == 1
    assert rows[0].source_id == source.id


async def test_发现源列表(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    await service.registerSource(
        dbSession, name="列表源A", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    rows = await service.listSources(dbSession)

    assert [row.name for row in rows] == ["列表源A"]
    assert all(isinstance(row, DiscoverySource) for row in rows)


# ---------------------------------------------------------------- 访问策略校验
#
# 安全审查 M-1 / M-2：策略键**只承认已实现的**。静默忽略未知键会制造
# 「我配了 schemas 白名单、以为生效了其实没生效」的承诺与实现不一致。


async def test_访问策略含未知键被拒(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    with pytest.raises(ValidationError) as excinfo:
        await service.registerSource(
            dbSession,
            name="未知键源",
            datasourceId=ds.id,
            owner="张业务",
            accessPolicy={"mode": "READ_ONLY", "schemas": ["A"]},
        )

    assert "schemas" in str(excinfo.value)


async def test_访问策略缺省补只读模式(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    source = await service.registerSource(
        dbSession, name="缺省策略源", datasourceId=ds.id, owner="张业务"
    )

    assert source.access_policy == {"mode": "READ_ONLY"}


async def test_访问策略schema_owner必须是字符串(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    with pytest.raises(ValidationError) as excinfo:
        await service.registerSource(
            dbSession,
            name="属主类型源",
            datasourceId=ds.id,
            owner="张业务",
            accessPolicy={"mode": "READ_ONLY", "schema_owner": 123},
        )

    assert "schema_owner" in str(excinfo.value)


async def test_访问策略体积超限被拒(dbSession):
    """策略是配置不是数据载荷：超大即拒（防止把策略当数据通道）。"""

    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    with pytest.raises(ValidationError):
        await service.registerSource(
            dbSession,
            name="超大策略源",
            datasourceId=ds.id,
            owner="张业务",
            accessPolicy={"mode": "READ_ONLY", "schema_owner": "x" * 5000},
        )


async def test_采集用策略中的schema_owner作为内省属主(dbSession):
    """`schema_owner`（库模式属主）与 `discovery_source.owner`（责任人）是两件事。

    ⚠️ `schema_owner` **只对 Oracle 有意义**（PG/MySQL 恒查连接默认 schema）⇒
    非 Oracle 数据源带 `schema_owner` 属于「配了但没生效」，登记即拒。
    """

    ds = await _seedDatasource(dbSession, type="oracle")
    fake = _FakeIntrospection([_table("订单", columns=[_column("id", "BIGINT")])])
    service = DiscoverySnapshotService(introspectionService=fake)
    source = await service.registerSource(
        dbSession,
        name="属主源",
        datasourceId=ds.id,
        owner="张业务",
        accessPolicy={"mode": "READ_ONLY", "schema_owner": "THBI"},
    )
    await dbSession.commit()

    await service.captureSnapshot(dbSession, sourceId=source.id)

    assert fake.calls[-1][1] == "THBI"


# ------------------------------------------------- schema_owner 的适用范围
#
# 安全审查 M-3：`schema_owner` 只对 **Oracle** 有意义（Oracle 是 owner 命名空间；
# PG/MySQL 恒查连接默认 schema，内省层会**静默忽略** owner）。
# ⇒ 非 Oracle 数据源配 `schema_owner` = 「我配了命名空间、以为生效了其实没生效」
#   ——正是本域反复强调要拒绝的「承诺与实现不一致」。


async def test_非oracle数据源带schema_owner被拒(dbSession):
    ds = await _seedDatasource(dbSession, type="mysql")
    service = DiscoverySnapshotService()

    with pytest.raises(ValidationError) as excinfo:
        await service.registerSource(
            dbSession,
            name="mysql带属主",
            datasourceId=ds.id,
            owner="张业务",
            accessPolicy={"mode": "READ_ONLY", "schema_owner": "THBI"},
        )

    assert "Oracle" in str(excinfo.value)


async def test_数据源类型改为非oracle后采集拒绝(dbSession):
    """登记时是 Oracle、后来数据源被改成 MySQL ⇒ 采集前复检（承诺不能失效）。"""

    ds = await _seedDatasource(dbSession, type="oracle")
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession,
        name="改类型源",
        datasourceId=ds.id,
        owner="张业务",
        accessPolicy={"mode": "READ_ONLY", "schema_owner": "THBI"},
    )
    await dbSession.commit()
    ds.type = "mysql"
    await dbSession.flush()

    with pytest.raises(ValidationError) as excinfo:
        await service.captureSnapshot(dbSession, sourceId=source.id)

    assert "Oracle" in str(excinfo.value)


# ---------------------------------------------------------------- 数据源状态


async def test_停用的数据源拒绝采集(dbSession):
    """M-6：停用的数据源不再被连接（授权根已撤销 ⇒ 发现动作应停）。"""

    ds = await _seedDatasource(dbSession)
    ds.is_active = False
    await dbSession.flush()
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession, name="停用数据源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ValidationError) as excinfo:
        await service.captureSnapshot(dbSession, sourceId=source.id)

    assert "数据源" in str(excinfo.value)


# ---------------------------------------------------------------- 发现源更新
#
# H-1：`status` 有 DISABLED 取值却没有停用路径 ⇒ 「授权可撤销」是空承诺。


async def test_更新发现源状态可停用且停用后拒绝采集(dbSession):
    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession, name="可停用源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    updated = await service.updateSource(
        dbSession, sourceId=source.id, changes={"status": "DISABLED"}
    )
    await dbSession.commit()

    assert updated.status == "DISABLED"
    with pytest.raises(ValidationError):
        await service.captureSnapshot(dbSession, sourceId=source.id)


async def test_更新发现源可改责任人与备注(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    source = await service.registerSource(
        dbSession, name="改责任人源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    updated = await service.updateSource(
        dbSession, sourceId=source.id, changes={"owner": "李业务", "note": "转岗"}
    )
    await dbSession.commit()

    assert updated.owner == "李业务"
    assert updated.note == "转岗"


async def test_更新发现源状态非法被拒(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    source = await service.registerSource(
        dbSession, name="非法状态源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ValidationError):
        await service.updateSource(
            dbSession, sourceId=source.id, changes={"status": "PAUSED"}
        )


async def test_更新发现源不允许改数据源(dbSession):
    """`datasource_id` 是授权根：改它等于换一条被授权的连接 ⇒ 拒绝。"""

    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    source = await service.registerSource(
        dbSession, name="不可改根源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ValidationError) as excinfo:
        await service.updateSource(
            dbSession, sourceId=source.id, changes={"datasource_id": ds.id}
        )

    assert "datasource_id" in str(excinfo.value)


async def test_更新不存在的发现源报不存在(dbSession):
    from app.domain.exceptions import NotFoundError

    service = DiscoverySnapshotService()

    with pytest.raises(NotFoundError):
        await service.updateSource(
            dbSession, sourceId=999999, changes={"status": "DISABLED"}
        )


async def test_更新发现源的策略同样受校验(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    source = await service.registerSource(
        dbSession, name="改策略源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ValidationError):
        await service.updateSource(
            dbSession,
            sourceId=source.id,
            changes={"access_policy": {"mode": "READ_ONLY", "x": 1}},
        )


# ---------------------------------------------------------------- 注释清洗


def test_规范化清洗注释中的控制字符():
    """M-5：业务库注释是**不可信外部文本**，控制字符与 Unicode 行分隔符必须剔除。"""

    tables = [
        _table(
            "T",
            columns=[_column("c", comment="坏\x00注释\u2028尾")],
            comment="表注释\u2029带分隔符",
        )
    ]

    snapshot = normalizeSnapshot(tables)
    table = snapshot["tables"][0]

    # ⚠️ 口径变更（收敛到 app/domain/external_text.py 单一事实源）：行分隔符
    # 从「删除」改为「折叠成空格」——删除会把两段文字粘成一个词（语义被改写），
    # 与 nl2sql_schema 的既有口径一致。控制字符仍是直接剔除。
    assert table["comment"] == "表注释 带分隔符"
    assert table["columns"][0]["comment"] == "坏注释 尾"


def test_规范化清洗后为空的注释归一为None():
    snapshot = normalizeSnapshot([_table("T", columns=[_column("c", comment="\u2028")])])

    assert snapshot["tables"][0]["columns"][0]["comment"] is None


# ---------------------------------------------------------------- 连接地址覆盖


async def test_连接地址覆盖作用到采集路径且不改数据源行(dbSession):
    """M-1：`--connect-host` 必须作用到**采集路径**。

    代码审查 M-1：覆盖此前只作用于脚本自己的「独立重算」，采集面仍走库里配置
    的地址 ⇒ 「内省用哪个库」在两面各行其是 ⇒ 两个哈希可能来自两个不同的库。

    同时：覆盖**只是连接地址**，不得改库里的数据源配置（否则就是一次静默的
    配置变更，产出与生产同源的意外副作用）。
    """

    ds = await _seedDatasource(dbSession)
    ds.host = "host.docker.internal"
    ds.port = 3306
    await dbSession.flush()
    fake = _FakeIntrospection([_table("T")])
    service = DiscoverySnapshotService(introspectionService=fake)
    source = await service.registerSource(
        dbSession, name="覆盖源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    await service.captureSnapshot(
        dbSession,
        sourceId=source.id,
        connectOverride=ConnectOverride(host="127.0.0.1", port=3307),
    )
    await dbSession.commit()

    assert fake.seenEndpoints[-1] == ("127.0.0.1", 3307)
    row = await dbSession.get(DataSource, ds.id)
    assert row is not None
    assert row.host == "host.docker.internal"
    assert row.port == 3306


async def test_未给覆盖时内省沿用库里配置的地址(dbSession):
    ds = await _seedDatasource(dbSession)
    ds.host = "host.docker.internal"
    await dbSession.flush()
    fake = _FakeIntrospection([_table("T")])
    service = DiscoverySnapshotService(introspectionService=fake)
    source = await service.registerSource(
        dbSession, name="不覆盖源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    assert fake.seenEndpoints[-1] == ("host.docker.internal", 3306)


def test_连接地址覆盖只改地址不改其余字段():
    """覆盖产出的必须是**新对象**（不可变原则），其余连接参数原样保留。"""

    ds = DataSource(
        id=7,
        name="演示源",
        type="mysql",
        host="host.docker.internal",
        port=3306,
        database_name="asset_demo",
        username="demo",
        password_encrypted="cipher",
        is_active=True,
        is_default=False,
    )

    overridden = withConnectOverride(ds, ConnectOverride(host="127.0.0.1"))

    assert overridden is not ds
    assert ds.host == "host.docker.internal"
    assert overridden.host == "127.0.0.1"
    assert overridden.port == 3306
    assert overridden.database_name == "asset_demo"
    assert overridden.username == "demo"
    assert overridden.password_encrypted == "cipher"


# ---------------------------------------------------------------- 库级不可变


async def test_快照行在库层不可更新(dbSession):
    """M-3：不可变由 DB 触发器兜底（应用层纪律之外再加一层）。"""

    from sqlalchemy import update
    from sqlalchemy.exc import DBAPIError

    ds = await _seedDatasource(dbSession)
    service = _service([_table("订单", columns=[_column("id", "BIGINT")])])
    source = await service.registerSource(
        dbSession, name="不可变源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()
    result = await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    with pytest.raises(DBAPIError):
        await dbSession.execute(
            update(MetadataSnapshot)
            .where(MetadataSnapshot.id == result.snapshotId)
            .values(table_count=999)
        )
    await dbSession.rollback()

    row = await dbSession.get(MetadataSnapshot, result.snapshotId)
    assert row is not None
    assert row.table_count == 1


# ---------------------------------------------------------------- 登记入参校验
#
# 代码审查 MEDIUM-3 / LOW-2：登记路径**没有**走 `updateSource` 已有的校验
# （`_normalizeStatus` / `_normalizeOwner`）⇒ 非法状态、空责任人可以落库，
# 变成「库里有、但采集时才发现不对」的死行。


async def test_登记时非法状态被拒(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    with pytest.raises(ValidationError) as excinfo:
        await service.registerSource(
            dbSession,
            name="非法状态源",
            datasourceId=ds.id,
            owner="张业务",
            status="PAUSED",
        )

    assert "PAUSED" in str(excinfo.value)


async def test_登记时空白责任人被拒(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    with pytest.raises(ValidationError):
        await service.registerSource(
            dbSession, name="空责任人源", datasourceId=ds.id, owner="   "
        )


async def test_登记时状态大小写归一(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()

    source = await service.registerSource(
        dbSession,
        name="小写状态源",
        datasourceId=ds.id,
        owner="张业务",
        status="disabled",
    )

    assert source.status == "DISABLED"


# ---------------------------------------------------------------- 策略为 null
#
# 安全审查 LOW-3：`updateSource` 传 `access_policy=None` 时静默重置为默认只读策略
# —— 调用方以为「没改策略」，实际把授权策略重置了。


async def test_更新发现源策略为null被拒(dbSession):
    ds = await _seedDatasource(dbSession)
    service = DiscoverySnapshotService()
    source = await service.registerSource(
        dbSession, name="策略null源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ValidationError):
        await service.updateSource(
            dbSession, sourceId=source.id, changes={"access_policy": None}
        )


# ---------------------------------------------------------------- 外部文本净化
#
# 安全审查 M-1 / M-2：净化只覆盖了**注释**，而表名 / 列名 / 属主 / 外键表名列名
# 同样是业务库（低权限账号可写）来的外部文本，同样会进快照、进 A3 的 LLM 上下文。


def test_规范化清洗表名与列名中的不可见字符():
    snapshot = normalizeSnapshot(
        [_table("订单" + chr(0), columns=[_column("id", "BIGINT")])]
    )

    table = snapshot["tables"][0]
    assert table["tableName"] == "订单"
    assert table["columns"][0]["columnName"] == "id"


def test_规范化清洗属主与外键标识符():
    snapshot = normalizeSnapshot(
        [
            _table(
                "订单",
                owner="THBI" + chr(0),
                columns=[_column("客户号" + chr(0))],
                fks=[
                    ForeignKeySchemaRead(
                        column_name="客户号",
                        ref_table="客户" + chr(0),
                        ref_column="id",
                    )
                ],
            )
        ]
    )

    table = snapshot["tables"][0]
    assert table["owner"] == "THBI"
    assert table["columns"][0]["columnName"] == "客户号"
    assert table["foreignKeys"][0]["refTable"] == "客户"
    assert table["foreignKeys"][0]["refColumn"] == "id"


def test_规范化转义注释里的尖括号():
    """尖括号不转义 ⇒ 业务库注释能拼出闭合标签逃逸出 LLM 上下文包装。"""

    snapshot = normalizeSnapshot(
        [_table("T", columns=[_column("c", comment="</instructions>")])]
    )

    assert snapshot["tables"][0]["columns"][0]["comment"] == "&lt;/instructions&gt;"


def test_规范化折叠注释里的换行():
    snapshot = normalizeSnapshot(
        [_table("T", columns=[_column("c", comment="第一行\n第二行")])]
    )

    assert snapshot["tables"][0]["columns"][0]["comment"] == "第一行 第二行"


def test_规范化对注释里的高置信pii掩码():
    """D-3 红线：PII 不进元数据库（邮箱 / 手机号 / 身份证号）。"""

    snapshot = normalizeSnapshot(
        [
            _table(
                "T",
                columns=[
                    _column("c", comment="联系 zhangsan@example.com 或 13800138000"),
                    _column("d", comment="证件 11010119900307561X"),
                ],
            )
        ]
    )

    columns = snapshot["tables"][0]["columns"]
    assert "zhangsan@example.com" not in columns[0]["comment"]
    assert "13800138000" not in columns[0]["comment"]
    assert "11010119900307561X" not in columns[1]["comment"]


def test_规范化不误伤正常业务注释():
    snapshot = normalizeSnapshot(
        [_table("T", columns=[_column("c", comment="订单状态：0待审 1已审")])]
    )

    assert snapshot["tables"][0]["columns"][0]["comment"] == "订单状态：0待审 1已审"


# ---------------------------------------------------------------- 连接地址覆盖 × 适配器池
#
# 代码审查 MEDIUM-7：`business_db_pool.get_adapter(datasourceId, ds)` **按 id 缓存**，
# 命中即返回旧适配器、**忽略传入的 ds** ⇒ 连接地址覆盖会被池化适配器静默吃掉
# （覆盖只在内省层「没被缓存」时才生效）。
# 修法：覆盖生效期间释放该数据源的池化适配器，采集后再释放一次（覆盖地址不得留在池里）。


async def test_连接地址覆盖时释放池化适配器(dbSession, monkeypatch):
    from app.infrastructure import business_db_pool

    disposed: list[int] = []

    async def _fakeDispose(datasourceId: int) -> None:
        disposed.append(datasourceId)

    monkeypatch.setattr(business_db_pool, "dispose_adapter", _fakeDispose)

    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession, name="池化覆盖源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    await service.captureSnapshot(
        dbSession,
        sourceId=source.id,
        connectOverride=ConnectOverride(host="127.0.0.1", port=3307),
    )
    await dbSession.commit()

    assert ds.id in disposed


async def test_无覆盖时不释放池化适配器(dbSession, monkeypatch):
    """没有覆盖就不该动共享连接池（避免把别的调用方的连接打断）。"""

    from app.infrastructure import business_db_pool

    disposed: list[int] = []

    async def _fakeDispose(datasourceId: int) -> None:
        disposed.append(datasourceId)

    monkeypatch.setattr(business_db_pool, "dispose_adapter", _fakeDispose)

    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])
    source = await service.registerSource(
        dbSession, name="池化普通源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    await service.captureSnapshot(dbSession, sourceId=source.id)
    await dbSession.commit()

    assert disposed == []



# ---------------------------------------------------------------- 体积上限


async def test_快照体积超上限时拒绝采集(dbSession):
    """超大快照必须**拒绝落库**（单行 JSONB 撑爆库 / 撑爆详情响应）。

    上限 `MAX_SNAPSHOT_BYTES`（4 MiB）：超了要**明确报错**，而不是截断内容
    （截断会让快照与真实 schema 不一致 ⇒ 内容寻址失真）。
    """

    from app.services.discovery_snapshot_service import MAX_SNAPSHOT_BYTES

    bigComment = "x" * (MAX_SNAPSHOT_BYTES + 1024)
    ds = await _seedDatasource(dbSession)
    service = _service([_table("大表", columns=[_column("id", "BIGINT")], comment=bigComment)])
    source = await service.registerSource(
        dbSession, name="超大快照源", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    with pytest.raises(ValidationError) as excinfo:
        await service.captureSnapshot(dbSession, sourceId=source.id)

    assert "过大" in str(excinfo.value)


# ---------------------------------------------------------------- 按源计数


async def test_按源统计快照只覆盖传入的源(dbSession):
    """MEDIUM-10：计数必须**限定在调用方给的源 id** 上。

    列表端点只需要**当前页**那几行的计数；整表 GROUP BY 会把全库快照都聚合一遍
    （大库上是白烧的 IO）⇒ 传入 ids 时结果里**不得**出现别的源。
    """

    ds = await _seedDatasource(dbSession)
    service = _service([_table("T")])
    sourceA = await service.registerSource(
        dbSession, name="计数源甲", datasourceId=ds.id, owner="张业务"
    )
    sourceB = await service.registerSource(
        dbSession, name="计数源乙", datasourceId=ds.id, owner="张业务"
    )
    await dbSession.commit()

    await service.captureSnapshot(dbSession, sourceId=sourceA.id)
    await service.captureSnapshot(dbSession, sourceId=sourceB.id)
    await dbSession.commit()

    counts = await service.countSnapshotsBySource(
        dbSession, sourceIds=[sourceA.id]
    )

    assert counts == {sourceA.id: 1}
    assert sourceB.id not in counts


async def test_按源统计快照传空集合直接返回空(dbSession):
    """空 id 列表 ⇒ 直接返回空字典（不必再查库）。"""

    service = _service([_table("T")])

    counts = await service.countSnapshotsBySource(dbSession, sourceIds=[])

    assert counts == {}
