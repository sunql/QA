"""采购源绑定分区的编译（Manifest v2 / M1-1.3 / 决策 D-9）。

为什么单独一个文件：这个分区有**独立的口径**（候选 ≠ 结论、有效期、名字化引用），
和本体核心（类/属性/JOIN）分开后两个文件各自一屏可读。

四条口径纪律：

1. **只收「已确认且当前生效」的绑定**（D-3：候选不是结论）。
   候选绑定**不进 manifest** —— manifest 就是读路径的数据源，把候选放进去等于
   让未确认的结论被消费。
2. **快照里不出现实例相关列**：`valid_from` / `valid_to` / `confirmed_time` /
   `confirmed_by` / 自增 `id` 全部不进快照 ⇒ 同一治理内容两次编译字节一致
   （M1-a DoD 第 2 条）。有效期语义由「快照 = 发布这一刻生效的绑定集合」承担。
3. **按名字引用**（`className` / `datasourceName` / `propertyName`），
   绝不按 id —— id 是自增主键，进快照会让同一治理内容在不同环境编译出不同字节
   （`measure.class_id` / `kpis.datasourceId` 已各犯过一次）。
4. **表达式安全是发布不变量**（security F1）：过滤表达式与列级变换表达式在
   **编译期**复核一次，不依赖「写入时校验过」这一前提 —— 绕过写路径入库
   （运维脚本 / 数据修复 / 未来的批量导入）不该把越权表达式带进快照。

墓碑类（`valid_to` 非空）及其属性整体丢弃：它们不在活跃模型里，
绑定指向它们 = 快照引用闸会判「不存在」⇒ 快照发不出去。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, TypedDict

from app.domain.exceptions import ValidationError
from app.domain.models import OntologyClass
from app.domain.procurement_binding_models import (
    ProcurementPropertyBinding,
    ProcurementSourceBinding,
)
from app.services.procurement_binding_validation import (
    validateFilterExpression,
    validateTransformExpression,
)
from app.services.semantic_manifest_common import asList, sortBy


class SourceBindingCompileError(ValueError):
    """源绑定数据不满足发布不变量（表达式不安全 / 归属错配 / 引用悬空）。

    ⚠️ **抛错而不是丢弃**：这类行一旦进快照，读路径就会拿着越权表达式去查库；
    而「悄悄跳过」又会让问题不可见（没有校验行可指认是哪一条绑定）。
    批次 B 会补绑定级校验项，把失败**定位到具体绑定**而不是定位到整次发布。
    """


class BindingProperty(TypedDict):
    """绑定内的一个属性映射。

    ⚠️ `propertyClassId` 是**内部字段，不进快照** —— 它只用来校验
    「这条属性确实属于这条绑定所属的类」（库侧没有这个约束，
    只有写路径在挡；绕过写路径入库就会错配）。
    """

    propertyName: str
    sourceColumn: str
    keyOrdinal: int | None
    transformExpression: str | None
    propertyClassId: int | None


def compileSourceBindings(
    sourceBindings: Iterable[ProcurementSourceBinding],
    propertyBindings: Iterable[ProcurementPropertyBinding],
    allClassById: dict[int, OntologyClass],
    activeNameById: dict[int, str],
    propertyNameById: dict[int, str],
    propertyClassIdById: dict[int, int],
    datasourceNameById: dict[int, str],
) -> list[dict[str, Any]]:
    """编译 `sourceBindings` 分区（纯函数，无 IO）。

    `propertyNameById` 只含**活跃类**的属性（墓碑类的属性不在其中）⇒
    指向墓碑类的属性绑定会被自然丢弃。
    """
    propertiesByBinding = groupBindingProperties(
        propertyBindings, propertyNameById, propertyClassIdById
    )
    compiled = []
    for row in sourceBindings:
        if not isPublishableBinding(row):
            continue
        if row.class_id not in allClassById:
            raise SourceBindingCompileError(
                f"本体数据异常：procurement_source_binding.class_id={row.class_id} "
                "不存在于 ontology_class"
            )
        if row.class_id not in activeNameById:
            # 类已是墓碑 ⇒ 类不在活跃模型里 ⇒ 绑定整体丢弃
            # （否则快照引用闸会判「类不存在」⇒ 快照发不出去）。
            continue
        properties = list(propertiesByBinding.get(row.id, ()))
        _assertPropertiesBelongToClass(row, properties)
        compiled.append(
            compileSourceBinding(
                row,
                activeNameById,
                allClassById,
                datasourceNameById,
                properties,
            )
        )
    return sortBy(compiled, key=bindingSortKey)


def isPublishableBinding(
    row: ProcurementSourceBinding, now: datetime | None = None
) -> bool:
    """绑定能否进快照：已确认（`is_candidate` 为假）**且** 当前生效。

    D-3：候选不是结论，不得被读路径（含 manifest）消费。

    三条判据：
    1. `is_candidate` 必须**显式为假** —— `not row.is_candidate` 在字段为 `None`
       时会 fail-open（`not None` 为真 ⇒ 候选被当成结论），这里改成
       `is False`（未知 ⇒ 不发布）；
    2. `valid_to` 必须为空（已失效不是「当前生效的绑定集合」）；
    3. `valid_from` 不得晚于此刻 —— 未来才生效的绑定**还没生效**，
       发布它等于提前消费未生效的结论（`valid_from` 在库侧是 `now()` 默认值，
       这条只防「绕过写路径手写未来时间」的行）。
    """
    if row.is_candidate is not False:
        return False
    if row.valid_to is not None:
        return False
    if row.valid_from is None:
        # 库里该列 NOT NULL（默认 now()）；None 只可能是**尚未落库**的内存对象
        # ⇒ 按「已生效」处理（落库后必然有值）。
        return True
    effectiveNow = now if now is not None else datetime.now(UTC)
    return row.valid_from <= effectiveNow


def groupBindingProperties(
    propertyBindings: Iterable[ProcurementPropertyBinding],
    propertyNameById: dict[int, str],
    propertyClassIdById: dict[int, int],
) -> dict[int, list[BindingProperty]]:
    """按源绑定分组属性绑定（只收已确认且生效、且属性仍在活跃模型里的）。"""
    grouped: dict[int, list[BindingProperty]] = {}
    for row in propertyBindings:
        if row.is_candidate is not False or row.valid_to is not None:
            continue
        if row.valid_from is not None and row.valid_from > datetime.now(UTC):
            # 未来才生效的属性绑定 ⇒ 还没生效 ⇒ 不进快照
            # （与源绑定同一口径：快照 = 发布这一刻生效的集合）。
            continue
        propertyName = propertyNameById.get(row.property_id)
        if propertyName is None:
            # 属性所属类已是墓碑 ⇒ 属性不在活跃模型里 ⇒ 丢弃这条绑定。
            continue
        grouped.setdefault(row.source_binding_id, []).append(
            {
                "propertyName": propertyName,
                "sourceColumn": row.source_column,
                "keyOrdinal": row.key_ordinal,
                "transformExpression": row.transform_expression,
                "propertyClassId": propertyClassIdById.get(row.property_id),
            }
        )
    return {
        bindingId: sortBy(items, key=_bindingPropertySortKey)
        for bindingId, items in grouped.items()
    }


def compileSourceBinding(
    row: ProcurementSourceBinding,
    activeNameById: dict[int, str],
    allClassById: dict[int, OntologyClass],
    datasourceNameById: dict[int, str],
    properties: list[BindingProperty],
) -> dict[str, Any]:
    if row.is_candidate is not False or row.valid_to is not None:
        raise ValueError("compileSourceBinding 只接受已确认且当前生效的绑定")
    if row.class_id not in allClassById:
        raise ValueError(
            f"本体数据异常：procurement_source_binding.class_id={row.class_id} "
            "不存在于 ontology_class"
        )
    className = activeNameById.get(row.class_id)
    if className is None:
        raise ValueError(
            f"本体数据异常：procurement_source_binding.class_id={row.class_id} "
            "指向已删除类"
        )
    datasourceName = datasourceNameById.get(row.datasource_id)
    if datasourceName is None:
        raise ValueError(
            f"本体数据异常：procurement_source_binding.datasource_id={row.datasource_id} "
            "不存在于 data_source"
        )
    _assertFilterExpressionSafe(row)
    for prop in properties:
        _assertTransformExpressionSafe(row, prop)
    return {
        "className": className,
        "datasourceName": datasourceName,
        "schemaName": row.schema_name,
        "sourceTable": row.source_table,
        "bindingRole": row.binding_role,
        "filterExpression": row.filter_expression,
        "filterAllowlist": asList(row.filter_allowlist),
        "properties": [_publicProperty(prop) for prop in properties],
    }


def bindingSortKey(binding: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """绑定排序键：全字符串、无 None ⇒ 稳定且不依赖入库顺序。"""
    return (
        binding["className"],
        binding["datasourceName"] or "",
        binding["schemaName"] or "",
        binding["sourceTable"],
        binding["bindingRole"],
    )


def _bindingPropertySortKey(item: BindingProperty) -> tuple[str, str, int, str]:
    """属性排序键：**覆盖全部出参字段** ⇒ 并列时两条内容相同 ⇒ 顺序不影响字节。

    ⚠️ 不用 id 兜底：id 是自增主键，跨环境不同 ⇒ 用它排序会破坏
    「同一治理内容不同环境编译出同一字节」。
    """
    return (
        item["propertyName"],
        item["sourceColumn"],
        item["keyOrdinal"] if item["keyOrdinal"] is not None else 0,
        item["transformExpression"] or "",
    )


def _publicProperty(prop: BindingProperty) -> dict[str, Any]:
    """出参视图：去掉内部字段 `propertyClassId`。"""
    return {
        "propertyName": prop["propertyName"],
        "sourceColumn": prop["sourceColumn"],
        "keyOrdinal": prop["keyOrdinal"],
        "transformExpression": prop["transformExpression"],
    }


def _assertPropertiesBelongToClass(
    row: ProcurementSourceBinding, properties: Iterable[BindingProperty]
) -> None:
    """属性必须属于**绑定所属的类**。

    为什么库侧不挡：`procurement_property_binding` 的 FK 只指向
    `ontology_property` 与 `procurement_source_binding`，没有
    `property.class_id = binding.class_id` 的约束 ⇒ 绕过写路径入库就会出现
    「类 A 的绑定带着类 B 的属性」⇒ 快照引用闸会判「不属于类」⇒
    **一次坏行阻断所有域的发布**，且报错指向「第 N 条属性」而不是哪条绑定。
    ⇒ 编译期就定位到绑定 id。
    """
    for prop in properties:
        classId = prop["propertyClassId"]
        if classId is not None and classId != row.class_id:
            raise SourceBindingCompileError(
                f"绑定 id={row.id}（class_id={row.class_id}）挂了属于类 "
                f"{classId} 的属性 {prop['propertyName']!r} ⇒ 属性归属错配"
            )


def _assertFilterExpressionSafe(row: ProcurementSourceBinding) -> None:
    """过滤表达式在**编译期**复核（security F1）。

    写入路径已经校验过一次，但「写入时校验过」不是不变量 ——
    绕过写路径的行会把越权表达式带进快照，而快照正是读路径的数据源。
    """
    try:
        validateFilterExpression(row.filter_expression, asList(row.filter_allowlist))
    except ValidationError as exc:
        raise SourceBindingCompileError(
            f"绑定 id={row.id} 的过滤表达式不合规：{exc}"
        ) from exc


def _assertTransformExpressionSafe(
    row: ProcurementSourceBinding, prop: BindingProperty
) -> None:
    """列级变换表达式在**编译期**复核（同上）。"""
    try:
        validateTransformExpression(prop["transformExpression"], prop["sourceColumn"])
    except ValidationError as exc:
        raise SourceBindingCompileError(
            f"绑定 id={row.id} 的属性 {prop['propertyName']!r} 的变换表达式不合规：{exc}"
        ) from exc
