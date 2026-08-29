"""导入规则引擎：表过滤 + 数据类型映射。

纯函数式规则求值：
- filter_tables 返回新列表，不修改输入；
- map_data_type 仅依据 rules 与 db_type 计算结果。
"""

from __future__ import annotations

import re

from app.domain.enums import DataType
from app.domain.exceptions import ValidationError
from app.domain.schemas import TableFilterRules, TableSchemaRead, TypeMappingRules

# 临时表名前缀（大小写不敏感）。
_TEMP_TABLE_PREFIXES: tuple[str, ...] = ("tmp_", "temp_", "#")

# MySQL 布尔字面类型。
_TINYINT_BOOLEAN = "TINYINT(1)"

# Oracle 数值类型的整数特例标记（规范化 key）。
_ORACLE_INTEGER_KEY = "NUMBER(p=0,s=0)"

# 未匹配到映射时的兜底类型。
_FALLBACK_TYPE = DataType.STRING


class ImportRuleEngine:
    """依据 ImportRuleConfig 对 introspect 结果做过滤与类型归一。"""

    def filter_tables(
        self,
        tables: list[TableSchemaRead],
        rules: TableFilterRules,
    ) -> list[TableSchemaRead]:
        """返回通过过滤规则的表（新列表，输入不变）。"""
        return [t for t in tables if self._is_allowed(t, rules)]

    def _is_allowed(self, table: TableSchemaRead, rules: TableFilterRules) -> bool:
        name = table.table_name
        if rules.include_temp_tables is False and self._is_temp(name):
            return False
        if any(re.search(p, name, re.IGNORECASE) for p in rules.name_blacklist_patterns):
            return False
        return True

    @staticmethod
    def _is_temp(name: str) -> bool:
        lowered = name.lower()
        return lowered.startswith(_TEMP_TABLE_PREFIXES)

    def map_data_type(self, db_type: str, rules: TypeMappingRules) -> DataType:
        """将数据库原生类型映射为 DataType；未知类型兜底为 STRING。"""
        normalized = self._normalize_type(db_type)
        value = rules.mappings.get(normalized)
        if value is None:
            return _FALLBACK_TYPE
        try:
            return DataType(value)
        except ValueError as exc:
            raise ValidationError(
                f"类型映射值非法: {value!r}（db_type={db_type!r}, normalized={normalized!r}）"
            ) from exc

    @staticmethod
    def _normalize_type(db_type: str) -> str:
        upper = db_type.upper().strip()
        if upper == _TINYINT_BOOLEAN:
            return "BOOLEAN"
        if upper.startswith("NUMBER"):
            if "P=0" in upper and "S=0" in upper:
                return _ORACLE_INTEGER_KEY
            return "NUMBER"
        return upper.split("(")[0]
