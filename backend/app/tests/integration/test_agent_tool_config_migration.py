"""验证 Alembic 0037 创建 agent_tool_config 表（含 unique + CHECK 约束 + 索引）。

运行前置：TEST_DATABASE_URL 指向 qa_metadata_test（port 5433），
dbSession fixture 会负责执行 migration。
"""
from sqlalchemy import text
import pytest

pytestmark = pytest.mark.asyncio


async def test_agent_tool_config_table_exists_with_columns(dbSession) -> None:
    result = await dbSession.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_name = 'agent_tool_config' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row[0]: (row[1], row[2]) for row in result}
    assert "id" in cols
    assert "name" in cols and cols["name"] == ("character varying", "NO")
    assert "handler_kind" in cols
    assert "data_object" in cols
    assert "data_layers" in cols
    assert "input_schema" in cols
    assert "handler_ref" in cols
    assert "arg_extractor_kind" in cols
    assert "enabled" in cols
    assert "version" in cols
    assert "created_time" in cols
    assert "updated_time" in cols


async def test_agent_tool_config_unique_on_name(dbSession) -> None:
    result = await dbSession.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conname = 'uq_agent_tool_config_name'"
        )
    )
    assert result.scalar_one_or_none() is not None


async def test_agent_tool_config_check_handler_kind(dbSession) -> None:
    result = await dbSession.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint "
            "WHERE conname = 'ck_agent_tool_config_handler_kind'"
        )
    )
    row = result.first()
    assert row is not None
    assert "BUILTIN" in row[1] and "NL2SQL" in row[1]


async def test_agent_tool_config_indexes_exist(dbSession) -> None:
    result = await dbSession.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'agent_tool_config' "
            "  AND indexname IN ("
            "    'ix_agent_tool_config_enabled',"
            "    'ix_agent_tool_config_data_object'"
            "  )"
        )
    )
    names = {row[0] for row in result}
    assert names == {
        "ix_agent_tool_config_enabled",
        "ix_agent_tool_config_data_object",
    }


async def test_agent_tool_config_default_values(dbSession) -> None:
    """enabled 默认 true；version 默认 1；data_layers 默认 []；input_schema 默认 {}。"""
    await dbSession.execute(
        text(
            "INSERT INTO agent_tool_config "
            "(name, data_object, handler_kind, handler_ref) "
            "VALUES ('_migration_default_test', 'SUPPLIER', 'BUILTIN', 'supplier_360')"
        )
    )
    await dbSession.commit()
    row = (
        await dbSession.execute(
            text(
                "SELECT enabled, version, data_layers, input_schema "
                "FROM agent_tool_config WHERE name = '_migration_default_test'"
            )
        )
    ).first()
    assert row[0] is True  # enabled
    assert row[1] == 1     # version
    assert row[2] == []    # data_layers
    assert row[3] == {}    # input_schema
    # 清理
    await dbSession.execute(
        text("DELETE FROM agent_tool_config WHERE name = '_migration_default_test'")
    )
    await dbSession.commit()