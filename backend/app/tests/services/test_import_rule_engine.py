import pytest

from app.domain.enums import DataType
from app.domain.exceptions import ValidationError
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


def test_filter_tables_include_temp_tables_keeps_tmp():
    engine = ImportRuleEngine()
    rules = TableFilterRules(include_temp_tables=True)
    tables = [
        TableSchemaRead(table_name="tmp_inventory", columns=[]),
        TableSchemaRead(table_name="wms_operation_log", columns=[]),
    ]
    result = engine.filter_tables(tables, rules)
    names = {t.table_name for t in result}
    assert names == {"tmp_inventory"}


def test_filter_tables_custom_blacklist_pattern():
    engine = ImportRuleEngine()
    rules = TableFilterRules(name_blacklist_patterns=[r"^audit_"])
    tables = [
        TableSchemaRead(table_name="audit_trail", columns=[]),
        TableSchemaRead(table_name="wms_inventory", columns=[]),
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


def test_map_data_type_unknown_type_falls_back_to_string():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("SDO_GEOMETRY", rules) == DataType.STRING


def test_map_data_type_tinyint1_maps_to_boolean():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("TINYINT(1)", rules) == DataType.BOOLEAN


def test_map_data_type_number_non_integer_maps_to_decimal():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("NUMBER(10,2)", rules) == DataType.DECIMAL
    assert engine.map_data_type("NUMBER", rules) == DataType.DECIMAL


def test_map_data_type_invalid_mapping_value_raises_validation_error():
    engine = ImportRuleEngine()
    rules = TypeMappingRules(mappings={"VARCHAR": "INVALID"})
    with pytest.raises(ValidationError):
        engine.map_data_type("VARCHAR", rules)


def test_map_data_type_is_case_insensitive_and_trims():
    engine = ImportRuleEngine()
    rules = TypeMappingRules()
    assert engine.map_data_type("  varchar2 ", rules) == DataType.STRING
