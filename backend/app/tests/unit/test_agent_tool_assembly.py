import pytest
from app.services.agent_tools import (
    AgentToolAssembly,
    BUILTIN_HANDLERS,
    NL2SQL_HANDLERS,
    ARG_EXTRACTORS,
    _VALID_HANDLER_REFS,
)


class _FakeRow:
    """最小 AgentToolConfig 替身（避开 DB fixture；assembly 只需属性读取）。"""
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestAgentToolAssembly:
    def test_assemble_builtin_supplier_360(self):
        row = _FakeRow(
            name="supplier_360",
            description="查询单供应商 360° 视图",
            data_object="SUPPLIER",
            data_layers=["DIM", "FEATURE"],
            input_schema={"type": "object", "required": ["key"]},
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
            arg_extractor_kind="supplier_key",
        )
        tool = AgentToolAssembly.assemble(row)
        assert tool.name == "supplier_360"
        assert tool.data_object == "SUPPLIER"
        assert tool.data_layers == ("DIM", "FEATURE")
        assert tool.handler is BUILTIN_HANDLERS["supplier_360"]

    def test_assemble_nl2sql(self):
        row = _FakeRow(
            name="nl2sql_q",
            description="test",
            data_object="ORDER",
            data_layers=["DIM"],
            input_schema={},
            handler_kind="NL2SQL",
            handler_ref="nl2sql_default",
            arg_extractor_kind="supplier_key",
        )
        tool = AgentToolAssembly.assemble(row)
        assert tool.handler is NL2SQL_HANDLERS["nl2sql_default"]

    def test_data_object_normalized(self):
        row = _FakeRow(
            name="x", description="x", data_object="  supplier  ",
            data_layers=[], input_schema={},
            handler_kind="BUILTIN", handler_ref="supplier_360",
            arg_extractor_kind="supplier_key",
        )
        tool = AgentToolAssembly.assemble(row)
        assert tool.data_object == "SUPPLIER"

    def test_data_layers_uppercased(self):
        row = _FakeRow(
            name="x", description="x", data_object="SUPPLIER",
            data_layers=["dim", "Feature"], input_schema={},
            handler_kind="BUILTIN", handler_ref="supplier_360",
            arg_extractor_kind="supplier_key",
        )
        tool = AgentToolAssembly.assemble(row)
        assert tool.data_layers == ("DIM", "FEATURE")

    def test_invalid_handler_kind_raises(self):
        row = _FakeRow(
            name="bad", description="x", data_object="SUPPLIER",
            data_layers=[], input_schema={},
            handler_kind="EXTERNAL_HTTP",  # not in CHECK whitelist
            handler_ref="x", arg_extractor_kind="supplier_key",
        )
        with pytest.raises(ValueError, match="handler_kind"):
            AgentToolAssembly.assemble(row)

    def test_invalid_handler_ref_raises(self):
        row = _FakeRow(
            name="bad", description="x", data_object="SUPPLIER",
            data_layers=[], input_schema={},
            handler_kind="BUILTIN",
            handler_ref="nonexistent_handler",
            arg_extractor_kind="supplier_key",
        )
        with pytest.raises(ValueError, match="handler_ref"):
            AgentToolAssembly.assemble(row)

    def test_handler_kind_ref_mismatch_raises(self):
        # NL2SQL ref 配 BUILTIN kind
        row = _FakeRow(
            name="bad", description="x", data_object="SUPPLIER",
            data_layers=[], input_schema={},
            handler_kind="BUILTIN",
            handler_ref="nl2sql_default",
            arg_extractor_kind="supplier_key",
        )
        with pytest.raises(ValueError, match="handler_ref"):
            AgentToolAssembly.assemble(row)

    def test_invalid_arg_extractor_raises(self):
        row = _FakeRow(
            name="bad", description="x", data_object="SUPPLIER",
            data_layers=[], input_schema={},
            handler_kind="BUILTIN",
            handler_ref="supplier_360",
            arg_extractor_kind="nonexistent_extractor",
        )
        with pytest.raises(ValueError, match="arg_extractor_kind"):
            AgentToolAssembly.assemble(row)


class TestHandlerRegistries:
    def test_builtin_has_expected_keys(self):
        """供应链 3 个 + feat-wiki-knowledge M8 的 4 个知识工具。"""
        assert set(BUILTIN_HANDLERS.keys()) == {
            "supplier_360", "supplier_risk", "graph_traverse",
            "wiki_search", "wiki_read", "rule_evaluate", "coverage_status",
        }

    def test_nl2sql_has_default(self):
        assert "nl2sql_default" in NL2SQL_HANDLERS

    def test_arg_extractors_expected_keys(self):
        assert set(ARG_EXTRACTORS.keys()) == {
            "supplier_key", "supplier_risk_key", "supplier_graph_key",
            "wiki_text", "wiki_no_args",
        }

    def test_valid_handler_refs_mapping(self):
        assert _VALID_HANDLER_REFS["BUILTIN"] == frozenset(BUILTIN_HANDLERS.keys())
        assert _VALID_HANDLER_REFS["NL2SQL"] == frozenset(NL2SQL_HANDLERS.keys())