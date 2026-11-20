"""P5 工作流域数据模型（Workflow / Command / Approval）单测。

本文件**双向钉住** ORM 与 Alembic 迁移：

1. 六张表的表名 / 列名 / 类型 / 可空性；
2. 约束（唯一约束、CHECK、FK 的 ondelete、索引）；
3. 三个迁移的 revision 链、DDL 覆盖范围、列注释与 CHECK 表达式**逐字一致**；
4. ``downgrade()`` 与 ``upgrade()`` **对称**（只撤自己那几张表）。

为什么用「读迁移源码 + 比字符串」而不是 ``alembic check``：
``alembic check`` 只比**名**不比**式**（见 ``Harness/changes`` 里 CHECK 约束补进 ORM
的复盘：只补名 = 假绿）。本文件补上「表达式逐字比对」这一层，
再由 ``alembic check`` 在真库上兜底结构漂移。

为什么**不**新建 integration 测试文件：
CI 集成测试按「排序后下标 mod 4」轮转分片归属 ⇒ 新建文件会假 net-new
（见 ``Harness/changes/2026-11-16-p5-workflow-command/plan.md`` D-12）。
本文件是 unit（路径哈希分片）⇒ 新增安全。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from app.domain.workflow_models import (
    ApprovalTask,
    BusinessCommand,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowOutbox,
    WorkflowStep,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "alembic" / "versions"

# 迁移号已串行占号（2026-11-16）：P5 = 0149–0152，WP-ONT-AI 从 0153 起。
_MIGRATION_STEMS = (
    "0149_workflow_definition",
    "0150_workflow_step_command",
    "0151_approval_outbox",
)

# 「只改既有表约束」的迁移（**不建表**）：A4-0（0159）放开 ``approval_task`` 的
# ``instance_id`` 并把幂等键换成 subject 三元组。
# ⚠️ **不在** ``_MIGRATION_STEMS`` 里 —— 那组受「纯加法 DDL」纪律约束
# （禁 alter / drop 任何既有表），而 0159 正是改既有表的约束。
_CONSTRAINT_MIGRATION_STEMS = ("0159_approval_task_subject",)

_EXPECTED_TABLES_BY_MIGRATION = {
    "0149_workflow_definition": ["workflow_definition", "workflow_instance"],
    "0150_workflow_step_command": ["workflow_step", "business_command"],
    "0151_approval_outbox": ["approval_task", "workflow_outbox"],
}

_ALL_MODELS = (
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowStep,
    BusinessCommand,
    ApprovalTask,
    WorkflowOutbox,
)


def _loadMigration(stem: str):
    path = _MIGRATIONS_DIR / f"{stem}.py"
    assert path.exists(), f"迁移文件不存在：{path}"
    spec = importlib.util.spec_from_file_location(f"_mig_{stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recordedOps(module, funcName: str) -> list[tuple]:
    """跑一遍迁移函数，记录它对 ``alembic.op`` 发的 DDL 调用。"""

    calls: list[tuple] = []

    class _FakeOp:
        def create_table(self, tableName, *columns, **kwargs):
            calls.append(("create_table", tableName, tuple(columns)))

        def create_index(self, name, tableName, *columns, **kwargs):
            calls.append(("create_index", name, tableName, kwargs))

        # 约束改写类 DDL（0159 用）—— 必须连 kwargs 一起记，否则「可空」与
        # 「约束类型」这类**关键字参数**会被吞掉（假绿）。
        def create_unique_constraint(self, name, tableName, columns, **kwargs):
            calls.append(("create_unique_constraint", name, tableName, tuple(columns), kwargs))

        def drop_constraint(self, name, tableName, **kwargs):
            calls.append(("drop_constraint", name, tableName, kwargs))

        def alter_column(self, tableName, columnName, **kwargs):
            calls.append(("alter_column", tableName, columnName, kwargs))

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


def _constraintColumnNames(constraint) -> tuple[str, ...]:
    """取约束的列名。

    迁移里 ``sa.UniqueConstraint("a", "b", name=...)`` 是**未绑定表**的独立对象
    ⇒ ``constraint.columns`` 为空，列名还在 ``_pending_colargs`` 里（是字符串）。
    绑定到表之后（ORM 侧）``columns`` 才可读。
    """

    names = tuple(col.name for col in constraint.columns)
    if names:
        return names
    return tuple(getattr(col, "name", col) for col in getattr(constraint, "_pending_colargs", ()))


def _uniqueConstraints(table) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint):
            result[constraint.name] = _constraintColumnNames(constraint)
    return result


def _foreignKeys(table) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for constraint in table.constraints:
        if isinstance(constraint, sa.ForeignKeyConstraint):
            targets = [f"{fk.target_fullname}" for fk in constraint.elements]
            result[constraint.name] = (tuple(sorted(targets))[0], constraint.ondelete or "")
    return result


def _indexNames(table) -> set[str]:
    return {index.name for index in table.indexes}


def _migrationTablesByStem(stem: str) -> dict[str, dict]:
    """返回 {表名: {'columns': {...}, 'checks': {...}, 'uniques': {...}, 'nullable': {...}}}。"""

    module = _loadMigration(stem)
    result: dict[str, dict] = {}
    for call in _recordedOps(module, "upgrade"):
        if call[0] != "create_table":
            continue
        tableName = call[1]
        columns: dict[str, sa.Column] = {}
        checks: dict[str, str] = {}
        uniques: dict[str, tuple[str, ...]] = {}
        for item in call[2]:
            if isinstance(item, sa.Column):
                columns[item.name] = item
            elif isinstance(item, sa.CheckConstraint):
                checks[item.name] = str(item.sqltext)
            elif isinstance(item, sa.UniqueConstraint):
                uniques[item.name] = _constraintColumnNames(item)
        result[tableName] = {
            "columns": columns,
            "checks": checks,
            "uniques": uniques,
            "nullable": {name: column.nullable for name, column in columns.items()},
        }
    return result


def _applyConstraintMigration(tables: dict[str, dict], stem: str) -> None:
    """把「只改既有表约束」的迁移应用到**状态**上（原地演进 tables）。

    ⚠️ 0159 不建表 ⇒ 只靠 ``create_table`` 扫不到它的效果 ⇒
    ``drop_constraint`` / ``create_unique_constraint`` / ``alter_column``
    必须也纳入状态机 ⇒ 语料反映的是**迁移链跑完后的有效结构**。
    """

    module = _loadMigration(stem)
    for call in _recordedOps(module, "upgrade"):
        if call[0] == "create_unique_constraint":
            _, name, tableName, columns, _kwargs = call
            tables[tableName]["uniques"][name] = columns
        elif call[0] == "drop_constraint":
            _, name, tableName, _kwargs = call
            tables[tableName]["uniques"].pop(name, None)
        elif call[0] == "alter_column":
            _, tableName, columnName, kwargs = call
            if "nullable" in kwargs:
                tables[tableName]["nullable"][columnName] = kwargs["nullable"]


def _allMigrationTables() -> dict[str, dict]:
    merged: dict[str, dict] = {}
    for stem in _MIGRATION_STEMS:
        merged.update(_migrationTablesByStem(stem))
    for stem in _CONSTRAINT_MIGRATION_STEMS:
        _applyConstraintMigration(merged, stem)
    return merged


# ---------------------------------------------------------------- 表与列


def test_六张表名与契约一致():
    """契约 §6 P5：`workflow_definition` / `workflow_instance` / `workflow_step` /
    `business_command` / `approval_task` + outbox 独立表（D-5）。"""

    assert {
        WorkflowDefinition.__tablename__,
        WorkflowInstance.__tablename__,
        WorkflowStep.__tablename__,
        BusinessCommand.__tablename__,
        ApprovalTask.__tablename__,
        WorkflowOutbox.__tablename__,
    } == {
        "workflow_definition",
        "workflow_instance",
        "workflow_step",
        "business_command",
        "approval_task",
        "workflow_outbox",
    }


def test_定义表列集合():
    assert set(WorkflowDefinition.__table__.columns.keys()) == {
        "id",
        "domain_code",
        "capability_code",
        "version",
        "definition_json",
        "status",
        "created_by",
        "created_time",
        "updated_time",
    }


def test_实例表列集合():
    # ⚠️ 这里**没有** ``current_step``：那一列早期是「预留」（全仓无人写 ⇒ 永远
    # 是 NULL ⇒ 接口恒返回 null ⇒ 一个会骗人的字段）⇒ 已从迁移 + ORM + DTO 一并
    # 删除。
    #
    # ⚠️ 这里也**没有** ``business_key`` / ``result_json``：两列**全仓无人写**
    # （``createInstance(businessKey=…)`` / ``advanceInstance(resultJson=…)``
    # 的参数没有任何调用方传值 ⇒ 永远是 NULL）⇒ 留着的代价是：
    # ① 接口恒返回 null（骗人的字段）；② 运维看到列以为「有业务单号/结果体」
    # ⇒ 与 ``current_step`` 同款 ⇒ 一并删除。
    #
    # ⚠️ **「原地改」的前提曾经是错的（2026-11-17 已纠正）**：这三列的删除、
    # ``created_by`` 改 NOT NULL、幂等唯一约束（含 ``created_by``）都是
    # **0149 已在开发库（5433）执行过之后**才改的 ⇒ 那个库的 P5 表结构
    # **已陈旧**（多 3 列 / 缺 NOT NULL / 唯一约束少一列 / 3 处注释过期）
    # ⇒ 开发库必须**重建**（一次性脚本见
    # ``Harness/changes/2026-11-16-p5-workflow-command/deploy-prod-1117.md`` §14）。
    # 生产从未跑过 0149（停在 0143）⇒ 生产不受影响。
    assert set(WorkflowInstance.__table__.columns.keys()) == {
        "id",
        "definition_id",
        "idempotency_key",
        "status",
        "request_json",
        "error_message",
        "created_by",
        "created_time",
        "updated_time",
    }


def test_实例读DTO不含businessKey():
    """实例摘要**不再暴露** ``businessKey``（死列已删 ⇒ 对外契约同步收口）。

    留着它 ⇒ 客户端永远收到 ``businessKey: null`` ⇒ 又一个骗人的字段
    （与被删掉的 ``current_step`` 同款）。
    """

    from app.domain.workflow_schemas import WorkflowInstanceRead

    assert "business_key" not in WorkflowInstanceRead.model_fields


def test_步骤表列集合():
    assert set(WorkflowStep.__table__.columns.keys()) == {
        "id",
        "instance_id",
        "step_code",
        "step_order",
        "status",
        "input_json",
        "output_json",
        "error_message",
        "started_at",
        "finished_at",
        "created_time",
        "updated_time",
    }


def test_命令表列集合():
    assert set(BusinessCommand.__table__.columns.keys()) == {
        "id",
        "instance_id",
        "step_id",
        "command_type",
        "idempotency_key",
        "status",
        "payload",
        "result",
        "error_message",
        "attempts",
        "executed_by",
        "created_time",
        "updated_time",
    }


def test_审批表列集合():
    assert set(ApprovalTask.__table__.columns.keys()) == {
        "id",
        "instance_id",
        "step_id",
        "subject_type",
        "subject_id",
        "matrix_code",
        "status",
        "approver_role",
        "approver_user",
        "decision_note",
        "due_at",
        "decided_at",
        "created_by",
        "created_time",
        "updated_time",
    }


def test_outbox表列集合():
    assert set(WorkflowOutbox.__table__.columns.keys()) == {
        "id",
        "instance_id",
        "event_type",
        "payload",
        "status",
        "attempts",
        "last_error",
        "available_at",
        "lease_owner",
        "lease_expires_at",
        "processed_at",
        "created_time",
    }


# ---------------------------------------------------------------- 契约语义


def test_审批对象模型开放可复用():
    """D-1：审批面**不得**写死成工作流实例 ⇒ 用 subject 型引用。

    WP-ONT-AI A4「四眼审核」是同构审批需求 ⇒ 同一张表 + 同一套矩阵键即可复用。
    """

    columns = ApprovalTask.__table__.columns
    assert "subject_type" in columns
    assert "subject_id" in columns
    # subject 引用与实例引用并存：实例是「谁发起的」，subject 是「批什么」。
    assert "instance_id" in columns
    assert columns["subject_type"].nullable is False
    assert columns["subject_id"].nullable is False


def test_幂等键是独立唯一约束列():
    """D-2：幂等键**独立列** + ``UniqueConstraint``（JSONB 里的键做不了唯一约束）。

    实例的幂等作用域 = ``(definition_id, created_by, idempotency_key)``：
    **按提交人私有** ⇒ ① 别人的键不会被「占死」（DoS）；② 复用别人的键只会
    拿到**自己的**实例（不泄露别人的实例、也不泄露「这个键存在」）。
    """

    uniques = _uniqueConstraints(BusinessCommand.__table__)
    assert uniques["uq_business_command_idempotency"] == (
        "command_type",
        "idempotency_key",
    )
    instanceUniques = _uniqueConstraints(WorkflowInstance.__table__)
    assert instanceUniques["uq_workflow_instance_idempotency"] == (
        "definition_id",
        "created_by",
        "idempotency_key",
    )
    # ⚠️ 约束作用域里不能有 NULL 列：PG 里 NULL 互不相等 ⇒ created_by 可空时
    #    幂等保证会**静默失效**（同一个键能落两行）。
    assert WorkflowInstance.__table__.columns["created_by"].nullable is False
    # 幂等键是**独立标量列**（不是 JSONB 里的字段）⇒ 库级可加唯一约束
    keyColumn = BusinessCommand.__table__.columns["idempotency_key"]
    assert keyColumn.type.__class__.__name__ == "String"
    assert keyColumn.nullable is False


def test_定义版本在同一域能力下唯一():
    uniques = _uniqueConstraints(WorkflowDefinition.__table__)
    assert uniques["uq_workflow_definition_scope"] == (
        "domain_code",
        "capability_code",
        "version",
    )


def test_审批任务在同一被审对象同一矩阵条目唯一():
    """A4-0（0159）：幂等键 = subject 三元组（**三列全 NOT NULL**）。

    ⚠️ 为什么不能留在 ``(instance_id, matrix_code)``：PG 里 NULL 互不相等 ⇒
    提案路径（``instance_id IS NULL``）的幂等会**静默失效** ⇒ 同一提案的同一
    矩阵条目能落两行 ⇒ 四眼失效。
    """

    uniques = _uniqueConstraints(ApprovalTask.__table__)
    assert uniques["uq_approval_task_subject"] == (
        "subject_type",
        "subject_id",
        "matrix_code",
    )
    assert "uq_approval_task_matrix" not in uniques, "旧幂等键必须已被换掉"


def test_审批任务实例列可空与迁移一致():
    """A4-0（0159）：``instance_id`` 可空（审批不一定挂在工作流实例上）。"""

    payload = _allMigrationTables()["approval_task"]
    assert payload["nullable"]["instance_id"] is True
    assert ApprovalTask.__table__.columns["instance_id"].nullable is True


def test_所有外键删除受限():
    """证据不悬空：实例 / 定义 / 步骤都是审计证据 ⇒ FK 全部 RESTRICT。"""

    expected = {
        WorkflowInstance: {"fk_workflow_instance_definition": "workflow_definition.id"},
        WorkflowStep: {"fk_workflow_step_instance": "workflow_instance.id"},
        BusinessCommand: {
            "fk_business_command_instance": "workflow_instance.id",
            "fk_business_command_step": "workflow_step.id",
        },
        ApprovalTask: {
            "fk_approval_task_instance": "workflow_instance.id",
            "fk_approval_task_step": "workflow_step.id",
        },
        WorkflowOutbox: {"fk_workflow_outbox_instance": "workflow_instance.id"},
    }
    for model, mapping in expected.items():
        fks = _foreignKeys(model.__table__)
        for name, target in mapping.items():
            assert fks[name] == (target, "RESTRICT"), f"{model.__tablename__}.{name}"


def test_状态字面量被check钉死():
    """状态机是**纯函数矩阵**（D-4）⇒ 库层也把字面量钉死（未知状态不许入库）。"""

    expected = {
        WorkflowDefinition: (
            "ck_workflow_definition_status",
            "status IN ('DRAFT','PUBLISHED','DISABLED')",
        ),
        WorkflowInstance: (
            "ck_workflow_instance_status",
            "status IN ('PENDING','RUNNING','WAITING_APPROVAL','APPROVED',"
            "'REJECTED','COMPLETED','FAILED','CANCELLED','COMPENSATED')",
        ),
        WorkflowStep: (
            "ck_workflow_step_status",
            "status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','SKIPPED','COMPENSATED')",
        ),
        BusinessCommand: (
            "ck_business_command_status",
            "status IN ('PENDING','EXECUTING','SUCCEEDED','FAILED','COMPENSATED','CANCELLED','ABORTED')",
        ),
        ApprovalTask: (
            "ck_approval_task_status",
            "status IN ('PENDING','APPROVED','REJECTED','CANCELLED','TIMEOUT')",
        ),
        WorkflowOutbox: (
            "ck_workflow_outbox_status",
            "status IN ('PENDING','PROCESSING','PROCESSED','FAILED')",
        ),
    }
    for model, (name, expression) in expected.items():
        checks = _checkExpressions(model.__table__)
        assert checks.get(name) == expression, f"{model.__tablename__}.{name}"


def test_json列必须是对象():
    """定义体 / 请求体塞成数组或字符串 ⇒ 解析侧会炸 ⇒ 库层钉死。"""

    assert (
        _checkExpressions(WorkflowDefinition.__table__)["ck_workflow_definition_json"]
        == "jsonb_typeof(definition_json) = 'object'"
    )
    assert (
        _checkExpressions(WorkflowInstance.__table__)["ck_workflow_instance_request"]
        == "jsonb_typeof(request_json) = 'object'"
    )


def test_计数与序号不得为负():
    assert _checkExpressions(BusinessCommand.__table__)["ck_business_command_attempts"] == "attempts >= 0"
    assert _checkExpressions(WorkflowOutbox.__table__)["ck_workflow_outbox_attempts"] == "attempts >= 0"
    assert _checkExpressions(WorkflowStep.__table__)["ck_workflow_step_order"] == "step_order >= 0"


def test_outbox有毒丸防护字段():
    """D-5：照抄 ``AuditOutbox`` 的毒丸防护（attempts / last_error / processed_at）。"""

    columns = WorkflowOutbox.__table__.columns
    for name in ("attempts", "last_error", "processed_at", "available_at"):
        assert name in columns


def test_索引齐备():
    assert "ix_workflow_definition_scope" in _indexNames(WorkflowDefinition.__table__)
    assert "ix_workflow_instance_definition" in _indexNames(WorkflowInstance.__table__)
    assert "ix_workflow_instance_status" in _indexNames(WorkflowInstance.__table__)
    assert "ix_workflow_step_instance" in _indexNames(WorkflowStep.__table__)
    assert "ix_business_command_instance" in _indexNames(BusinessCommand.__table__)
    assert "ix_business_command_status" in _indexNames(BusinessCommand.__table__)
    assert "ix_approval_task_instance" in _indexNames(ApprovalTask.__table__)
    assert "ix_approval_task_status" in _indexNames(ApprovalTask.__table__)
    assert "ix_workflow_outbox_pending" in _indexNames(WorkflowOutbox.__table__)


def test_生效版本部分唯一索引_orm与迁移逐字一致():
    """同一域能力下**至多一条** PUBLISHED（审批层级 = 安全边界 ⇒ 库层钉死）。

    ORM 与迁移两侧的**谓词**必须逐字一致 —— 只比对「索引名存在」不够：
    谓词写错（少了 WHERE、或条件不同）⇒ 索引要么失效、要么误伤草稿版本。
    """

    expectedPredicate = "status = 'PUBLISHED'"

    ormIndex = {
        index.name: index for index in WorkflowDefinition.__table__.indexes
    }["uq_workflow_definition_published"]
    assert ormIndex.unique is True, "生效版本唯一索引必须是 UNIQUE"
    assert [column.name for column in ormIndex.columns] == ["domain_code", "capability_code"]
    ormWhere = ormIndex.dialect_options["postgresql"]["where"]
    assert str(ormWhere) == expectedPredicate, f"ORM 谓词变了：{ormWhere}"

    migrationCalls = [
        call
        for call in _recordedOps(_loadMigration("0149_workflow_definition"), "upgrade")
        if call[0] == "create_index"
    ]
    migrationIndex = {call[1]: call[3] for call in migrationCalls}[
        "uq_workflow_definition_published"
    ]
    assert migrationIndex["unique"] is True, "迁移侧索引必须 UNIQUE"
    assert str(migrationIndex["postgresql_where"]) == expectedPredicate, (
        f"迁移谓词变了：{migrationIndex['postgresql_where']}"
    )


# ---------------------------------------------------------------- 迁移一致性


def test_迁移revision链正确且串行():
    modules = [_loadMigration(stem) for stem in _MIGRATION_STEMS]
    assert [module.revision for module in modules] == ["0149", "0150", "0151"]
    assert [module.down_revision for module in modules] == ["0148", "0149", "0150"]


def test_约束改写迁移串在审批域之后():
    """A4-0（0159）接在**当前链尾**（0158）之后 ⇒ 迁移号串行、不留空号。"""

    module = _loadMigration(_CONSTRAINT_MIGRATION_STEMS[0])
    assert module.revision == "0159"
    assert module.down_revision == "0158"
    assert (_MIGRATIONS_DIR / "0158_procurement_object_reconciliation.py").exists()


def test_迁移文件名不超过32字符():
    """仓库约定：迁移文件名 ≤ 32 字符（``alembic_version.version_num`` 是 varchar(32)）。"""

    for stem in _MIGRATION_STEMS + _CONSTRAINT_MIGRATION_STEMS:
        assert len(f"{stem}.py") <= 32, f"{stem}.py 超过 32 字符"


def test_约束改写迁移只动一张表的约束():
    """0159 是**改既有表**的迁移 ⇒ 纪律：不建表、不删表、不执行裸 SQL。"""

    module = _loadMigration(_CONSTRAINT_MIGRATION_STEMS[0])
    calls = _recordedOps(module, "upgrade")
    assert [call[0] for call in calls] == [
        "alter_column",
        "drop_constraint",
        "create_unique_constraint",
    ], calls
    assert not [call for call in calls if call[0] in {"create_table", "drop_table", "execute"}]


def test_约束改写迁移升级与降级对称():
    """升级换键 ⇒ 降级换回 ⇒ 两次的键与列集必须**互换**。"""

    module = _loadMigration(_CONSTRAINT_MIGRATION_STEMS[0])
    up = _recordedOps(module, "upgrade")
    down = _recordedOps(module, "downgrade")

    upDrop = [call for call in up if call[0] == "drop_constraint"][0]
    upCreate = [call for call in up if call[0] == "create_unique_constraint"][0]
    downDrop = [call for call in down if call[0] == "drop_constraint"][0]
    downCreate = [call for call in down if call[0] == "create_unique_constraint"][0]

    assert upDrop[1] == downCreate[1], "升级删的键 = 降级建的键"
    assert upCreate[1] == downDrop[1], "升级建的键 = 降级删的键"
    assert upCreate[3] == ("subject_type", "subject_id", "matrix_code")
    assert downCreate[3] == ("instance_id", "matrix_code"), "降级换回旧键的旧列集"

    upAlter = [call for call in up if call[0] == "alter_column"][0]
    downAlter = [call for call in down if call[0] == "alter_column"][0]
    assert upAlter[1:3] == downAlter[1:3], "改的列必须一致"
    assert upAlter[3]["nullable"] is True
    assert downAlter[3]["nullable"] is False


def test_每个迁移只建自己的新表不触碰既有表():
    """纯加法 DDL：不 alter / drop 任何既有表（本体发现域 0146 的同一纪律）。"""

    forbidden = {
        "drop_table",
        "alter_column",
        "add_column",
        "drop_column",
        "create_index_unique",
        "execute",
    }
    for stem in _MIGRATION_STEMS:
        module = _loadMigration(stem)
        calls = _recordedOps(module, "upgrade")
        created = [call[1] for call in calls if call[0] == "create_table"]
        assert created == _EXPECTED_TABLES_BY_MIGRATION[stem], stem
        assert not [call for call in calls if call[0] in forbidden], stem


def test_降级与升级对称():
    """规范硬约束：``downgrade()`` 必须对称反转 ``upgrade()``。

    只撤其中一张表 ⇒ 库停在「没有任何 revision 描述」的状态。
    """

    for stem in _MIGRATION_STEMS:
        module = _loadMigration(stem)
        upgradeTables = [call[1] for call in _recordedOps(module, "upgrade") if call[0] == "create_table"]
        downgradeCalls = _recordedOps(module, "downgrade")
        dropped = [call[1] for call in downgradeCalls if call[0] == "drop_table"]
        assert dropped == list(reversed(upgradeTables)), stem
        # 索引先于表撤销
        dropIndexes = [call[1] for call in downgradeCalls if call[0] == "drop_index"]
        for indexName in dropIndexes:
            assert indexName, stem


def test_迁移列注释与orm双向逐字一致():
    """ORM ``comment`` 与迁移 ``comment`` 必须**双向**逐字一致。

    「ORM 的注释出现在迁移源码文本里」这种单向断言有两个盲区：
    ① 注释挂到**错误的列**上照样通过；② 迁移里有、ORM 里没有的注释测不出来。
    ⇒ 这里按 (表名, 列名) 建映射做**双向字典比对**。
    """

    ormComments = {
        (table.name, column.name): column.comment
        for table in (model.__table__ for model in _ALL_MODELS)
        for column in table.columns
        if column.comment
    }
    migrationComments: dict[tuple[str, str], str] = {}
    for tableName, payload in _allMigrationTables().items():
        for columnName, column in payload["columns"].items():
            if column.comment:
                migrationComments[(tableName, columnName)] = column.comment

    assert ormComments == migrationComments, (
        "ORM 与迁移的列注释不一致："
        f"仅 ORM 有={sorted(set(ormComments) - set(migrationComments))}，"
        f"仅迁移有={sorted(set(migrationComments) - set(ormComments))}，"
        "内容不同="
        f"{sorted(k for k in set(ormComments) & set(migrationComments) if ormComments[k] != migrationComments[k])}"
    )


def test_迁移check表达式与orm逐字一致():
    """``alembic check`` 只比约束名不比式 ⇒ 这一层由单测兜住（只补名 = 假绿）。"""

    migrationChecks: dict[str, str] = {}
    migrationUniques: dict[str, tuple[str, ...]] = {}
    for payload in _allMigrationTables().values():
        migrationChecks.update(payload["checks"])
        migrationUniques.update(payload["uniques"])

    for model in _ALL_MODELS:
        table = model.__table__
        for name, expression in _checkExpressions(table).items():
            assert name in migrationChecks, f"迁移缺少 CHECK 约束：{name}"
            assert migrationChecks[name] == expression, f"CHECK 表达式不一致：{name}"
        for name, columns in _uniqueConstraints(table).items():
            assert name in migrationUniques, f"迁移缺少唯一约束：{name}"
            assert migrationUniques[name] == columns, f"唯一约束列不一致：{name}"


def test_迁移列集合与orm一致():
    """列名集合逐字比对 ⇒ 迁移少建/多建一列都会被抓住。"""

    migrationColumns = {
        tableName: set(payload["columns"]) for tableName, payload in _allMigrationTables().items()
    }
    for model in _ALL_MODELS:
        table = model.__table__
        assert set(table.columns.keys()) == migrationColumns[table.name], table.name
