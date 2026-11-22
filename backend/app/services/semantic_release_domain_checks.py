"""采购域发布校验项（M1-1.3 批次 B / D-10）：域切片 + 源绑定。

这一组校验项回答三个问题：

1. **这次发布的到底是哪个域的东西？** —— `DOMAIN_SLICE_EMPTY` / `DOMAIN_SLICE_CROSS_DOMAIN`
2. **绑定是不是「结论」？** —— `SOURCE_BINDING_CANDIDATE`（D-3：候选不是结论）
3. **这份绑定发布出去走得通吗？** —— `SOURCE_BINDING_JOIN_MISSING` / `SOURCE_BINDING_PROPERTY_UNBOUND`

## 为什么「域切片」要能被**复算**

发布快照存的是**切片后的 manifest**（采购域发布只带采购域的东西）。
但「跨域补齐了哪些类」这件事**不在**切片里（补齐后的类与本体锚定的类长得一样）
⇒ 若只在编译那一刻算一次，validate / activate 阶段就复算不出来（证据不可复算）。

解法：**闭合是幂等的** —— 对切片再切一次，得到的类集合与原切片完全相同，
补齐集合也一样 ⇒ 校验项可以只依赖**已存快照**，不需要额外存「补齐清单」。

（幂等性依赖两条：① 度量引用闭合是幂等的；② JOIN 邻居闭合取**不动点**
—— 见 `semantic_domain_slice.sliceManifestForRelease` 的说明。）

## 为什么「孤岛」判据取全本体口径

`SOURCE_BINDING_JOIN_MISSING` 问的是「这个类在本体里有没有 JOIN 边」。
若只看切片，「只连跨域边」的类会被误判成孤岛 ⇒ **误阻断合法发布**。
⇒ 切片阶段把**有绑定类的一跳 JOIN 邻居**也拉进来 ⇒
「切片里有没有边」与「本体里有没有边」**完全等价**。

## 候选绑定为什么只出**一条**发布级行

候选绑定**不进快照**（D-3）⇒ 数量没有契约上限 ⇒ 若按候选逐条出校验项，
条数上界就无从推导（M1-1.2 的 off-by-2 教训：上限必须从契约推导）。
⇒ 候选只出**一条发布级汇总行**（WARNING），detail 带总数 + 前 5 个类名。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.semantic_domain_slice import sliceManifestForRelease
from app.services.semantic_release_check_types import (
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    TARGET_BINDING,
    TARGET_RELEASE,
    PublishCheck,
    bindingTargetKey,
)

# ---- 校验项编码（机器可读）----
CHECK_DOMAIN_SLICE_EMPTY = "DOMAIN_SLICE_EMPTY"
CHECK_DOMAIN_SLICE_CROSS_DOMAIN = "DOMAIN_SLICE_CROSS_DOMAIN"
CHECK_SOURCE_BINDING_CANDIDATE = "SOURCE_BINDING_CANDIDATE"
CHECK_SOURCE_BINDING_JOIN_MISSING = "SOURCE_BINDING_JOIN_MISSING"
CHECK_SOURCE_BINDING_PROPERTY_UNBOUND = "SOURCE_BINDING_PROPERTY_UNBOUND"

# ---- 校验项名称（中文，展示用）----
ITEM_DOMAIN_SLICE_EMPTY = "域切片为空"
ITEM_DOMAIN_SLICE_CROSS_DOMAIN = "跨域依赖补齐"
ITEM_SOURCE_BINDING_CANDIDATE = "候选绑定治理债"
ITEM_SOURCE_BINDING_JOIN_MISSING = "绑定类无 JOIN 边"
ITEM_SOURCE_BINDING_PROPERTY_UNBOUND = "绑定类属性未绑列"

# detail 里最多列几个名字（超长会让 check 表的 detail 列难读）
_MAX_DETAIL_NAMES = 10
# 候选汇总行里最多列几个类名
_MAX_CANDIDATE_NAMES = 5


@dataclass(frozen=True, slots=True)
class ReleaseCheckContext:
    """发布校验的**上下文事实**（纯函数够不到的部分）。

    - `domainCode`：本次发布的作用域（`None` = 全局发布 ⇒ 不做域切片校验）。
    - `candidateBindingCount`：**活库**里的候选绑定条数；`None` = **没查过**
      （候选不进快照 ⇒ 只能实时查；没查就不出这一行 —— 宁可少一行，
      也不写一句「没有候选绑定」这种没依据的结论）。
    - `candidateClassNames`：候选绑定涉及的类名（取前几个，供 detail 展示）。
    - `activeDatasourceNames`：**活库**里启用中的数据源名字集合（`None` = 没查过）。
      用于 `MET-002`：快照只记数据源**名字**（id 是自增主键，入档会让跨环境字节不一致），
      「名字还在不在、是否停用」只能实时查。
    - `relationViolations`：**活库** `ontology_relation` 的违规条目（端点类已墓碑 /
      关系类型不在代码白名单）；`None` = 没查过 ⇒ `REL-001` 只判 manifest 内可见部分。
    - `agentViolations`：**活库** `agent_definition` 的违规条目（域/层不在启用词表 /
      工具未注册或已停用 / 未绑定工具）；`None` = 没查过 ⇒ `AGT-001` 不出行。

    ⚠️ 三个 `None` 的语义统一：**没查过 ≠ 没问题**。校验项宁可少一行，
    也不写一句没依据的「全部正常」。
    """

    domainCode: str | None = None
    candidateBindingCount: int | None = None
    candidateClassNames: tuple[str, ...] = ()
    activeDatasourceNames: frozenset[str] | None = None
    relationViolations: tuple[str, ...] | None = None
    agentViolations: tuple[str, ...] | None = None


def evaluateDomainChecks(
    manifest: Mapping[str, Any], *, context: ReleaseCheckContext
) -> tuple[PublishCheck, ...]:
    """域级 + 绑定级校验项（纯函数；候选数由调用方从活库取回放进 context）。"""
    checks: list[PublishCheck] = []
    if context.domainCode:
        checks.extend(_domainSliceChecks(manifest, context.domainCode))
    candidateCheck = _candidateBindingCheck(context)
    if candidateCheck is not None:
        checks.append(candidateCheck)
    checks.extend(_bindingChecks(manifest))
    return tuple(checks)


# ---------------------------------------------------------------------------
# 内部：域切片
# ---------------------------------------------------------------------------


def _domainSliceChecks(
    manifest: Mapping[str, Any], domainCode: str
) -> list[PublishCheck]:
    """域切片校验：空切片（阻断）+ 跨域补齐（告警）。"""
    slice_ = sliceManifestForRelease(manifest, domainCode)
    if not slice_.classes:
        return [
            PublishCheck(
                code=CHECK_DOMAIN_SLICE_EMPTY,
                item=ITEM_DOMAIN_SLICE_EMPTY,
                severity=SEVERITY_ERROR,
                result=RESULT_FAIL,
                targetType=TARGET_RELEASE,
                detail=(
                    f"域 {domainCode} 的切片没有任何类"
                    f"（业务对象 {len(slice_.businessObjects)} 个）"
                    "⇒ 不能发布一份空快照"
                ),
            )
        ]

    closure = slice_.closureClassNames
    if not closure:
        return [
            PublishCheck(
                code=CHECK_DOMAIN_SLICE_CROSS_DOMAIN,
                item=ITEM_DOMAIN_SLICE_CROSS_DOMAIN,
                severity=SEVERITY_WARNING,
                result=RESULT_PASS,
                targetType=TARGET_RELEASE,
                detail=f"域 {domainCode} 的切片无需跨域补齐",
            )
        ]
    return [
        PublishCheck(
            code=CHECK_DOMAIN_SLICE_CROSS_DOMAIN,
            item=ITEM_DOMAIN_SLICE_CROSS_DOMAIN,
            severity=SEVERITY_WARNING,
            result=RESULT_FAIL,
            targetType=TARGET_RELEASE,
            detail=(
                f"域 {domainCode} 的切片为满足引用契约补齐了 {len(closure)} 个跨域类："
                f"{_joinNames(closure)}"
            ),
        )
    ]


# ---------------------------------------------------------------------------
# 内部：候选绑定
# ---------------------------------------------------------------------------


def _candidateBindingCheck(context: ReleaseCheckContext) -> PublishCheck | None:
    """候选绑定汇总行（发布级 WARNING）；**没查过候选数 ⇒ 不出这一行**。

    D-3：候选不是结论 ⇒ 不进快照、不被读路径消费；但**必须显式暴露**
    （否则治理债会静默消失）。

    ⚠️ `candidateBindingCount is None` ⇒ 调用方没查活库 ⇒ **不出行**：
    写一句「没有候选绑定」是**没依据的结论**（比缺一行更坏）。
    """
    if context.candidateBindingCount is None:
        return None
    count = max(0, int(context.candidateBindingCount))
    if count == 0:
        return PublishCheck(
            code=CHECK_SOURCE_BINDING_CANDIDATE,
            item=ITEM_SOURCE_BINDING_CANDIDATE,
            severity=SEVERITY_WARNING,
            result=RESULT_PASS,
            targetType=TARGET_RELEASE,
            detail="没有未确认（候选）的绑定（源绑定 / 属性绑定）",
        )
    names = list(context.candidateClassNames)[:_MAX_CANDIDATE_NAMES]
    return PublishCheck(
        code=CHECK_SOURCE_BINDING_CANDIDATE,
        item=ITEM_SOURCE_BINDING_CANDIDATE,
        severity=SEVERITY_WARNING,
        result=RESULT_FAIL,
        targetType=TARGET_RELEASE,
        detail=(
            f"仍有 {count} 条未确认（候选）的绑定（源绑定 / 属性绑定）未进快照"
            f"（涉及类：{_joinNames(names)}）⇒ 治理债，待业务确认"
        ),
    )


# ---------------------------------------------------------------------------
# 内部：绑定级校验
# ---------------------------------------------------------------------------


def _bindingChecks(manifest: Mapping[str, Any]) -> list[PublishCheck]:
    """绑定级校验：孤岛（ERROR，逐条绑定）+ 属性未绑列（WARNING，逐类）。"""
    bindings = _partition(manifest, "sourceBindings")
    if not bindings:
        return []
    joins = _partition(manifest, "joins")
    propertyNamesByClass = _classPropertyNames(manifest)
    checks: list[PublishCheck] = []
    for binding in bindings:
        className = binding.get("className")
        if not isinstance(className, str) or not className:
            continue
        checks.append(_joinEdgeCheck(className, _bindingTargetKey(binding), joins))
    checks.extend(_unboundPropertyChecks(bindings, propertyNamesByClass))
    return checks


def _unboundPropertyChecks(
    bindings: Sequence[Mapping[str, Any]],
    propertyNamesByClass: Mapping[str, frozenset[str]],
) -> list[PublishCheck]:
    """逐**类**判「属性未绑列」（WARNING）。

    ⚠️ 必须按类取**并集**：同一个类可以有多条合法绑定（SSOT §6：同一类可有多条
    BASE 绑定 = 多落点）。判据若只看「这一条绑定」，则「绑定 T1 映射 a、绑定 T2
    映射 b」的类会被两条绑定各报一次「未绑列」——**假阳性**（类其实已经绑全）。
    ⇒ 判据 = 「该类**所有绑定**已映射的属性名的并集」与「类声明了源列的属性名」
    求差；每个类只出**一行**（同一类的多条绑定不重复报同一条债）。
    """
    bindingsByClass: dict[str, list[Mapping[str, Any]]] = {}
    for binding in bindings:
        className = binding.get("className")
        if not isinstance(className, str) or not className:
            continue
        bindingsByClass.setdefault(className, []).append(binding)
    checks: list[PublishCheck] = []
    for className in sorted(bindingsByClass):
        group = bindingsByClass[className]
        representative = min(group, key=_bindingTargetKey)
        checks.append(
            _unboundPropertyCheck(
                group,
                className,
                _bindingTargetKey(representative),
                propertyNamesByClass,
            )
        )
    return checks


def _joinEdgeCheck(
    className: str, targetKey: str, joins: Sequence[Mapping[str, Any]]
) -> PublishCheck:
    """绑定类在本体里是否有 JOIN 边（孤岛判据）。"""
    if _hasJoinEdge(joins, className):
        return PublishCheck(
            code=CHECK_SOURCE_BINDING_JOIN_MISSING,
            item=ITEM_SOURCE_BINDING_JOIN_MISSING,
            severity=SEVERITY_ERROR,
            result=RESULT_PASS,
            targetType=TARGET_BINDING,
            targetKey=targetKey,
            detail=f"绑定类 {className} 在本体里有 JOIN 边",
        )
    return PublishCheck(
        code=CHECK_SOURCE_BINDING_JOIN_MISSING,
        item=ITEM_SOURCE_BINDING_JOIN_MISSING,
        severity=SEVERITY_ERROR,
        result=RESULT_FAIL,
        targetType=TARGET_BINDING,
        targetKey=targetKey,
        detail=(
            f"绑定类 {className} 在整个本体里零 JOIN 边（孤岛）"
            "⇒ 这份绑定发布出去也走不通"
        ),
    )


def _unboundPropertyCheck(
    bindings: Sequence[Mapping[str, Any]],
    className: str,
    targetKey: str,
    propertyNamesByClass: Mapping[str, frozenset[str]],
) -> PublishCheck:
    """类已绑源表，但类上有 `sourceColumn` 的属性**没有任何绑定**映射（WARNING）。

    判据只看「**类声明了物理列**」的属性：`ontology_property.source_column`
    非空 ⇒ 类自称这个属性来自某个物理列；该类所有绑定里都没有对应映射 ⇒
    落到这些表时这一列无处可取。

    ⚠️ 取该类**所有绑定**的并集（见 `_unboundPropertyChecks`）：同一类可以有多条
    绑定（多落点），分摊映射是合法的。
    """
    unbound = _unboundProperties(bindings, propertyNamesByClass.get(className, frozenset()))
    if not unbound:
        return PublishCheck(
            code=CHECK_SOURCE_BINDING_PROPERTY_UNBOUND,
            item=ITEM_SOURCE_BINDING_PROPERTY_UNBOUND,
            severity=SEVERITY_WARNING,
            result=RESULT_PASS,
            targetType=TARGET_BINDING,
            targetKey=targetKey,
            detail=f"绑定类 {className} 的属性全部有列映射",
        )
    return PublishCheck(
        code=CHECK_SOURCE_BINDING_PROPERTY_UNBOUND,
        item=ITEM_SOURCE_BINDING_PROPERTY_UNBOUND,
        severity=SEVERITY_WARNING,
        result=RESULT_FAIL,
        targetType=TARGET_BINDING,
        targetKey=targetKey,
        detail=(
            f"绑定类 {className}（{len(bindings)} 条绑定）有 {len(unbound)} 个属性"
            f"声明了源列但未绑定列：{_joinNames(unbound)}"
        ),
    )


def _unboundProperties(
    bindings: Sequence[Mapping[str, Any]], declaredPropertyNames: frozenset[str]
) -> list[str]:
    """类声明了源列、但该类**所有绑定**都没映射的属性名（已排序）。"""
    if not declaredPropertyNames:
        return []
    mapped: set[str] = set()
    for binding in bindings:
        mapped.update(
            str(prop.get("propertyName"))
            for prop in _partition(binding, "properties")
            if isinstance(prop, Mapping) and isinstance(prop.get("propertyName"), str)
        )
    return sorted(name for name in declaredPropertyNames if name not in mapped)


def _hasJoinEdge(joins: Sequence[Mapping[str, Any]], className: str) -> bool:
    """该类是否作为 JOIN 的任一端出现过。"""
    for join in joins:
        if join.get("sourceClassName") == className or join.get("targetClassName") == className:
            return True
    return False


def _classPropertyNames(manifest: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """类 → 「声明了源列的属性名」集合。

    只收 `sourceColumn` 非空的属性：**类自称有物理列**才算「应该有映射」。
    """
    result: dict[str, frozenset[str]] = {}
    for cls in _partition(manifest, "classes"):
        className = cls.get("className")
        if not isinstance(className, str) or not className:
            continue
        names = {
            str(prop.get("propertyName"))
            for prop in _partition(cls, "properties")
            if isinstance(prop, Mapping)
            and isinstance(prop.get("propertyName"), str)
            and prop.get("sourceColumn")
        }
        result[className] = frozenset(names)
    return result


def _bindingTargetKey(binding: Mapping[str, Any]) -> str:
    """绑定的定位键（**与差异侧共用同一个渲染器** ⇒ 两侧可对账）。"""
    return bindingTargetKey(binding)


# ---------------------------------------------------------------------------
# 内部：通用
# ---------------------------------------------------------------------------


def _partition(manifest: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    """取一个分区（缺分区 / 非列表 ⇒ 空列表）。"""
    value = manifest.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _joinNames(names: Sequence[str]) -> str:
    """名字列表 ⇒ 展示串（超长截断并报总数）。"""
    listed = list(names)
    if len(listed) > _MAX_DETAIL_NAMES:
        return "、".join(listed[:_MAX_DETAIL_NAMES]) + f" 等 {len(listed)} 个"
    return "、".join(listed)
