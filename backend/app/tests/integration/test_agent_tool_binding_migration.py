"""验证 Alembic 0035 + AgentDefinition.tool_name 字段就位。"""
from sqlalchemy import text
import pytest

pytestmark = pytest.mark.asyncio


async def test_agent_definition_has_tool_name_column(dbSession) -> None:
    result = await dbSession.execute(
        text(
            "SELECT column_name, data_type, is_nullable, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_name = 'agent_definition' "
            "  AND column_name IN ('tool_name', 'tool_name_updated_at')"
        )
    )
    cols = {row[0]: row for row in result}
    assert "tool_name" in cols
    assert cols["tool_name"][1] == "character varying"
    assert cols["tool_name"][2] == "YES"
    assert cols["tool_name"][3] == 64
    assert "tool_name_updated_at" in cols
    assert cols["tool_name_updated_at"][2] == "YES"
