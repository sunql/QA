import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from app.domain.schemas import (
    AgentToolConfigCreate,
    AgentToolConfigRead,
    AgentToolConfigUpdate,
    _UnsetType,
)


class TestAgentToolConfigCreate:
    def test_minimal_valid(self):
        dto = AgentToolConfigCreate(
            name="supplier_360",
            data_object="supplier",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
        )
        assert dto.name == "supplier_360"
        assert dto.data_object == "SUPPLIER"  # normalized
        assert dto.handler_kind.value == "BUILTIN"
        assert dto.data_layers == []
        assert dto.input_schema == {}
        assert dto.arg_extractor_kind == "supplier_key"

    def test_invalid_name_pattern_rejected(self):
        with pytest.raises(ValidationError):
            AgentToolConfigCreate(
                name="Supplier-360",  # uppercase + hyphen
                data_object="SUPPLIER",
                handler_kind="BUILTIN",
                handler_ref="supplier_360",
            )

    def test_empty_data_object_rejected(self):
        with pytest.raises(ValidationError, match="data_object 不能为空"):
            AgentToolConfigCreate(
                name="foo",
                data_object="   ",
                handler_kind="BUILTIN",
                handler_ref="foo",
            )

    def test_full_payload(self):
        dto = AgentToolConfigCreate(
            name="nl2sql_query",
            description="test",
            data_object="  order  ",
            data_layers=["DIM", "FEATURE"],
            input_schema={"type": "object"},
            handler_kind="NL2SQL",
            handler_ref="nl2sql_default",
            arg_extractor_kind="supplier_key",
        )
        assert dto.data_object == "ORDER"
        assert dto.data_layers == ["DIM", "FEATURE"]

    def test_invalid_handler_kind_rejected(self):
        with pytest.raises(ValidationError):
            AgentToolConfigCreate(
                name="foo",
                data_object="SUPPLIER",
                handler_kind="EXTERNAL_HTTP",  # not in enum
                handler_ref="foo",
            )


class TestAgentToolConfigUpdate:
    def test_no_name_field(self):
        # name 不可改 — Update DTO 不应有此字段
        fields = set(AgentToolConfigUpdate.model_fields.keys())
        assert "name" not in fields
        assert "version" in fields  # required for optimistic lock

    def test_unset_defaults(self):
        dto = AgentToolConfigUpdate(version=1)
        for f in (
            "description", "data_object", "data_layers", "input_schema",
            "handler_kind", "handler_ref", "arg_extractor_kind", "enabled",
        ):
            assert isinstance(getattr(dto, f), _UnsetType), f"{f} should default to UNSET"

    def test_version_required(self):
        with pytest.raises(ValidationError):
            AgentToolConfigUpdate()

    def test_explicit_null_description(self):
        # description 允许显式置空（nullable 字段）
        dto = AgentToolConfigUpdate(version=1, description=None)
        assert dto.description is None

    def test_data_object_normalized_when_provided(self):
        dto = AgentToolConfigUpdate(version=1, data_object="  vendor  ")
        assert dto.data_object == "VENDOR"


class TestAgentToolConfigRead:
    def test_includes_audit_fields(self):
        dto = AgentToolConfigRead(
            id=1,
            name="supplier_360",
            data_object="SUPPLIER",
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
            version=3,
            enabled=True,
            created_time=datetime.now(timezone.utc),
            updated_time=None,
        )
        assert dto.id == 1
        assert dto.version == 3