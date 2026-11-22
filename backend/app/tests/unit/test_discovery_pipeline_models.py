"""发现流水线数据模型（WP-ONT-AI A2 / G3，迁移 0166）单测。

本文件**双向钉住** ORM 与 Alembic 迁移：

1. 六张表的表名 / 列集合 / 可空性；
2. 约束（CHECK 表达式逐字、FK ondelete、唯一索引含 `COALESCE` 表达式）；
3. **LLM 计量列必须存在**（V0.3 强制口径，发现域一旦引入 LLM 就要计量）；
4. **候选证据不得为空**（证据完整率 100% 是硬红线 ⇒ 库层 CHECK）；
5. 迁移 revision 链、只建这六张表、列注释与 CHECK 表达式与 ORM 逐字一致。

为什么不用 `alembic check` 兜这一层：它**只比约束名不比式**（本仓既有教训），
表达式必须由单测逐字比对；`alembic check` 只负责真库上的结构漂移。
"""

from __future__ import annotations

import importlib.util
import pathlib

import sqlalchemy as sa

from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryEvaluation,
    DiscoveryFinding,
    DiscoveryProfile,
    DiscoveryProposal,
    DiscoveryReview,
    DiscoveryRun,
)

_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[3] / "alembic" / "versions"
_MIGRATION_STEM = "0166_discovery_pipeline"
_EVALUATION_MIGRATION_STEM = "0167_discovery_evaluation"

_TABLES = (
    DiscoveryRun.__table__,
    DiscoveryProfile.__table__,
    DiscoveryCandidate.__table__,
    DiscoveryFinding.__table__,
    DiscoveryProposal.__table__,
    DiscoveryReview.__table__,
    DiscoveryEvaluation.__table__,
)


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

        def create_index(self, name, tableName, columns=None, **kwargs):
            calls.append(
                ("create_index", name, tableName, tuple(columns or ()), dict(kwargs))
            )

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
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _foreignKeys(table) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for constraint in table.constraints:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            target = sorted(str(fk.target_fullname) for fk in constraint.elements)[0]
            result[constraint.name] = (target, constraint.ondelete or "")
    return result


def _migrationColumnComments(module) -> dict[tuple[str, str], str]:
    comments: dict[tuple[str, str], str] = {}
    for call in _recordedOps(module, "upgrade"):
        if call[0] != "create_table":
            continue
        tableName = call[1]
        for column in call[2]:
            if isinstance(column, sa.Column) and column.comment:
                comments[(tableName, column.name)] = column.comment
    return comments


# ---------------------------------------------------------------- 表与列


def test_七张表都在discovery命名空间内():
    names = {table.name for table in _TABLES}

    assert names == {
        "discovery_run",
        "discovery_profile",
        "discovery_candidate",
        "discovery_finding",
        "discovery_proposal",
        "discovery_review",
        "discovery_evaluation",
    }
    assert not any(name.startswith("ontology_") for name in names), (
        "红线：发现域不得把自己写进 ontology_* 命名空间"
    )


def test_运行表带llm计量列():
    """V0.3 强制：每次 LLM 调用记录 token 与成本 ⇒ 发现域一引入 LLM 就必须有这三列。"""

    columns = set(DiscoveryRun.__table__.columns.keys())

    assert {"prompt_tokens", "completion_tokens", "cost"} <= columns
    assert DiscoveryRun.__table__.columns["cost"].type.precision == 12
    assert DiscoveryRun.__table__.columns["cost"].type.scale == 6


def test_可交互表有更新时间而证据表没有():
    """候选/问题/提案会变状态 ⇒ 有 `updated_time`；剖析/审核/运行是证据 ⇒ 只增不改。"""

    for table in (
        DiscoveryCandidate.__table__,
        DiscoveryFinding.__table__,
        DiscoveryProposal.__table__,
    ):
        assert "updated_time" in table.columns, table.name
    for table in (
        DiscoveryRun.__table__,
        DiscoveryProfile.__table__,
        DiscoveryReview.__table__,
    ):
        assert "updated_time" not in table.columns, table.name


def test_候选证据结构至少包含payload与evidence():
    columns = set(DiscoveryCandidate.__table__.columns.keys())

    assert {"payload", "evidence_refs", "confidence", "model_version", "prompt_version"} <= columns


# ---------------------------------------------------------------- 约束


def test_可空jsonb列写sql_null而不是json_null():
    """CHECK 用 `IS NULL`（SQL NULL）判空 ⇒ 列类型必须 `none_as_null=True`。

    JSONB 默认把 Python `None` 存成 **JSON `null`**（不是 SQL NULL）⇒ 会直接违反
    `sample_values IS NULL OR jsonb_typeof(...)='array'`。这是 JSONB 的经典坑。
    """
    from sqlalchemy.dialects.postgresql import JSONB

    for table, columnName in (
        (DiscoveryProfile.__table__, "sample_values"),
        (DiscoveryProposal.__table__, "impact_json"),
    ):
        columnType = table.columns[columnName].type
        assert isinstance(columnType, JSONB), columnName
        assert columnType.none_as_null is True, columnName


def test_候选证据不得为空():
    checks = _checkExpressions(DiscoveryCandidate.__table__)

    assert checks["ck_discovery_candidate_evidence_nonempty"] == (
        "jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) > 0"
    )


def test_候选置信度必须在0到1之间():
    checks = _checkExpressions(DiscoveryCandidate.__table__)

    assert checks["ck_discovery_candidate_confidence_range"] == (
        "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)"
    )


