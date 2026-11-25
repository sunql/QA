"""运行时本体绑定解析器（单测）：类→物理表 / 属性→物理列 / fail-closed。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.exceptions import ValidationError
from app.services.ontology_binding_resolver import (
    BindingResolutionError,
    ResolvedBinding,
)

#: 编译层的时间基准（注入 ⇒ 可构造「未来才生效」的绑定行）。
_NOW = datetime(2026, 11, 25, tzinfo=UTC)
_PAST = _NOW - timedelta(days=1)
_FUTURE = _NOW + timedelta(days=1)


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


def _classRow(**overrides):
    from app.services import ontology_binding_resolver as mod

    base = {"classId": "cid", "className": "DWD_PURCHASE_ORDER_DTL", "validTo": None}
    base.update(overrides)
    return mod.ClassRow(**base)


def _sourceRow(**overrides):
    from app.services import ontology_binding_resolver as mod

    base = {
        "sourceBindingId": "sid",
        "datasourceId": 1,
        "schemaName": "THBI",
        "sourceTable": "DWD_PURCHASE_ORDER_DTL",
        "bindingRole": "BASE",
        "isCandidate": False,
        "validTo": None,
        "validFrom": _PAST,
    }
    base.update(overrides)
    return mod.SourceBindingRow(**base)


def _propertyRow(**overrides):
    from app.services import ontology_binding_resolver as mod

    base = {
        "propertyId": "pid",
        "propertyName": "PO_NO",
        "sourceColumn": "PO_NO",
        "classId": "cid",
        "sourceBindingId": "sid",
        "validFrom": _PAST,
    }
    base.update(overrides)
    return mod.PropertyBindingRow(**base)


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


def test_columns_mapping_is_read_only() -> None:
    """不可变：列映射是只读视图，调用方改不动（全局「不原地改对象」）。"""
    binding = _binding()
    with pytest.raises(TypeError):
        binding.columns["PO_QTY"] = "X"  # type: ignore[index]


def test_compile_binding_rejects_candidate_rows() -> None:
    """候选绑定（is_candidate=True）不得进读路径。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(),
            sourceRows=(_sourceRow(isCandidate=True),),
            propertyRows=(_propertyRow(),),
            now=_NOW,
        )
    assert "绑定" in str(exc.value)


def test_compile_binding_maps_properties_to_columns() -> None:
    from app.services import ontology_binding_resolver as mod

    resolved = mod._compileBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        classRow=_classRow(),
        sourceRows=(_sourceRow(),),
        propertyRows=(
            _propertyRow(propertyName="PO_NO", sourceColumn="PO_NO"),
            _propertyRow(propertyName="PO_QTY", sourceColumn="PO_QTY"),
        ),
        now=_NOW,
    )
    assert resolved.qualifiedTable() == "THBI.DWD_PURCHASE_ORDER_DTL"
    assert resolved.columns == {"PO_NO": "PO_NO", "PO_QTY": "PO_QTY"}


def test_compile_binding_rejects_missing_class() -> None:
    """类不存在 ⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="NO_SUCH_CLASS_XYZ",
            datasourceId=1,
            classRow=None,
            sourceRows=(),
            propertyRows=(),
            now=_NOW,
        )
    assert "本体类不存在" in str(exc.value)


def test_compile_binding_rejects_tombstoned_class() -> None:
    """墓碑类（valid_to 非空）⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(validTo=_NOW),
            sourceRows=(_sourceRow(),),
            propertyRows=(),
            now=_NOW,
        )
    assert "墓碑" in str(exc.value)


def test_compile_binding_rejects_multiple_active_bindings() -> None:
    """同一数据源同一角色多条生效绑定 ⇒ 口径不唯一 ⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(),
            sourceRows=(_sourceRow(), _sourceRow(sourceBindingId="sid2")),
            propertyRows=(),
            now=_NOW,
        )
    assert "口径不唯一" in str(exc.value)


def test_compile_binding_ignores_non_base_role() -> None:
    """多角色合法：FILTER / NUMERATOR 等角色不是本读路径的口径，不参与唯一性。"""
    from app.services import ontology_binding_resolver as mod

    resolved = mod._compileBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        classRow=_classRow(),
        sourceRows=(
            _sourceRow(),
            _sourceRow(sourceBindingId="sid2", bindingRole="FILTER"),
        ),
        propertyRows=(_propertyRow(),),
        now=_NOW,
    )
    assert resolved.sourceTable == "DWD_PURCHASE_ORDER_DTL"


def test_compile_binding_ignores_only_non_base_role() -> None:
    """只有 FILTER 角色 ⇒ 本读路径没有 BASE 绑定 ⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(),
            sourceRows=(_sourceRow(bindingRole="FILTER"),),
            propertyRows=(),
            now=_NOW,
        )
    assert "绑定" in str(exc.value)


def test_compile_binding_rejects_future_source_binding() -> None:
    """未来才生效的绑定不是「已生效」⇒ 不得进读路径。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(),
            sourceRows=(_sourceRow(validFrom=_FUTURE),),
            propertyRows=(_propertyRow(),),
            now=_NOW,
        )
    assert "绑定" in str(exc.value)


def test_compile_binding_skips_future_property_binding() -> None:
    """未来才生效的属性绑定不进列映射 ⇒ 必需属性缺失 ⇒ fail-closed。"""
    from app.services import ontology_binding_resolver as mod

    resolved = mod._compileBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        classRow=_classRow(),
        sourceRows=(_sourceRow(),),
        propertyRows=(_propertyRow(validFrom=_FUTURE),),
        now=_NOW,
    )
    assert resolved.columns == {}


def test_compile_binding_rejects_non_thbi_schema() -> None:
    """硬规则：绑定 schema 必须是 THBI。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(),
            sourceRows=(_sourceRow(schemaName="ZJTH"),),
            propertyRows=(),
            now=_NOW,
        )
    assert "THBI" in str(exc.value)


def test_compile_binding_rejects_empty_physical_column() -> None:
    """物理列缺失 ⇒ fail-closed（不回退到本体表的 source_column）。"""
    from app.services import ontology_binding_resolver as mod

    with pytest.raises(BindingResolutionError) as exc:
        mod._compileBinding(
            className="DWD_PURCHASE_ORDER_DTL",
            datasourceId=1,
            classRow=_classRow(),
            sourceRows=(_sourceRow(),),
            propertyRows=(_propertyRow(sourceColumn=""),),
            now=_NOW,
        )
    assert "PO_NO" in str(exc.value)


def test_compile_binding_ignores_columns_of_other_source_binding() -> None:
    """别的源绑定上的列名不得混进本类的列映射（FILTER 表列不混入 BASE）。"""
    from app.services import ontology_binding_resolver as mod

    resolved = mod._compileBinding(
        className="DWD_PURCHASE_ORDER_DTL",
        datasourceId=1,
        classRow=_classRow(),
        sourceRows=(_sourceRow(),),
        propertyRows=(
            _propertyRow(propertyName="PO_NO", sourceColumn="PO_NO"),
            _propertyRow(
                propertyName="PO_QTY",
                sourceColumn="PO_QTY",
                sourceBindingId="sid2",
            ),
        ),
        now=_NOW,
    )
    assert resolved.columns == {"PO_NO": "PO_NO"}
