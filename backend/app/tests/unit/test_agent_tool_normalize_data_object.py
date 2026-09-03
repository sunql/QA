import pytest
from app.domain.enums import AgentToolHandlerKind
from app.domain.schemas import _normalizeDataObject


class TestAgentToolHandlerKind:
    def test_builtin_value(self):
        assert AgentToolHandlerKind.BUILTIN.value == "BUILTIN"

    def test_nl2sql_value(self):
        assert AgentToolHandlerKind.NL2SQL.value == "NL2SQL"

    def test_inherits_str(self):
        # str Enum 兼容 JSON 序列化与 ORM 写入
        assert isinstance(AgentToolHandlerKind.BUILTIN, str)


class TestNormalizeDataObject:
    def test_strip_and_upper(self):
        assert _normalizeDataObject("  supplier  ") == "SUPPLIER"

    def test_already_upper_unchanged(self):
        assert _normalizeDataObject("SUPPLIER") == "SUPPLIER"

    def test_lowercase_to_upper(self):
        assert _normalizeDataObject("supplier") == "SUPPLIER"

    def test_none_raises(self):
        with pytest.raises(ValueError, match="data_object 不能为空"):
            _normalizeDataObject(None)

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="data_object 不能为空"):
            _normalizeDataObject("   ")