def test_字面量列被check钉死():
    assert _checkExpressions(DiscoveryRun.__table__)["ck_discovery_run_status"] == (
        "status IN ('RUNNING','SUCCEEDED','FAILED')"
    )
    assert _checkExpressions(DiscoveryFinding.__table__)["ck_discovery_finding_severity"] == (
        "severity IN ('ERROR','WARNING','INFO')"
    )
    assert _checkExpressions(DiscoveryProposal.__table__)["ck_discovery_proposal_status"] == (
        "status IN ('DRAFT','IN_REVIEW','APPROVED','REJECTED','PUBLISHED')"
    )
    assert _checkExpressions(DiscoveryReview.__table__)["ck_discovery_review_decision"] == (
        "decision IN ('APPROVE','REJECT','REQUEST_CHANGES')"
    )


def test_外键删除策略():
    """run 产出的证据随 run 级联；源/快照/发布是引用 ⇒ RESTRICT；审批任务 SET NULL。"""

    assert _foreignKeys(DiscoveryRun.__table__)["fk_discovery_run_source"] == (
        "discovery_source.id",
        "RESTRICT",
    )
    assert _foreignKeys(DiscoveryProfile.__table__)["fk_discovery_profile_run"] == (
        "discovery_run.id",
        "CASCADE",
    )
    assert _foreignKeys(DiscoveryCandidate.__table__)["fk_discovery_candidate_run"] == (
        "discovery_run.id",
        "CASCADE",
    )
    assert _foreignKeys(DiscoveryProposal.__table__)[
        "fk_discovery_proposal_base_release"
    ] == ("semantic_release.id", "RESTRICT")
    assert _foreignKeys(DiscoveryReview.__table__)[
        "fk_discovery_review_approval_task"
    ] == ("approval_task.id", "SET NULL")


def test_评审的审批任务号允许为空():
    """首期用轻量审核；二期接 P5 审批时回填 ⇒ 外键必须可空。"""

    assert DiscoveryReview.__table__.columns["approval_task_id"].nullable is True


def test_剖析在同一run内按表列唯一且列可为空():
    index = next(
        index
        for index in DiscoveryProfile.__table__.indexes
        if index.name == "uq_discovery_profile_scope"
    )

    assert index.unique is True
    assert DiscoveryProfile.__table__.columns["column_name"].nullable is True
    # COALESCE 表达式不可省：NULL 互不相等 ⇒ 表级指标行会无限重复
    assert any("COALESCE(column_name" in str(expr) for expr in index.expressions)


# ---------------------------------------------------------------- 迁移一致性


def test_迁移文件存在且revision链正确():
    module = _loadMigration()

    assert module.revision == "0166"
    assert module.down_revision == "0165"


def test_迁移只建这六张新表不触碰既有表():
    module = _loadMigration()
    calls = _recordedOps(module, "upgrade")
    created = [call[1] for call in calls if call[0] == "create_table"]

    assert created == [
        "discovery_run",
        "discovery_profile",
        "discovery_candidate",
        "discovery_finding",
        "discovery_proposal",
        "discovery_review",
    ]
    forbidden = {"drop_table", "alter_column", "add_column", "drop_column"}
    assert not [call for call in calls if call[0] in forbidden]


def test_迁移列注释与orm逐字一致():
    comments = _migrationColumnComments(_loadMigration())
    comments.update(_migrationColumnComments(_loadMigration(_EVALUATION_MIGRATION_STEM)))
    ormComments = {
        (table.name, column.name): column.comment
        for table in _TABLES
        for column in table.columns
        if column.comment
    }

    migrationComments = comments

    assert ormComments == migrationComments, (
        "ORM 与迁移的列注释不一致："
        f"仅 ORM 有={sorted(set(ormComments) - set(migrationComments))}，"
        f"仅迁移有={sorted(set(migrationComments) - set(ormComments))}，"
        "内容不同="
        f"{sorted(k for k in set(ormComments) & set(migrationComments) if ormComments[k] != migrationComments[k])}"
    )


def test_迁移check表达式与orm逐字一致():
    """`alembic check` 只比约束名不比式 ⇒ 这一层由单测兜住（只补名 = 假绿）。"""

    migrationChecks: dict[str, str] = {}
    for module in (
        _loadMigration(),
        _loadMigration(_EVALUATION_MIGRATION_STEM),
    ):
        for call in _recordedOps(module, "upgrade"):
            if call[0] != "create_table":
                continue
            for item in call[2]:
                if isinstance(item, sa.CheckConstraint):
                    migrationChecks[item.name] = str(item.sqltext)

    for table in _TABLES:
        for name, expression in _checkExpressions(table).items():
            assert name in migrationChecks, f"迁移缺少 CHECK 约束：{name}"
            assert migrationChecks[name] == expression, f"CHECK 表达式不一致：{name}"


def test_迁移的唯一索引也是coalesce表达式且唯一():
    module = _loadMigration()
    calls = _recordedOps(module, "upgrade")
    indexCalls = [
        call for call in calls if call[0] == "create_index" and call[1] == "uq_discovery_profile_scope"
    ]

    assert len(indexCalls) == 1
    _, _, tableName, columns, kwargs = indexCalls[0]
    assert tableName == "discovery_profile"
    assert kwargs.get("unique") is True
    assert any("COALESCE(column_name" in str(column) for column in columns)


def test_迁移文件名不超过32字符():
    assert len(f"{_MIGRATION_STEM}.py") <= 32


def test_评测迁移链在0166之后且只建评测表():
    module = _loadMigration(_EVALUATION_MIGRATION_STEM)

    assert module.revision == "0167"
    assert module.down_revision == "0166"
    created = [call[1] for call in _recordedOps(module, "upgrade") if call[0] == "create_table"]
    assert created == ["discovery_evaluation"]
    assert len(f"{_EVALUATION_MIGRATION_STEM}.py") <= 32
