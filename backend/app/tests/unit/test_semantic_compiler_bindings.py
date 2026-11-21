"""采购源绑定分区的编译（M1-1.3 / D-9）：候选 ≠ 结论、有效期、字节确定。

本模块是 v2 manifest 的新增分区，独立成文件便于单测定位：
口径一旦与本体核心（类/属性/JOIN）混在一起，改口径时容易漏掉「候选不得进快照」
这条 D-3 纪律。

覆盖：
- 四条排除口径（候选 / 已失效 / 墓碑类 / 墓碑类的属性）；
- 两条数据损坏口径（类不存在 / 数据源不存在 ⇒ 抛错，不静默降级）；
- 排序稳定性与字节确定性（同一输入 ⇒ 同一字节，与入库顺序无关）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.services.semantic_compiler_bindings import (
    SourceBindingCompileError,
    compileSourceBindings,
)
from app.tests.unit._semantic_rows import (
    canonicalJson,
    makeClass,
    makeDataSource,
    makeProperty,
    makePropertyBinding,
    makeSourceBinding,
)

_EXPIRED = datetime(2026, 1, 1, tzinfo=UTC)


def _rows() -> tuple[dict, dict, dict]:
    """一次编译需要的三张索引：类 / 属性 / 数据源名。"""
    classes = {
        1: makeClass(1, "Supplier"),
        2: makeClass(2, "PurchaseOrder"),
        3: makeClass(3, "LegacySupplier", validTo=_EXPIRED),
    }
    properties = {
        11: makeProperty(1, "supplierCode", propertyId=11),
        12: makeProperty(1, "supplierName", propertyId=12),
        21: makeProperty(2, "poNo", propertyId=21),
        31: makeProperty(3, "legacyCode", propertyId=31),
    }
    datasourceNameById = {7: makeDataSource(7, "THBI").name}
    return classes, properties, datasourceNameById


def _compile(
    sourceBindings: list,
    propertyBindings: list | None = None,
) -> list[dict]:
    classes, properties, datasourceNameById = _rows()
    activeNameById = {1: "Supplier", 2: "PurchaseOrder"}
    propertyNameById = {
        pid: row.property_name
        for pid, row in properties.items()
        if row.class_id in activeNameById
    }
    propertyClassIdById = {
        pid: row.class_id
        for pid, row in properties.items()
        if row.class_id in activeNameById
    }
    return compileSourceBindings(
        sourceBindings,
        propertyBindings or [],
        classes,
        activeNameById,
        propertyNameById,
        propertyClassIdById,
        datasourceNameById,
    )


def test_confirmed_active_binding_is_compiled() -> None:
    """已确认 + 当前生效 ⇒ 进快照，按名字引用（不记 id）。"""
    binding = makeSourceBinding(
        100,
        class_id=1,
        datasource_id=7,
        schema_name="ERP",
        source_table="SUPPLIER",
        binding_role="BASE",
        filter_expression="STATUS = 'A'",
        filter_allowlist=["STATUS"],
    )
    propertyBinding = makePropertyBinding(101, 100, 11, "SUPPLIER_CODE", key_ordinal=1)

    compiled = _compile([binding], [propertyBinding])

    assert len(compiled) == 1
    item = compiled[0]
    assert item["className"] == "Supplier"
    assert item["datasourceName"] == "THBI"
    assert item["schemaName"] == "ERP"
    assert item["sourceTable"] == "SUPPLIER"
    assert item["bindingRole"] == "BASE"
    assert item["filterExpression"] == "STATUS = 'A'"
    assert item["filterAllowlist"] == ["STATUS"]
    assert item["properties"] == [
        {
            "propertyName": "supplierCode",
            "sourceColumn": "SUPPLIER_CODE",
            "keyOrdinal": 1,
            "transformExpression": None,
        }
    ]


def test_candidate_binding_is_excluded() -> None:
    """D-3：候选不是结论，不得被读路径（含 manifest）消费。"""
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
        is_candidate=True,
    )

    assert _compile([binding]) == []


def test_expired_binding_is_excluded() -> None:
    """`valid_to` 非空 = 已失效 ⇒ 不是「当前生效的绑定集合」。"""
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
        valid_to=_EXPIRED,
    )

    assert _compile([binding]) == []


def test_binding_of_tombstone_class_is_excluded() -> None:
    """类已是墓碑 ⇒ 类不在活跃模型 ⇒ 绑定整体丢弃（否则快照引用闸判「类不存在」）。"""
    binding = makeSourceBinding(
        100, class_id=3, datasource_id=7, schema_name="ERP", source_table="LEGACY",
    )

    assert _compile([binding]) == []


def test_property_of_tombstone_class_is_excluded() -> None:
    """属性属于墓碑类 ⇒ 该属性不在活跃模型 ⇒ 这条属性绑定丢弃，其余保留。"""
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
    )
    keep = makePropertyBinding(101, 100, 11, "SUPPLIER_CODE")
    drop = makePropertyBinding(102, 100, 31, "LEGACY_CODE")

    compiled = _compile([binding], [keep, drop])

    assert [p["propertyName"] for p in compiled[0]["properties"]] == ["supplierCode"]


def test_binding_of_unknown_class_raises() -> None:
    """类 id 完全不存在 ⇒ 数据被写坏 ⇒ 抛错（不静默降级）。"""
    binding = makeSourceBinding(
        100, class_id=999, datasource_id=7, schema_name="ERP", source_table="X",
    )

    with pytest.raises(ValueError, match="class_id=999"):
        _compile([binding])


def test_binding_of_unknown_datasource_raises() -> None:
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=999, schema_name="ERP", source_table="X",
    )

    with pytest.raises(ValueError, match="datasource_id=999"):
        _compile([binding])


def test_output_order_is_independent_of_input_order() -> None:
    """排序键与入库顺序无关 ⇒ 同一治理内容两次编译字节一致。"""
    first = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
    )
    second = makeSourceBinding(
        200, class_id=2, datasource_id=7, schema_name="ERP", source_table="PORDER",
        binding_role="NUMERATOR",
    )

    forward = _compile([first, second])
    backward = _compile([second, first])

    assert canonicalJson(forward) == canonicalJson(backward)
    assert [b["className"] for b in forward] == ["PurchaseOrder", "Supplier"]


# ---------------------------------------------------------------------------
# 5. 发布不变量（security F1 / code-review LOW-1 / F5）
# ---------------------------------------------------------------------------


def test_binding_of_property_of_another_class_raises() -> None:
    """属性必须属于绑定所属的类：库侧没有这个约束（只有写路径在挡）。

    错配若漏到快照，引用闸会判「不属于类」⇒ 一次坏行阻断**所有域**的发布，
    且报错指向「第 N 条属性」而不是哪条绑定 ⇒ 编译期就要定位到绑定 id。
    """
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
    )
    # 属性 21 属于类 2（PurchaseOrder），绑定属于类 1（Supplier）。
    wrongProperty = makePropertyBinding(101, 100, 21, "PORDER_NO")

    with pytest.raises(SourceBindingCompileError, match="id=100"):
        _compile([binding], [wrongProperty])


def test_unsafe_filter_expression_is_rejected_at_compile_time() -> None:
    """过滤表达式在**编译期**复核：写到库里的表达式若越权，不得进快照。

    写路径已经校验过一次，但「写入时校验过」不是不变量 ——
    绕过写路径入库的行不该把越权表达式带进读路径的数据源。
    """
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
        # 表达式引用了白名单外的列 SALARY。
        filter_expression="SALARY > 100",
        filter_allowlist=["STATUS"],
    )

    with pytest.raises(SourceBindingCompileError, match="过滤表达式"):
        _compile([binding])


def test_filter_expression_without_allowlist_is_rejected() -> None:
    """有表达式却没白名单 = 默认放行 = 越权 ⇒ 编译期同样拒绝。"""
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
        filter_expression="STATUS = 'A'",
        filter_allowlist=[],
    )

    with pytest.raises(SourceBindingCompileError, match="过滤表达式"):
        _compile([binding])


def test_unsafe_transform_expression_is_rejected_at_compile_time() -> None:
    """列级变换表达式只允许引用本属性自己的物理列。"""
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
    )
    propertyBinding = makePropertyBinding(
        101, 100, 11, "SUPPLIER_CODE", transform_expression="SALARY * 2",
    )

    with pytest.raises(SourceBindingCompileError, match="变换表达式"):
        _compile([binding], [propertyBinding])


def test_future_binding_is_not_published() -> None:
    """未来才生效的绑定**还没生效** ⇒ 不进快照（否则等于提前消费未生效的结论）。"""
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
        valid_from=datetime.now(UTC) + timedelta(days=1),
    )

    assert _compile([binding]) == []


def test_future_property_binding_is_not_published() -> None:
    """属性绑定与源绑定同一口径：未来才生效的不进快照。

    绑定本身仍然生效 ⇒ 仍进快照，但**不带**这条尚未生效的属性映射。
    """
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
    )
    propertyBinding = makePropertyBinding(
        101, 100, 11, "SUPPLIER_CODE",
        valid_from=datetime.now(UTC) + timedelta(days=1),
    )

    compiled = _compile([binding], [propertyBinding])

    assert len(compiled) == 1
    assert compiled[0]["properties"] == []


def test_unknown_candidate_flag_is_not_published() -> None:
    """`is_candidate` 为 None（未知）⇒ **不发布**（fail-closed）。

    `not row.is_candidate` 在 None 时为真 ⇒ 候选被当成结论 ⇒ 这里必须显式判 `is False`。
    """
    binding = makeSourceBinding(
        100, class_id=1, datasource_id=7, schema_name="ERP", source_table="SUPPLIER",
    )
    binding.is_candidate = None  # type: ignore[assignment]

    assert _compile([binding]) == []
