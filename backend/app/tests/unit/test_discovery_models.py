"""本体发现域数据模型（WP-ONT-AI A1-1）单测。

本文件**双向钉住** ORM 与 Alembic 迁移：

1. 表名 / 列名 / 类型 / 可空性；
2. 约束（唯一约束、CHECK、FK 的 ondelete、索引）；
3. **快照行只增不改**（`metadata_snapshot` 没有 `updated_time`）；
4. 迁移的 revision 链、DDL 覆盖范围、列注释与 CHECK 表达式**逐字一致**。

为什么用「读迁移源码 + 比字符串」而不是 `alembic check`：
`alembic check` 只比**名**不比**式**（见 `Harness/changes` 里 CHECK 约束补进 ORM 的
复盘：只补名 = 假绿）。本文件补上「表达式逐字比对」这一层，
再由 `alembic check` 在真库上兜底结构漂移。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.domain.discovery_models import DiscoverySource, MetadataSnapshot

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "versions"
_MIGRATION_STEM = "0146_discovery_snapshot"
_IMMUTABILITY_STEM = "0147_snapshot_immutability"


def _loadMigration(stem: str = _MIGRATION_STEM):
    path = _MIGRATIONS_DIR / f"{stem}.py"
    assert path.exists(), f"迁移文件不存在：{path}"
    spec = importlib.util.spec_from_file_location(f"_mig_{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recordedOps(module, funcName: str) -> list[tuple]:
    """跑一遍迁移函数，记录它对 `alembic.op` 发的 DDL 调用。"""

    calls: list[tuple] = []

    class _FakeOp:
        def create_table(self, tableName, *columns, **kwargs):
            calls.append(("create_table", tableName, tuple(columns)))

        def create_index(self, name, tableName, *columns, **kwargs):
            calls.append(("create_index", name, tableName))

        def __getattr__(self, name):
            def _unexpected(*args, **kwargs):
                calls.append((name, *args))

            return _unexpected

    original = module.op
    module.op = _FakeOp()
    try:
        getattr(module, funcName)()
    finally:
        module.op = original
    return calls


def _checkExpressions(table) -> dict[str, str]:
    result: dict[str, str] = {}
    for constraint in table.constraints:
        if isinstance(constraint, sa.CheckConstraint):
            result[constraint.name] = str(constraint.sqltext)
    return result


def _uniqueConstraints(table) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            result[constraint.name] = tuple(col.name for col in constraint.columns)
    return result


def _foreignKeys(table) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for constraint in table.constraints:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            targets = [f"{fk.target_fullname}" for fk in constraint.elements]
            result[constraint.name] = (
                tuple(sorted(targets))[0],
                constraint.ondelete or "",
            )
    return result


# ---------------------------------------------------------------- 表与列


def test_表名与既有命名约定一致():
    assert DiscoverySource.__tablename__ == "discovery_source"
    assert MetadataSnapshot.__tablename__ == "metadata_snapshot"


def test_发现源列集合():
    assert set(DiscoverySource.__table__.columns.keys()) == {
        "id",
        "name",
        "datasource_id",
        "owner",
        "access_policy",
        "status",
        "note",
        "created_by",
        "created_time",
    }


def test_快照列集合():
    assert set(MetadataSnapshot.__table__.columns.keys()) == {
        "id",
        "source_id",
        "schema_hash",
        "snapshot_json",
        "table_count",
        "column_count",
        "captured_at",
        "created_by",
        "created_time",
    }


def test_快照行只增不改没有更新时间列():
    """不可变快照：没有 `updated_time`，也没有 UPDATE 路径（服务层 A1-2 负责）。"""

    assert "updated_time" not in MetadataSnapshot.__table__.columns
    assert "updated_time" not in DiscoverySource.__table__.columns


def test_哈希列长度与快照列为jsonb():
    assert MetadataSnapshot.__table__.columns["schema_hash"].type.length == 64
    assert MetadataSnapshot.__table__.columns["snapshot_json"].type.__class__.__name__ == "JSONB"
    assert DiscoverySource.__table__.columns["access_policy"].type.__class__.__name__ == "JSONB"


# ---------------------------------------------------------------- 约束


def test_发现源名称唯一():
    uniques = _uniqueConstraints(DiscoverySource.__table__)
    assert uniques["uq_discovery_source_name"] == ("name",)


def test_快照在源内按哈希唯一():
    uniques = _uniqueConstraints(MetadataSnapshot.__table__)
    assert uniques["uq_metadata_snapshot_source_hash"] == ("source_id", "schema_hash")


def test_外键指向既有数据源与发现源且删除受限():
    """D-9：`discovery_source` 只引用既有 `data_source`，不新建连接、不新建真相源。"""

    fks = _foreignKeys(DiscoverySource.__table__)
    assert fks["fk_discovery_source_datasource"] == ("data_source.id", "RESTRICT")

    snapshotFks = _foreignKeys(MetadataSnapshot.__table__)
    assert snapshotFks["fk_metadata_snapshot_source"] == ("discovery_source.id", "RESTRICT")


def test_发现源状态被check钉死():
    checks = _checkExpressions(DiscoverySource.__table__)
    assert checks["ck_discovery_source_status"] == "status IN ('ACTIVE','DISABLED')"


def test_快照哈希格式被check钉死():
    checks = _checkExpressions(MetadataSnapshot.__table__)
    assert checks["ck_metadata_snapshot_hash_sha256"] == "schema_hash ~ '^[0-9a-f]{64}$'"


def test_快照内容必须是json对象():
    checks = _checkExpressions(MetadataSnapshot.__table__)
    assert checks["ck_metadata_snapshot_is_object"] == "jsonb_typeof(snapshot_json) = 'object'"


def test_快照计数不得为负():
    checks = _checkExpressions(MetadataSnapshot.__table__)
    assert checks["ck_metadata_snapshot_counts_nonneg"] == "table_count >= 0 AND column_count >= 0"


def test_索引齐备():
    indexNames = {index.name for index in DiscoverySource.__table__.indexes}
    assert "ix_discovery_source_datasource" in indexNames
    snapshotIndexNames = {index.name for index in MetadataSnapshot.__table__.indexes}
    assert "ix_metadata_snapshot_source" in snapshotIndexNames


# ---------------------------------------------------------------- 迁移一致性


def test_迁移文件存在且revision链正确():
    module = _loadMigration()
    assert module.revision == "0146"
    assert module.down_revision == "0145"


def test_迁移只建两张新表不触碰既有表():
    module = _loadMigration()
    calls = _recordedOps(module, "upgrade")
    created = [call for call in calls if call[0] == "create_table"]
    assert [call[1] for call in created] == ["discovery_source", "metadata_snapshot"]
    # 不得出现任何 alter / drop / 对既有表的写操作
    forbidden = {"drop_table", "alter_column", "add_column", "drop_column", "create_index_unique"}
    assert not [call for call in calls if call[0] in forbidden]


def test_迁移列注释与orm逐字一致():
    """ORM `comment` 与迁移 `comment` 必须**双向**逐字一致。

    「ORM 的注释出现在迁移源码文本里」这种单向断言有两个盲区（代码审查 M-10）：
    ① 注释挂到**错误的列**上照样通过；② 迁移里有、ORM 里没有的注释
    （这正是 `alembic check` 报漂移的方向）测不出来。
    ⇒ 这里按 (表名, 列名) 建映射做**双向字典比对**。
    """

    module = _loadMigration()
    ormComments = {
        (table.name, column.name): column.comment
        for table in (DiscoverySource.__table__, MetadataSnapshot.__table__)
        for column in table.columns
        if column.comment
    }
    migrationComments = _migrationColumnComments(module)

    assert ormComments == migrationComments, (
        "ORM 与迁移的列注释不一致："
        f"仅 ORM 有={sorted(set(ormComments) - set(migrationComments))}，"
        f"仅迁移有={sorted(set(migrationComments) - set(ormComments))}，"
        "内容不同="
        f"{sorted(k for k in set(ormComments) & set(migrationComments) if ormComments[k] != migrationComments[k])}"
    )


def _migrationColumnComments(module) -> dict[tuple[str, str], str]:
    """从迁移的 DDL 调用里抽出 (表名, 列名) → 列注释。"""

    comments: dict[tuple[str, str], str] = {}
    for call in _recordedOps(module, "upgrade"):
        if call[0] != "create_table":
            continue
        tableName = call[1]
        for column in call[2]:
            if isinstance(column, sa.Column) and column.comment:
                comments[(tableName, column.name)] = column.comment
    return comments


# ---------------------------------------------------------------- 不可变触发器


def test_不可变触发器迁移链在0146之后():
    module = _loadMigration(_IMMUTABILITY_STEM)

    assert module.revision == "0147"
    assert module.down_revision == "0146"


def test_迁移文件名不超过32字符():
    """仓库约定：迁移文件名 ≤ 32 字符（`alembic_version.version_num` 是 varchar(32)，
    且 `Harness/rules/开发流程规范.md` 有同名软约定）。"""

    for stem in (_MIGRATION_STEM, _IMMUTABILITY_STEM):
        assert len(f"{stem}.py") <= 32, f"{stem}.py 超过 32 字符"


def test_不可变触发器同时拦截更新与删除():
    """M-3：不可变由**库层**兜底 ⇒ 触发器必须同时挂 UPDATE 与 DELETE。"""

    source = (
        _MIGRATIONS_DIR / f"{_IMMUTABILITY_STEM}.py"
    ).read_text(encoding="utf-8")

    assert "BEFORE UPDATE OR DELETE ON metadata_snapshot" in source
    assert "prevent_snapshot_modification" in source


def test_迁移check表达式与orm逐字一致():
    """`alembic check` 只比约束名不比式 ⇒ 这一层由单测兜住（只补名 = 假绿）。"""

    module = _loadMigration()
    migrationChecks: dict[str, str] = {}
    for tableName in ("discovery_source", "metadata_snapshot"):
        calls = _recordedOps(module, "upgrade")
        for call in calls:
            if call[0] != "create_table" or call[1] != tableName:
                continue
            for item in call[2]:
                if isinstance(item, sa.CheckConstraint):
                    migrationChecks[item.name] = str(item.sqltext)
    for table in (DiscoverySource.__table__, MetadataSnapshot.__table__):
        for name, expression in _checkExpressions(table).items():
            assert name in migrationChecks, f"迁移缺少 CHECK 约束：{name}"
            assert migrationChecks[name] == expression, f"CHECK 表达式不一致：{name}"
