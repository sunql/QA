"""结构化度量入档（P1a / D7）：内嵌 `class_id` 必须归一成 `className`。

`MeasureSpecSchema` 把度量存在 JSONB 里，其中 `class_id` 是**自增主键**（DB 加不了
外键）。Manifest 是自包含快照，带上实例 id 会让「同样的治理内容换个环境得到同样字节」
失效，回放快照时还会解析到另一个环境的另一张表。

从 `test_semantic_compiler.py` 拆出（原文件 831 行超 800 行上限）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.semantic_compiler import compileFromRows
from app.tests.unit._semantic_rows import makeClass, makeMetric


def test_measure_embedded_class_reference_is_normalized() -> None:
    """`MeasureSpecSchema` 存的是自增 class_id；Manifest 里必须是 className。

    class_id 是实例相关主键：直接入档会让「同样的治理内容换个环境得到同样字节」
    失效（D5 设计要点 2），回放快照时还会解析到另一个环境的另一张表。
    """
    classes = [makeClass(1, "PurchaseOrder"), makeClass(2, "Supplier")]
    metric = makeMetric(
        10,
        "supplierOnTimeRate",
        metricType="RATIO",
        numeratorMeasure={
            "agg": "SUM",
            "expr": "ON_TIME_QTY",
            "filter": None,
            "class_id": 1,
            "class_alias": "采购订单",
        },
        denominatorMeasure={
            "agg": "COUNT",
            "expr": "PROMISED_RECEIPT_DATE",
            "filter": "STATUS <> 'CANCELLED'",
            "class_id": 2,
            "class_alias": "供应商",
        },
    )

    manifest = compileFromRows(classes=classes, properties=[], joins=[], metrics=[metric])

    [compiled] = manifest["metrics"]
    assert compiled["numeratorMeasure"] == {
        "agg": "SUM",
        "expr": "ON_TIME_QTY",
        "filter": None,
        "className": "PurchaseOrder",
        "classAlias": "采购订单",
    }
    assert compiled["denominatorMeasure"] == {
        "agg": "COUNT",
        "expr": "PROMISED_RECEIPT_DATE",
        "filter": "STATUS <> 'CANCELLED'",
        "className": "Supplier",
        "classAlias": "供应商",
    }
    # 自增 id 不得出现在快照的任何角落
    assert "class_id" not in compiled["numeratorMeasure"]


def test_measure_referencing_tombstoned_class_compiles_to_null_name() -> None:
    """度量指向墓碑类：与 refClassName 同规则 ⇒ className 为 null，其余字段保留。"""
    classes = [
        makeClass(1, "PurchaseOrder"),
        makeClass(2, "LegacySupplier", validTo=datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    metric = makeMetric(
        10,
        "legacyRate",
        numeratorMeasure={
            "agg": "SUM",
            "expr": "QTY",
            "class_id": 2,
            "class_alias": "旧供应商",
        },
    )

    manifest = compileFromRows(classes=classes, properties=[], joins=[], metrics=[metric])

    [compiled] = manifest["metrics"]
    assert compiled["numeratorMeasure"] == {
        "agg": "SUM",
        "expr": "QTY",
        "className": None,
        "classAlias": "旧供应商",
    }


def test_measure_referencing_missing_class_raises() -> None:
    """指向完全不存在 class_id = 治理数据被写坏 ⇒ 抛错，不静默编译成 null。"""
    metric = makeMetric(
        10, "brokenMetric", numeratorMeasure={"agg": "SUM", "class_id": 999}
    )

    with pytest.raises(ValueError, match="class_id=999"):
        compileFromRows(
            classes=[makeClass(1, "PurchaseOrder")],
            properties=[],
            joins=[],
            metrics=[metric],
        )


def test_measure_with_unknown_shape_raises() -> None:
    """度量必须是对象；写成字符串属治理数据损坏，不能悄悄入档。"""
    metric = makeMetric(10, "brokenMetric", numeratorMeasure="SUM(QTY)")

    with pytest.raises(ValueError, match="必须是对象"):
        compileFromRows(
            classes=[makeClass(1, "PurchaseOrder")],
            properties=[],
            joins=[],
            metrics=[metric],
        )


def test_compiler_does_not_mutate_stored_measure_dict() -> None:
    """归一化必须复制新对象：原地改 ORM 行里的 JSON 会污染会话身份映射。"""
    stored = {"agg": "SUM", "expr": "QTY", "class_id": 1, "class_alias": "采购订单"}
    metric = makeMetric(10, "qtySum", numeratorMeasure=stored)

    compileFromRows(
        classes=[makeClass(1, "PurchaseOrder")],
        properties=[],
        joins=[],
        metrics=[metric],
    )

    assert metric.numerator_measure == {
        "agg": "SUM",
        "expr": "QTY",
        "class_id": 1,
        "class_alias": "采购订单",
    }


def test_compiler_does_not_alias_stored_dimension_defaults() -> None:
    """`dimensionDefaults` 同理：入档前必须复制。

    它与 measure 是同一类缺陷（评审实证：`compiled[...] is row.dimension_defaults`
    为 True），下游任何规范化写都会污染会话里的 ORM 行。
    """
    stored = {"time": "LAST_12_MONTHS"}
    metric = makeMetric(10, "qtySum", dimensionDefaults=stored)

    manifest = compileFromRows(classes=[], properties=[], joins=[], metrics=[metric])

    compiled = manifest["metrics"][0]["dimensionDefaults"]
    assert compiled == {"time": "LAST_12_MONTHS"}
    assert compiled is not metric.dimension_defaults
