"""LLM schema 语义增强服务。"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.domain.schemas import CamelModel, ColumnSchemaRead, TableSchemaRead

logger = logging.getLogger(__name__)


class EnhancedColumn(CamelModel):
    name: str
    alias: str | None = None
    description: str | None = None
    enum_values: list[str] | None = None


class EnhancedTable(CamelModel):
    name: str
    alias: str | None = None
    description: str | None = None
    columns: list[EnhancedColumn]


class FilterSuggestions(CamelModel):
    exclude_patterns: list[str] = []
    exclude_tables: list[str] = []


class EnhancedSchemaResult(CamelModel):
    tables: list[EnhancedTable]
    filter_suggestions: FilterSuggestions


class ImportLlmEnhancer:
    def __init__(self, llm_client=None) -> None:
        self._llm_client = llm_client

    async def enhance_schema(
        self,
        tables: list[TableSchemaRead],
        *,
        generate_aliases: bool = True,
        generate_descriptions: bool = True,
        detect_enums: bool = True,
        suggest_filters: bool = True,
    ) -> EnhancedSchemaResult:
        if not tables or not generate_aliases and not generate_descriptions and not detect_enums and not suggest_filters:
            return self._empty_result(tables)

        try:
            raw = await self._call_llm(tables)
            data = json.loads(raw)
            return self._parse(data, tables)
        except Exception as exc:
            logger.warning("LLM schema 增强失败，降级为无增强: %s", exc)
            return self._empty_result(tables)

    async def _call_llm(self, tables: list[TableSchemaRead]) -> str:
        if self._llm_client is None:
            raise RuntimeError("llm_client not provided")
        prompt = self._build_prompt(tables)
        return await self._llm_client.complete(prompt)

    @staticmethod
    def _build_prompt(tables: list[TableSchemaRead]) -> list[dict[str, Any]]:
        schema = [
            {
                "name": t.table_name,
                "columns": [
                    {"name": c.column_name, "data_type": c.data_type}
                    for c in t.columns
                ],
            }
            for t in tables
        ]
        system = (
            "你是一个数据库 schema 语义理解助手。根据提供的表结构元数据："
            "1. 为每个表生成简短中文别名（2-6 字）和一句话业务描述。"
            "2. 为每个列生成中文别名（1-4 字）和简短描述。"
            "3. 识别状态/枚举字段，列出其可能取值（如无则不填）。"
            "4. 建议应该排除的日志表、临时表、系统表。"
            "输出必须是严格 JSON，不要任何额外解释。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"tables": schema}, ensure_ascii=False)},
        ]

    def _parse(self, data: dict[str, Any], original: list[TableSchemaRead]) -> EnhancedSchemaResult:
        enhanced_by_name = {t["name"]: t for t in data.get("tables", [])}
        result_tables: list[EnhancedTable] = []
        for t in original:
            enhanced = enhanced_by_name.get(t.table_name, {})
            columns = []
            enhanced_cols = {c["name"]: c for c in enhanced.get("columns", [])}
            for c in t.columns:
                ec = enhanced_cols.get(c.column_name, {})
                columns.append(
                    EnhancedColumn(
                        name=c.column_name,
                        alias=ec.get("alias"),
                        description=ec.get("description"),
                        enum_values=ec.get("enum_values"),
                    )
                )
            result_tables.append(
                EnhancedTable(
                    name=t.table_name,
                    alias=enhanced.get("alias"),
                    description=enhanced.get("description"),
                    columns=columns,
                )
            )
        fs = data.get("filter_suggestions", {})
        return EnhancedSchemaResult(
            tables=result_tables,
            filter_suggestions=FilterSuggestions(
                exclude_patterns=fs.get("exclude_patterns", []),
                exclude_tables=fs.get("exclude_tables", []),
            ),
        )

    def _empty_result(self, tables: list[TableSchemaRead]) -> EnhancedSchemaResult:
        return EnhancedSchemaResult(
            tables=[
                EnhancedTable(
                    name=t.table_name,
                    columns=[EnhancedColumn(name=c.column_name) for c in t.columns],
                )
                for t in tables
            ],
            filter_suggestions=FilterSuggestions(),
        )
