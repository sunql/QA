import pytest

from app.domain.schemas import ColumnSchemaRead, TableSchemaRead
from app.services.import_llm_enhancer import ImportLlmEnhancer


class FakeLlmClient:
    async def complete(self, messages, response_format=None):
        return """
        {
          "tables": [
            {
              "name": "wms_inventory",
              "alias": "库存",
              "description": "仓库物料库存主数据",
              "columns": [
                {"name": "quantity", "alias": "数量", "description": "库存数量", "enum_values": null}
              ]
            }
          ],
          "filter_suggestions": {"exclude_patterns": ["_log$"], "exclude_tables": []}
        }
        """


@pytest.mark.asyncio
async def test_enhance_schema_returns_aliases_and_descriptions():
    enhancer = ImportLlmEnhancer(llm_client=FakeLlmClient())
    tables = [
        TableSchemaRead(
            table_name="wms_inventory",
            columns=[ColumnSchemaRead(column_name="quantity", data_type="DECIMAL", nullable=True)],
        )
    ]
    result = await enhancer.enhance_schema(tables)
    assert result.tables[0].alias == "库存"
    assert result.tables[0].columns[0].alias == "数量"


@pytest.mark.asyncio
async def test_enhance_schema_degrades_on_invalid_json():
    class BadClient:
        async def complete(self, messages, response_format=None):
            return "not json"

    enhancer = ImportLlmEnhancer(llm_client=BadClient())
    tables = [
        TableSchemaRead(
            table_name="wms_inventory",
            columns=[ColumnSchemaRead(column_name="quantity", data_type="DECIMAL", nullable=True)],
        )
    ]
    result = await enhancer.enhance_schema(tables)
    assert result.tables[0].alias is None
    assert result.filter_suggestions.exclude_patterns == []
