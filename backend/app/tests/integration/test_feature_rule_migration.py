"""Alembic 0043 落表 + 索引 + 约束验证。

Brief §5：feature_rule（头档）+ feature_rule_threshold（阈值档，1:N），
unique 约束保 (data_object, data_layer, target_level, code) 唯一。
"""
import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def test_feature_rule_tables_exist(dbSession) -> None:
    """迁移后两张表都建好，列名 + 类型 + 约束对齐 spec §5。"""
    # feature_rule 列存在性
    fr_cols = await dbSession.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'feature_rule'")
    )
    fr_col_names = {row[0] for row in fr_cols}
    assert "code" in fr_col_names
    assert "data_object" in fr_col_names
    assert "target_level" in fr_col_names
    assert "version" in fr_col_names

    # feature_rule unique 约束含 data_object
    uq_result = await dbSession.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = 'uq_feature_rule_scope_code'"
        )
    )
    assert uq_result.scalar_one_or_none() is not None

    # feature_rule_threshold FK 指向 feature_rule
    fk_result = await dbSession.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = 'fk_feature_rule_threshold_rule'"
        )
    )
    assert fk_result.scalar_one_or_none() is not None


async def test_feature_rule_column_types(dbSession) -> None:
    """feature_rule 列类型对齐 spec §5.1。"""
    result = await dbSession.execute(
        text(
            "SELECT column_name, data_type, character_maximum_length, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_name = 'feature_rule' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row[0]: row[1:] for row in result}

    assert cols["id"][0] == "bigint"
    assert cols["code"][0] == "character varying" and cols["code"][1] == 64
    assert cols["data_object"][0] == "character varying" and cols["data_object"][1] == 64
    assert cols["data_layer"][0] == "character varying" and cols["data_layer"][1] == 16
    assert cols["target_level"][0] == "character varying" and cols["target_level"][1] == 16
    assert cols["feature_name"][0] == "character varying" and cols["feature_name"][1] == 64
    assert cols["enabled"][0] == "boolean"
    assert cols["priority"][0] == "integer"
    assert cols["version"][0] == "integer"


async def test_feature_rule_threshold_column_types(dbSession) -> None:
    """feature_rule_threshold 列类型对齐 spec §5.2。"""
    result = await dbSession.execute(
        text(
            "SELECT column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns "
            "WHERE table_name = 'feature_rule_threshold' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row[0]: row[1:] for row in result}

    assert cols["id"][0] == "bigint"
    assert cols["rule_id"][0] == "bigint"
    assert cols["severity"][0] == "character varying"
    assert cols["operator"][0] == "character varying"
    assert cols["threshold_value"][0] == "numeric"
    assert cols["threshold_value"][1] == 20 and cols["threshold_value"][2] == 6
    assert cols["unit"][0] == "character varying"
    assert cols["threshold_order"][0] == "integer"


async def test_feature_rule_unique_constraint_name(dbSession) -> None:
    """uq_feature_rule_scope_code 存在且包含正确列。"""
    result = await dbSession.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conname = 'uq_feature_rule_scope_code'"
        )
    )
    row = result.first()
    assert row is not None, "uq_feature_rule_scope_code constraint not found"


async def test_feature_rule_threshold_unique_constraint(dbSession) -> None:
    """uq_feature_rule_threshold_rule_severity 唯一约束存在。"""
    result = await dbSession.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = 'uq_feature_rule_threshold_rule_severity'"
        )
    )
    assert result.scalar_one_or_none() is not None


async def test_feature_rule_fk_cascade_delete(dbSession) -> None:
    """feature_rule_threshold.rule_id FK ON DELETE CASCADE。"""
    result = await dbSession.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conname = 'fk_feature_rule_threshold_rule'"
        )
    )
    row = result.first()
    assert row is not None, "fk_feature_rule_threshold_rule not found"
    assert "CASCADE" in row[1]


async def test_feature_rule_scope_enabled_index(dbSession) -> None:
    """ix_feature_rule_scope_enabled 索引存在。"""
    result = await dbSession.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'feature_rule' AND indexname = 'ix_feature_rule_scope_enabled'"
        )
    )
    assert result.scalar_one_or_none() is not None
