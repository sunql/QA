"""语义发布表 `semantic_release` 的模型契约（P1a / D4）。

设计依据：`docs/update/1109/p0/裁决记录.md`
- 裁决一：发布快照自包含 ⇒ 本表必须自带 `manifest_json` + `manifest_hash`；
- 裁决四：首期全局快照 ⇒ `domain_code` 可空，非空值必须指向 `data_domain.code`。

本文件只钉**模型契约**（列、类型、可空性、索引、注册），迁移的升级/降级往返
在真实 PG 上单独验证。
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app.domain.enums import SemanticReleaseStatus
from app.domain.models import Base
from app.domain.semantic_release_models import SemanticRelease


def test_table_name() -> None:
    assert SemanticRelease.__tablename__ == "semantic_release"


def test_required_columns_exist() -> None:
    columns = SemanticRelease.__table__.columns

    for name in (
        "id",
        "domain_code",
        "version",
        "status",
        "schema_version",
        "manifest_json",
        "manifest_hash",
        "created_by",
        "created_time",
        "updated_time",
        "activated_time",
        "note",
    ):
        assert name in columns, name


def test_manifest_is_self_contained_not_null() -> None:
    """裁决一：快照必须落库，且不允许空快照。"""
    columns = SemanticRelease.__table__.columns

    assert columns["manifest_json"].nullable is False
    assert columns["manifest_hash"].nullable is False
    assert columns["schema_version"].nullable is False


def test_manifest_json_is_jsonb_on_postgres() -> None:
    """JSONB 才能让发布 diff 在库侧做键查询。"""
    col = SemanticRelease.__table__.columns["manifest_json"]

    assert isinstance(col.type.dialect_impl(postgresql.dialect()), postgresql.JSONB)


def test_manifest_hash_is_sha256_hex_width() -> None:
    assert SemanticRelease.__table__.columns["manifest_hash"].type.length == 64


def test_domain_code_is_nullable_fk_to_data_domain_restrict() -> None:
    """裁决四：NULL = 全局快照；非空时域必须存在，且域不可被删。"""
    col = SemanticRelease.__table__.columns["domain_code"]

    assert col.nullable is True

    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].target_fullname == "data_domain.code"
    assert fks[0].constraint.ondelete == "RESTRICT"


def test_status_defaults_to_draft() -> None:
    """ORM 侧 default 与迁移的 server_default 必须是同一个字面量。

    drift 工具只比列名，不比默认值 ⇒ 两侧分歧不会有任何信号，只能靠用例钉住。
    """
    col = SemanticRelease.__table__.columns["status"]

    assert col.nullable is False
    assert col.default is not None
    assert col.default.arg == SemanticReleaseStatus.DRAFT.value
    assert col.server_default is not None
    # server_default 可能是裸字符串或被引号包裹的文本，两种形态都接受，只认字面量。
    assert str(col.server_default.arg).strip("'") == SemanticReleaseStatus.DRAFT.value


def test_status_enum_covers_state_machine() -> None:
    """状态机四态必须齐全（DRAFT→VALIDATED→ACTIVE→ARCHIVED）。"""
    assert {s.value for s in SemanticReleaseStatus} == {
        "DRAFT",
        "VALIDATED",
        "ACTIVE",
        "ARCHIVED",
    }


def test_global_version_unique_index_declared() -> None:
    """全局快照（domain_code IS NULL）下版本号不得重复。"""
    indexes = {ix.name: ix for ix in SemanticRelease.__table__.indexes}
    ix = indexes["uq_semantic_release_global_version"]

    assert ix.unique is True
    assert [c.name for c in ix.columns] == ["version"]
    assert str(ix.dialect_options["postgresql"]["where"]) == "domain_code IS NULL"


def test_one_active_release_per_domain_enforced_by_index() -> None:
    """并发激活的数据库级兜底：同一域最多一条 ACTIVE。

    用 `COALESCE(domain_code, '')` 把 NULL 归一到同一取值，
    否则 PostgreSQL 唯一索引对 NULL 互不相等，全局快照会漏防。
    """
    indexes = {ix.name: ix for ix in SemanticRelease.__table__.indexes}
    ix = indexes["uq_semantic_release_one_active"]

    assert ix.unique is True
    assert str(ix.dialect_options["postgresql"]["where"]) == "status = 'ACTIVE'"
    # 表达式本身必须钉住：改成裸 domain_code 会让全局（NULL）作用域漏防，
    # 而上面的 unique/where 断言仍全绿 —— 这条是唯一的探针。
    assert "COALESCE" in str(ix.expressions[0]).upper()


def test_status_has_check_constraint_for_state_machine() -> None:
    """`status` 必须受库侧 CHECK 约束。

    部分唯一索引的谓词是字面量 `status = 'ACTIVE'`：若写入路径存进 `'active'`
    或 `'ACTIVE '`，索引会把它们当不同值 ⇒ 「同域最多一条 ACTIVE」的兜底静默失效。
    CHECK 让非法取值在库侧直接失败（服务层状态机 D10 仍保留，二者是双保险）。
    """
    checks = {
        c.name: str(c.sqltext)
        for c in SemanticRelease.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }

    assert "ck_semantic_release_status" in checks
    sqlText = checks["ck_semantic_release_status"]
    for status in ("DRAFT", "VALIDATED", "ACTIVE", "ARCHIVED"):
        assert status in sqlText, status


def test_manifest_shape_check_constraints_declared() -> None:
    """快照形状与哈希格式在 ORM 侧也必须声明（迁移 0142 与模型是两处）。

    集成用例（真实 PG）证明的是**库侧行为**；这里钉住 ORM 侧声明，
    否则 `alembic autogenerate` 会把这两条约束当成「DB 多余」提议删除。
    """
    checks = {
        c.name: str(c.sqltext)
        for c in SemanticRelease.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint"
    }

    assert "jsonb_typeof" in checks["ck_semantic_release_manifest_is_object"]
    assert "0-9a-f" in checks["ck_semantic_release_manifest_hash_sha256"]


def test_status_index_declared() -> None:
    """发布列表页 / 巡检按状态列举；索引漏声明会让 drift 报 extra_index。"""
    indexes = {ix.name: ix for ix in SemanticRelease.__table__.indexes}

    assert "ix_semantic_release_status" in indexes
    assert [c.name for c in indexes["ix_semantic_release_status"].columns] == ["status"]


def test_model_is_registered_in_base_metadata() -> None:
    """drift CLI 只 import `app.domain.models`，漏注册就会被误报 extra_table。"""
    assert "semantic_release" in Base.metadata.tables
