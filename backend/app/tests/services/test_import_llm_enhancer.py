import pytest

from app.domain.schemas import ColumnSchemaRead, TableSchemaRead
from app.infrastructure.llm.base_client import LlmMessage, LlmResponse
from app.services.import_llm_enhancer import ImportLlmEnhancer


def _llm_response(content: str) -> LlmResponse:
    return LlmResponse(
        content=content,
        modelName="fake-model",
        promptTokens=0,
        completionTokens=0,
        totalTokens=0,
    )


class FakeLlmClient:
    async def complete(self, messages, **kwargs):
        assert all(isinstance(m, LlmMessage) for m in messages)
        return _llm_response(
            """
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
        )


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
        async def complete(self, messages, **kwargs):
            return _llm_response("not json")

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


@pytest.mark.asyncio
async def test_enhance_schema_degrades_without_llm_client():
    enhancer = ImportLlmEnhancer(llm_client=None)
    tables = [
        TableSchemaRead(
            table_name="wms_inventory",
            columns=[ColumnSchemaRead(column_name="quantity", data_type="DECIMAL", nullable=True)],
        )
    ]
    result = await enhancer.enhance_schema(tables)
    assert result.tables[0].alias is None
    assert result.tables[0].columns[0].name == "quantity"
    assert result.filter_suggestions.exclude_tables == []
