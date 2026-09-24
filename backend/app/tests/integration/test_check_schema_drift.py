"""Schema drift 检测脚本单测 + 集成测试。

覆盖：
- 解析不同风格的 Alembic 迁移文件（modern str-typed / old direct assignment）
- 检测 ORM 声明但 DB 缺失的表
- 检测 DB 存在但 ORM 未声明的表（多余表）
- 检测 alembic_version 滞后于 head
- 检测 alembic_version 不在迁移文件集合（脏状态）
- 检测 alembic_version 表不存在（全新空库）
- 列级：ORM 声明而 DB 缺失（blocking）/ DB 多出（warning）
- 索引级：ORM 声明而 DB 缺失（blocking）/ DB 多出（warning）/ 同名不同列（blocking）
- happy path：DB 与 head 一致时返回 []

被测模块是 `app/infrastructure/schema_drift.py`（2026-09-12 从 `scripts/` 搬来，
见其 docstring）。集成用例通过 `python -m` 起独立进程跑（进程边界，与 alembic env
解耦），纯逻辑用例直接 import 该模块。

**列/索引粒度是 2026-09-12 补的**：那次两库漂移（prod 缺
`ix_menu_config_visible_sort`、prod `users` 多 4 列）**恰好全在列/索引级**，
而本脚本当时只比表集合，全程没报警。见
`Harness/changes/fix-schema-drift-two-dbs/`。

粒度扩了之后最大的风险从「漏报」变成了「误报」——PG 给每个 PK 和
UniqueConstraint 都建支撑索引，而 ORM 侧的 `UniqueConstraint` 是无名的
（`name=None`），DB 侧却有名字（`uq_xxx`）。只按名字比会在真实库上造出上百条
假阳性。所以 `test_healthy_db_has_no_column_or_index_drift` 是这套用例里
最重要的一条：它是归一化逻辑的假阳性守卫。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    func,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_MODULE_NAME = "app.infrastructure.schema_drift"

# 2026-09-12：模块从 scripts/ 搬进 app/（与 main.py 同处 app 包，让
# `docker cp backend/app/.` 一次带全那对必须同版本的两半）。搬进来后它就是一个
# 正常的 app 子模块，直接 import 即可 —— 原先那套 importlib 按文件路径加载
# 是外部脚本时代的产物，顺带消掉了它带来的 sys.modules 注册 hack。
from app.infrastructure import schema_drift as _drift_module  # noqa: E402

_parseRevisionFiles = _drift_module._parseRevisionFiles
_findHead = _drift_module._findHead


def _runDriftScript(dbUrl: str, *args: str) -> subprocess.CompletedProcess:
    """在独立进程里跑 drift 模块（进程边界，与 alembic env 解耦）。

    用 `python -m` 而不是文件路径：`-m` 会把 cwd 放进 sys.path，模块内
    `from app.domain.models import Base` 才解析得到。
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = dbUrl
    return subprocess.run(
        [sys.executable, "-m", _MODULE_NAME, *args],
        cwd=str(_BACKEND_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _dbUrl() -> str:
    """真实 PG 测试库 URL（未配置时由 _pg_support fail-fast）。"""
    from app.tests._pg_support import resolveTestDatabaseUrl

    return resolveTestDatabaseUrl()


async def _execSql(dbUrl: str, *statements: str) -> None:
    """在真实 PG 上顺序执行 DDL（测试自己负责还原）。"""
    import asyncpg

    conn = await asyncpg.connect(dbUrl.replace("+asyncpg", ""))
    try:
        for statement in statements:
            await conn.execute(statement)
    finally:
        await conn.close()


async def _fetchScalar(dbUrl: str, sql: str, *params: object) -> object:
    import asyncpg

    conn = await asyncpg.connect(dbUrl.replace("+asyncpg", ""))
    try:
        return await conn.fetchval(sql, *params)
    finally:
        await conn.close()


def _syntheticTable() -> Table:
    """合成表：覆盖三种需要归一化的索引形态。

    - `synth_pkey`：PG 给 PK 自动建的支撑索引（ORM 侧 primary_key 表意）
    - `uq_synth_code`：ORM 侧**无名** UniqueConstraint，DB 侧却有名字
    - `ix_synth_expr`：表达式索引，列名不可直接与 DB 的 `<expr>` 比对
    """
    metadata = MetaData()
    table = Table(
        "synth",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("code", String(32)),
        Column("name", String(32)),
        UniqueConstraint("code"),
    )
    Index("ix_synth_name", table.c.name)
    Index("ix_synth_expr", func.lower(table.c.name))
    return table


def _dbIndex(  # noqa: PLR0913 - 测试构造器，参数即字段
    name: str,
    columns: tuple[str, ...],
    *,
    primary: bool = False,
    unique: bool = False,
    constraints: tuple[str, ...] = (),
):
    """构造 DB 侧索引形状（与脚本 _queryDbIndexes 的返回同形）。"""
    return _drift_module.DbIndexShape(
        name=name,
        columns=columns,
        isPrimary=primary,
        isUnique=unique,
        constraintNames=constraints,
    )


def _compare(
    *,
    dbColumns: set[str],
    dbIndexes: list,
    table: Table | None = None,
):
    """用合成表的 ORM 形状跑一次比对。"""
    shape = _drift_module._ormTableShape(table or _syntheticTable())
    return _drift_module._compareTable("synth", shape, dbColumns, dbIndexes)


_ALL_SYNTH_COLUMNS = {"id", "code", "name"}


def _synthIndexes() -> list:
    """合成表在 DB 侧的索引全集（与 `_syntheticTable()` 一一对应，无漂移）。"""
    return [
        _dbIndex("synth_pkey", ("id",), primary=True, unique=True),
        _dbIndex("uq_synth_code", ("code",), unique=True, constraints=("uq_synth_code",)),
        _dbIndex("ix_synth_name", ("name",)),
        _dbIndex("ix_synth_expr", ("<expr>",)),
    ]


class TestOrmTableShape:
    """ORM 元数据 → 比对用的形状（纯逻辑，合成表）。"""

    def test_columns_and_index_names_are_extracted(self) -> None:
        shape = _drift_module._ormTableShape(_syntheticTable())

        assert shape.columns == frozenset(_ALL_SYNTH_COLUMNS)
        assert set(shape.indexColumns) == {"ix_synth_name", "ix_synth_expr"}

    def test_simple_index_columns_are_captured_in_order(self) -> None:
        shape = _drift_module._ormTableShape(_syntheticTable())

        assert shape.indexColumns["ix_synth_name"] == ("name",)

    def test_expression_index_columns_are_none(self) -> None:
        """表达式索引无法按列名比对 —— 必须标 None 而不是猜，否则必误报。"""
        shape = _drift_module._ormTableShape(_syntheticTable())

        assert shape.indexColumns["ix_synth_expr"] is None

    def test_unnamed_unique_constraint_is_captured_by_column_set(self) -> None:
        """ORM 的 UniqueConstraint 无名，DB 侧有名字 —— 只能靠列集合认亲。"""
        shape = _drift_module._ormTableShape(_syntheticTable())

        assert ("code",) in shape.uniqueColumnSets
        assert ("id",) in shape.uniqueColumnSets  # PK 同理


class TestCompareTableColumnsAndIndexes:
    """列/索引分类逻辑（纯逻辑）。

    这里覆盖的是「粒度扩了之后最大的风险」：把 PG 的支撑索引、表达式索引
    误判成漂移。每条假阳性守卫都对应真实库上的一个形态。
    """

    def test_clean_table_yields_no_issues(self) -> None:
        issues = _compare(dbColumns=_ALL_SYNTH_COLUMNS, dbIndexes=_synthIndexes())

        assert issues == []

    def test_pk_backing_index_is_not_reported(self) -> None:
        issues = _compare(dbColumns=_ALL_SYNTH_COLUMNS, dbIndexes=_synthIndexes())
        codes = [i.code for i in issues]

        assert "extra_index" not in codes

    def test_unnamed_unique_constraint_matches_by_column_set(self) -> None:
        """ORM `UniqueConstraint("code")` 无名 vs DB `uq_synth_code`：不得误报。"""
        issues = _compare(dbColumns=_ALL_SYNTH_COLUMNS, dbIndexes=_synthIndexes())

        assert [i for i in issues if i.code == "extra_index"] == []

    def test_unique_index_without_constraint_is_still_extra(self) -> None:
        """反面：DB 上唯一但非约束支撑的索引，ORM 确实没声明 → 该报。

        否则「按列集合认亲」会过宽，把真的多余索引用 (name,) 这种集合认成 UniqueConstraint。
        """
        issues = _compare(
            dbColumns=_ALL_SYNTH_COLUMNS,
            dbIndexes=[*_synthIndexes(), _dbIndex("uq_synth_name_only", ("name",), unique=True)],
        )

        extra = [i for i in issues if i.code == "extra_index"]
        assert len(extra) == 1
        assert "uq_synth_name_only" in extra[0].message
        assert extra[0].severity == "warning"

    def test_plain_extra_index_is_warning(self) -> None:
        issues = _compare(
            dbColumns=_ALL_SYNTH_COLUMNS,
            dbIndexes=[*_synthIndexes(), _dbIndex("idx_synth_legacy", ("code", "name"))],
        )

        extra = [i for i in issues if i.code == "extra_index"]
        assert len(extra) == 1
        assert "idx_synth_legacy" in extra[0].message
        assert extra[0].severity == "warning"

    def test_missing_orm_index_is_blocking(self) -> None:
        """0060 事故形态：ORM/迁移声明了索引，DB 没有。"""
        issues = _compare(
            dbColumns=_ALL_SYNTH_COLUMNS,
            dbIndexes=[i for i in _synthIndexes() if i.name != "ix_synth_name"],
        )

        missing = [i for i in issues if i.code == "missing_index"]
        assert len(missing) == 1
        assert "ix_synth_name" in missing[0].message
        assert missing[0].severity == "blocking"

    def test_index_same_name_different_columns_is_blocking(self) -> None:
        issues = _compare(
            dbColumns=_ALL_SYNTH_COLUMNS,
            dbIndexes=[
                i if i.name != "ix_synth_name" else _dbIndex("ix_synth_name", ("id",))
                for i in _synthIndexes()
            ],
        )

        mismatch = [i for i in issues if i.code == "index_mismatch"]
        assert len(mismatch) == 1
        assert "ix_synth_name" in mismatch[0].message
        assert mismatch[0].severity == "blocking"

    def test_expression_index_is_not_column_compared(self) -> None:
        """两边都是表达式（DB `<expr>`）→ 跳过列比对，绝不误报 mismatch。"""
        issues = _compare(dbColumns=_ALL_SYNTH_COLUMNS, dbIndexes=_synthIndexes())

        assert [i for i in issues if i.code == "index_mismatch"] == []

    def test_missing_orm_column_is_blocking(self) -> None:
        issues = _compare(
            dbColumns={"id", "name"},
            dbIndexes=_synthIndexes(),
        )

        missing = [i for i in issues if i.code == "missing_column"]
        assert len(missing) == 1
        assert "synth.code" in missing[0].message
        assert missing[0].severity == "blocking"

    def test_extra_db_column_is_warning(self) -> None:
        """users 4 列事故形态：DB 有 ORM 不认识的列（手工 DDL 残留）。

        只警告不阻断 —— prod 上那 4 列带着真实 bcrypt 凭据，
        若判 blocking 会让线上直接起不来。
        """
        issues = _compare(
            dbColumns={*_ALL_SYNTH_COLUMNS, "legacy_col"},
            dbIndexes=_synthIndexes(),
        )

        extra = [i for i in issues if i.code == "extra_column"]
        assert len(extra) == 1
        assert "synth.legacy_col" in extra[0].message
        assert extra[0].severity == "warning"


class TestSeveritySplit:
    """blocking / warning 分类（启动阻断策略的单一事实来源）。"""

    def test_missing_column_blocks_but_extra_column_does_not(self) -> None:
        issues = _compare(
            dbColumns={"id", "name", "legacy_col"},
            dbIndexes=_synthIndexes(),
        )
        blocking, warnings = _drift_module._splitBySeverity(issues)

        assert [i.code for i in blocking] == ["missing_column"]
        assert [i.code for i in warnings] == ["extra_column"]

    def test_healthy_issues_produce_no_blocking(self) -> None:
        blocking, warnings = _drift_module._splitBySeverity([])

        assert blocking == [] and warnings == []


class TestParseRevisionFiles:
    """测试 Alembic 迁移文件解析（两种风格）。"""

    def test_modern_style_with_type_annotation(self, tmp_path: Path) -> None:
        (tmp_path / "0001_init.py").write_text(
            'revision: str = "0001_init"\n'
            'down_revision: str | None = None\n'
        )
        (tmp_path / "0002_next.py").write_text(
            'revision: str = "0002_next"\n'
            'down_revision: str | None = "0001_init"\n'
        )
        revs, graph = _parseRevisionFiles(tmp_path)
        assert revs == {"0001_init", "0002_next"}
        assert graph == {"0001_init": None, "0002_next": "0001_init"}

    def test_old_style_without_type_annotation(self, tmp_path: Path) -> None:
        (tmp_path / "0001_init.py").write_text(
            'revision = "0001_init"\n'
            "down_revision = None\n"
        )
        (tmp_path / "0002_next.py").write_text(
            'revision = "0002_next"\n'
            'down_revision = "0001_init"\n'
        )
        revs, graph = _parseRevisionFiles(tmp_path)
        assert revs == {"0001_init", "0002_next"}
        assert graph == {"0001_init": None, "0002_next": "0001_init"}

    def test_mixed_styles(self, tmp_path: Path) -> None:
        (tmp_path / "0001_init.py").write_text(
            'revision: str = "0001_init"\n'
            'down_revision: str | None = None\n'
        )
        (tmp_path / "0002_next.py").write_text(
            'revision = "0002_next"\n'
            'down_revision = "0001_init"\n'
        )
        revs, graph = _parseRevisionFiles(tmp_path)
        assert graph["0001_init"] is None
        assert graph["0002_next"] == "0001_init"


class TestFindHead:
    """测试 head 解析（链唯一性）。"""

    def test_single_chain(self) -> None:
        graph = {"a": None, "b": "a", "c": "b"}
        assert _findHead(graph) == "c"

    def test_multi_head_raises(self) -> None:
        graph = {"a": None, "b": None}
        with pytest.raises(RuntimeError, match="多 head"):
            _findHead(graph)

    def test_no_head_raises(self) -> None:
        graph = {"a": "b", "b": "a"}
        with pytest.raises(RuntimeError, match="多 head"):
            _findHead(graph)


class TestCheckDriftSubprocess:
    """通过 subprocess 调用脚本（真实进程边界，避免 alembic env 副作用）。"""

    @pytest.fixture()
    def db_url(self) -> str:
        return _dbUrl()

    def _run_script(self, db_url: str, *args: str) -> subprocess.CompletedProcess:
        """运行脚本并返回结果。"""
        return _runDriftScript(db_url, *args)

    def test_healthy_db_returns_zero(self, db_url: str) -> None:
        """DB 已 upgrade head 时，脚本 exit 0。"""
        result = self._run_script(db_url, "--quiet")
        assert result.returncode == 0, (
            f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
        )

    def test_missing_table_returns_one(self, db_url: str) -> None:
        """当 ORM 表被临时移除，脚本应 exit 1 + 输出漂移项。"""
        # 用 psql 临时重命名一张表，触发 ORM-vs-DB 漂移
        import asyncpg

        async def rename() -> None:
            conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
            try:
                await conn.execute(
                    "ALTER TABLE entity_mapping RENAME TO entity_mapping_drift_test"
                )
            finally:
                await conn.close()

        async def restore() -> None:
            conn = await asyncpg.connect(db_url.replace("+asyncpg", ""))
            try:
                await conn.execute(
                    "ALTER TABLE entity_mapping_drift_test RENAME TO entity_mapping"
                )
            finally:
                await conn.close()

        asyncio_run(rename())
        try:
            result = self._run_script(db_url)
        finally:
            asyncio_run(restore())

        assert result.returncode == 1, (
            f"应检测到漂移 exit 1，但 exit={result.returncode}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "entity_mapping" in combined
        assert "缺失" in combined


class TestSkipSchemaCheckEnvVar:
    """测试 SKIP_SCHEMA_CHECK 环境变量在 main.py 中的存在性。"""

    def test_main_py_checks_env_var(self) -> None:
        main_py = (_BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")
        assert "SKIP_SCHEMA_CHECK" in main_py, (
            "app/main.py 必须检查 SKIP_SCHEMA_CHECK 环境变量"
        )
        assert 'os.environ.get("SKIP_SCHEMA_CHECK") != "1"' in main_py


class TestColumnAndIndexDriftSubprocess:
    """列/索引漂移的真实 PG 用例（每条都是历史上真踩过的形态）。

    DDL 一律在 `finally` 里还原，且**不删数据**（改列名而非删列）。
    """

    @pytest.fixture()
    def db_url(self) -> str:
        return _dbUrl()

    def test_healthy_db_has_no_column_or_index_drift(self, db_url: str) -> None:
        """健康库上不得报出任何「ORM 声明却缺失」的对象 —— **假阳性守卫**。

        这是本组用例里最重要的一条。PG 给每个 PK / UniqueConstraint 都建了支撑
        索引，而 ORM 侧的 `UniqueConstraint` 是无名的；只要归一化写成按名字比，
        这套真实 schema 上就会立刻误报成「ORM 索引缺失」。误报的代价是启动被
        阻断，所以这里把几个典型约束支撑索引**按名字**钉死。

        注意 `[db:index]` **可以**出现：测试库上确实有 23 个 ORM 未声明的历史
        遗留索引，它们就该被报成非阻断警告。
        """
        result = _runDriftScript(db_url)
        combined = result.stdout + result.stderr

        assert result.returncode == 0, combined

        # ORM 声明的东西一个都不能被判缺失，否则会误阻断启动
        assert "[orm:table]" not in combined
        assert "[orm:column]" not in combined
        assert "[orm:index]" not in combined
        assert "[db:column]" not in combined

        # 约束支撑索引必须被认领（真实库上共 130+ 个，漏认就刷屏）
        for name in (
            "menu_config.menu_config_pkey",
            "menu_config.uq_menu_config_code",
            "users.users_pkey",
            "users.uq_users_username",
            "ontology_class.uq_ontology_class_name_version",
            "data_lineage.uq_data_lineage_edge",
        ):
            assert name not in combined, f"约束支撑索引 {name} 被误报为多余"

    def test_dropped_orm_index_is_blocking_drift(self, db_url: str) -> None:
        """0060 事故回归：ORM（及迁移）声明了索引，DB 里没有。

        这正是当时 prod 的状态 —— `ix_menu_config_visible_sort` 由 0033 创建、
        ORM 也一直声明着，唯独 prod 缺失，而旧检查只看表集合，全程没吭声。
        """
        indexName = "ix_menu_config_visible_sort"
        originalDef = asyncio_run(
            _fetchScalar(
                db_url,
                "SELECT indexdef FROM pg_indexes WHERE indexname = $1",
                indexName,
            )
        )
        assert originalDef, f"{indexName} 不在测试库，构造不出该场景"

        asyncio_run(_execSql(db_url, f"DROP INDEX {indexName}"))
        try:
            result = _runDriftScript(db_url)
            combined = result.stdout + result.stderr

            assert result.returncode == 1, combined
            assert "[orm:index]" in combined
            assert indexName in combined
        finally:
            asyncio_run(_execSql(db_url, str(originalDef)))

    def test_renamed_orm_column_is_blocking_drift(self, db_url: str) -> None:
        """ORM 声明的列在 DB 里不存在 → blocking（运行期查询必炸）。

        用 RENAME 而非 DROP：既构造出「名字对不上」，又不动任何一行数据。
        `menu_config.path` 不属于任何索引，改动面最小。
        """
        asyncio_run(
            _execSql(db_url, "ALTER TABLE menu_config RENAME COLUMN path TO path_drift_probe")
        )
        try:
            result = _runDriftScript(db_url)
            combined = result.stdout + result.stderr

            assert result.returncode == 1, combined
            assert "[orm:column]" in combined
            assert "menu_config.path" in combined
        finally:
            asyncio_run(
                _execSql(db_url, "ALTER TABLE menu_config RENAME COLUMN path_drift_probe TO path")
            )

    def test_extra_db_column_is_warning_not_blocking(self, db_url: str) -> None:
        """users 4 列事故回归：DB 有 ORM 不认识的列（手工 DDL 残留）。

        默认只警告不阻断 —— prod 上那 4 列带着真实 bcrypt 凭据，
        判成 blocking 等于让线上起不来。`--strict`（CI 门禁）才升级为漂移。
        """
        asyncio_run(
            _execSql(
                db_url,
                "ALTER TABLE menu_config ADD COLUMN IF NOT EXISTS drift_probe_col text",
            )
        )
        try:
            lenient = _runDriftScript(db_url)
            combined = lenient.stdout + lenient.stderr

            assert lenient.returncode == 0, combined
            assert "[db:column]" in combined
            assert "drift_probe_col" in combined

            strict = _runDriftScript(db_url, "--strict")
            assert strict.returncode == 1, strict.stdout + strict.stderr
        finally:
            asyncio_run(
                _execSql(db_url, "ALTER TABLE menu_config DROP COLUMN IF EXISTS drift_probe_col")
            )

    def test_stray_index_is_warning_not_blocking(self, db_url: str) -> None:
        """DB 上多一个 ORM 未声明的普通索引 —— 历史残留，不阻断启动。"""
        asyncio_run(
            _execSql(db_url, "CREATE INDEX IF NOT EXISTS ix_drift_probe ON menu_config (code)")
        )
        try:
            result = _runDriftScript(db_url)
            combined = result.stdout + result.stderr

            assert result.returncode == 0, combined
            assert "[db:index]" in combined
            assert "ix_drift_probe" in combined
        finally:
            asyncio_run(_execSql(db_url, "DROP INDEX IF EXISTS ix_drift_probe"))


class TestStartupSeverityPolicy:
    """启动阻断策略必须与 CLI 一致（否则 warning 会把容器挡在门外）。

    背景：扩展列/索引粒度之前，`main.py` 对**任何** issue 都 `raise`，而 CLI
    却按 severity 区分 —— 两者不一致。真实库上存在 26 个 ORM 未声明的历史索引，
    若把「DB 多出」也算 blocking，容器将永远起不来。
    """

    def test_main_py_uses_shared_severity_split(self) -> None:
        main_py = (_BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")

        assert "_splitBySeverity" in main_py, (
            "app/main.py 必须用 schema_drift 的 _splitBySeverity 区分"
            "blocking / warning，不能对所有 issue 一律 raise"
        )

    def test_drift_module_lives_inside_app_package(self) -> None:
        """本模块必须与 main.py 同处 `app/` 包内（2026-09-12 从 scripts/ 搬来的原因）。

        两者是**一对必须同版本部署的两半**。放在 `scripts/` 时，《部署》文档里的
        `docker cp backend/app/.` 只会带上 main.py，容器里留下旧实现 —— 实测两个
        错配方向都会让容器起不来：

        - 新 main + 旧模块：旧版返回 `list[str]`，`_splitBySeverity` 读 `.severity`
          → `AttributeError`，且该调用在 main.py 的 `try/except` 之外，
          连「请执行 alembic upgrade head」都打不出来
        - 旧 main + 新模块：旧 main 是 `if issues:`，本模块新增的 DB 多余索引
          warning 会把它顶成真 → 容器直接起不来

        按**包路径**判定（而非「源码里出现过某个字符串」）：只要 main.py 的 import
        是 `app.*` 起头、实现本体也确实在 `app/` 下，那一次 cp 就必然带全两半。
        注意别用 scripts/ 的 shim 来满足这条 —— shim 在 scripts/，同样 cp 不到。
        """
        main_py = (_BACKEND_ROOT / "app" / "main.py").read_text(encoding="utf-8")

        assert "from app.infrastructure.schema_drift import" in main_py, (
            "app/main.py 必须从 app.* import drift 模块，否则 docker cp app/. 会漏"
        )
        assert "from scripts.check_schema_drift import" not in main_py, (
            "不得再从 scripts.* import —— 那正是会造成「两半版本不一致」的路径"
        )
        assert (_BACKEND_ROOT / "app" / "infrastructure" / "schema_drift.py").is_file(), (
            "实现本体必须在 app/ 下；scripts/ 的同名文件只是 CLI shim，cp 不到"
        )

    def test_blocking_and_warning_codes_are_disjoint(self) -> None:
        """severity 表必须自洽：每个 code 都有归属，且 blocking 覆盖全部 missing。"""
        severity = _drift_module._SEVERITY_BY_CODE
        blocking = {c for c, s in severity.items() if s == "blocking"}

        assert "missing_column" in blocking
        assert "missing_index" in blocking
        assert "missing_table" in blocking
        assert "extra_column" not in blocking
        assert "extra_index" not in blocking
        assert "extra_table" not in blocking


def asyncio_run(coro):  # noqa: ANN001, ANN201
    """helper：同步包装 asyncpg 调用（不引入额外依赖）。"""
    import asyncio

    return asyncio.run(coro)