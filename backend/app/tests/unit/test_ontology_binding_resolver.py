"""运行时本体绑定解析器（单测）：类→物理表 / 属性→物理列 / fail-closed。"""

from __future__ import annotations

import pytest

from app.domain.exceptions import ValidationError
from app.services.ontology_binding_resolver import (
    BindingResolutionError,
    ResolvedBinding,
)


def _binding(**overrides) -> ResolvedBinding:
    base = {
        "className": "DWD_PURCHASE_ORDER_DTL",
        "datasourceId": 1,
        "schemaName": "THBI",
        "sourceTable": "DWD_PURCHASE_ORDER_DTL",
        "columns": {"PO_NO": "PO_NO", "PO_QTY": "PO_QTY"},
    }
    base.update(overrides)
    return ResolvedBinding(**base)


def test_qualified_table_joins_schema_and_source_table() -> None:
    assert _binding().qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"


def test_column_returns_physical_column_for_known_property() -> None:
    assert _binding().column("PO_QTY") == "PO_QTY"


def test_column_raises_with_missing_property_name() -> None:
    with pytest.raises(BindingResolutionError) as exc:
        _binding().column("RECEIVED_QTY")
    assert "RECEIVED_QTY" in str(exc.value)


def test_binding_resolution_error_is_validation_error() -> None:
    """上层按 ValidationError 统一转 422 ⇒ 解析失败须是它的子类。"""
    assert issubclass(BindingResolutionError, ValidationError)


def test_compile_binding_rejects_candidate_rows() -> None:
    """候选绑定（is_candidate=True）不得进读路径。"""
    from app.services import ontology_binding_resolver as mod

    candidateRow = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", True, None),
        (("pid", "PO_NO", "PO_NO", "cid"),),
    )
    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            rows=candidateRow,
        )
    assert "绑定" in str(exc.value)


def test_compile_binding_maps_properties_to_columns() -> None:
    from app.services import ontology_binding_resolver as mod

    rows = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", False, None),
        (("pid", "PO_NO", "PO_NO", "cid"), ("pid", "PO_QTY", "PO_QTY", "cid")),
    )
    resolved = mod._compileBinding(
        className="DWD_PURCHASE_ORDER_DTL", datasourceId=1, rows=rows
    )
    assert resolved.qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"
    assert resolved.columns == {"PO_NO": "PO_NO", "PO_QTY": "PO_QTY"}


def test_compile_binding_rejects_missing_class() -> None:
    """类不存在 ⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(className="NO_SUCH_CLASS_XYZ", datasourceId=1, rows=())
    assert "本体类不存在" in str(exc.value)


def test_compile_binding_rejects_tombstoned_class() -> None:
    """墓碑类（valid_to 非空）⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    rows = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", "2026-11-25 00:00:00+00:00"),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", False, None),
    )
    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL", datasourceId=1, rows=rows
        )
    assert "墓碑" in str(exc.value)


def test_compile_binding_rejects_multiple_active_bindings() -> None:
    """同一数据源多条生效绑定 ⇒ 口径不唯一 ⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    rows = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", False, None),
        ("sid2", 1, "THBI", "DWD_PURCHASE_ORDER_DTL_BAK", False, None),
        (("pid", "PO_NO", "PO_NO", "cid"),),
    )
    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL", datasourceId=1, rows=rows
        )
    assert "口径不唯一" in str(exc.value)


def test_compile_binding_rejects_non_thbi_schema() -> None:
    """硬规则：绑定 schema 必须是 THBI。"""
    from app.services import ontology_binding_resolver as mod

    rows = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "ZJTH", "DWD_PURCHASE_ORDER_DTL", False, None),
    )
    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL", datasourceId=1, rows=rows
        )
    assert "THBI" in str(exc.value)


def test_compile_binding_rejects_empty_physical_column() -> None:
    """物理列缺失 ⇒ fail-closed（不回退到本体表的 source_column）。"""
    from app.services import ontology_binding_resolver as mod

    rows = (
        ("cid", "DWD_PURCHASE_ORDER_DTL", None),
        ("sid", 1, "THBI", "DWD_PURCHASE_ORDER_DTL", False, None),
        (("pid", "PO_NO", "", "cid"),),
    )
    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL", datasourceId=1, rows=rows
        )
    assert "PO_NO" in str(exc.value)
