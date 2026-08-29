import pytest

from app.domain.enums import DataType
from app.domain.schemas import TableFilterRules, TableSchemaRead, TypeMappingRules
from app.services.import_rule_engine import ImportRuleEngine


def test_filter_tables_excludes_log_and_temp():
    engine = ImportRuleEngine()
    rules = TableFilterRules()
    tables = [
        TableSchemaRead(table_name="wms_inventory", columns=[]),
        TableSchemaRead(table_name="wms_operation_log", columns=[]),
        TableSchemaRead(table_name="tmp_inventory", columns=[]),
    ]
    result = engine.filter_tables(tables, rules)
    names = {t.table_name for t in result}
    assert names == {"wms_inventory"}


def test_map_data_type_oracle_number_integer():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("NUMBER(p=0,s=0)", rules) == DataType.INT


def test_map_data_type_varchar_to_string():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("VARCHAR", rules) == DataType.STRING
