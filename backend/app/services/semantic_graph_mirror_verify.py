"""镜像 vs 计划的**对账**（P3b D24）——纯比较，无 IO。

「应然」= `buildMirrorPlan` 的产物（manifest + 当前解析 ⇒ 计划）；
「实然」= 从图上读回的镜像节点与边。逐条求差，绝不静默。

⚠️ 口径（L-13）：计划用的是**当前**的 `id_mapping` 解析。若物化之后映射变了，
对账会出现「计划有图上没有 + 图上有计划没有」的成对差异 —— 这可能是**解析漂移**
而不是镜像缺陷；报告原样给出差异明细，运维结合台账 `manifest_hash` 判断。
真正的「发布当时快照对账」需要发布时冻结 unified_id 清单（另立卡）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.semantic_graph_mirror_spec import (
    RELEASE_MIRROR_ENTITY_TYPES,
    RELEASE_MIRROR_NODE_FIELDS,
    RELEASE_MIRROR_RELEASE_FIELDS,
)
from app.services.semantic_graph_mirror import MirrorPlan

STATUS_CONSISTENT = "consistent"
STATUS_INCONSISTENT = "inconsistent"


@dataclass(frozen=True)
class NodeDiff:
    """缺一个 / 多一个镜像节点。"""

    entityType: str
    unifiedId: str


@dataclass(frozen=True)
class FieldMismatch:
    """两边都有的节点上，某个字段的值对不上。"""

    entityType: str
    unifiedId: str
    field: str
    expected: Any
    actual: Any


@dataclass(frozen=True)
class EdgeDiff:
    """缺一条 / 多一条镜像边（边无属性 ⇒ 差异只有端点对）。"""

    relType: str
    sourceUid: str
    targetUid: str


@dataclass(frozen=True)
class MirrorVerification:
    """对账结果：任何差异非空 ⇒ inconsistent。"""

    status: str
    missingNodes: tuple[NodeDiff, ...]
    extraNodes: tuple[NodeDiff, ...]
    fieldMismatches: tuple[FieldMismatch, ...]
    missingEdges: tuple[EdgeDiff, ...]
    extraEdges: tuple[EdgeDiff, ...]

    @property
    def isConsistent(self) -> bool:
        return self.status == STATUS_CONSISTENT

    @property
    def diffCount(self) -> int:
        return (
            len(self.missingNodes)
            + len(self.extraNodes)
            + len(self.fieldMismatches)
            + len(self.missingEdges)
            + len(self.extraEdges)
        )


def _expectedFieldMap(plan: MirrorPlan, node) -> dict[str, Any]:
    """计划节点的**全部**可比字段：实体字段 + 发布标识字段。"""
    fields = dict(node.fields)
    fields["release_id"] = plan.releaseId
    fields["release_version"] = plan.releaseVersion
    fields["mirror_of"] = node.unifiedId
    return fields


def _diffNodes(plan: MirrorPlan, actualNodes) -> tuple[
    list[NodeDiff], list[NodeDiff], list[FieldMismatch]
]:
    expected = {(n.entityType, n.unifiedId): n for n in plan.nodes}
    actual: dict[tuple[str, str], dict[str, Any]] = {}
    for entityType in RELEASE_MIRROR_ENTITY_TYPES:
        for row in actualNodes.get(entityType, []):
            actual[(entityType, row.get("unified_id"))] = row
    missing: list[NodeDiff] = []
    extra: list[NodeDiff] = []
    mismatches: list[FieldMismatch] = []
    for key, node in expected.items():
        row = actual.get(key)
        if row is None:
            missing.append(NodeDiff(entityType=key[0], unifiedId=key[1]))
            continue
        comparable = RELEASE_MIRROR_NODE_FIELDS[key[0]] + RELEASE_MIRROR_RELEASE_FIELDS
        for field in comparable:
            expectedValue = _expectedFieldMap(plan, node).get(field)
            actualValue = row.get(field)
            if expectedValue != actualValue:
                mismatches.append(
                    FieldMismatch(
                        entityType=key[0],
                        unifiedId=key[1],
                        field=field,
                        expected=expectedValue,
                        actual=actualValue,
                    )
                )
    for key in actual:
        if key not in expected:
            extra.append(NodeDiff(entityType=key[0], unifiedId=key[1]))
    return missing, extra, mismatches


def _diffEdges(
    plan: MirrorPlan, actualEdges: Iterable[tuple[str, str, str]]
) -> tuple[list[EdgeDiff], list[EdgeDiff]]:
    expected = {(e.relType, e.sourceUid, e.targetUid) for e in plan.edges}
    actual: set[tuple[str, str, str]] = set(actualEdges)
    missing = [EdgeDiff(*triple) for triple in sorted(expected - actual)]
    extra = [EdgeDiff(*triple) for triple in sorted(actual - expected)]
    return missing, extra


def verifyMirrorAgainstPlan(
    plan: MirrorPlan,
    actualNodes: Mapping[str, list[dict[str, Any]]],
    actualEdges: Iterable[tuple[str, str, str]],
) -> MirrorVerification:
    """对账：计划 vs 图。差异全部逐条列出并**确定性排序**（报告可复现）。"""
    missingNodes, extraNodes, fieldMismatches = _diffNodes(plan, actualNodes)
    missingEdges, extraEdges = _diffEdges(plan, actualEdges)
    missingNodes.sort(key=lambda d: (d.entityType, d.unifiedId))
    extraNodes.sort(key=lambda d: (d.entityType, d.unifiedId))
    fieldMismatches.sort(key=lambda m: (m.entityType, m.unifiedId, m.field))
    hasDiff = bool(
        missingNodes or extraNodes or fieldMismatches or missingEdges or extraEdges
    )
    return MirrorVerification(
        status=STATUS_INCONSISTENT if hasDiff else STATUS_CONSISTENT,
        missingNodes=tuple(missingNodes),
        extraNodes=tuple(extraNodes),
        fieldMismatches=tuple(fieldMismatches),
        missingEdges=tuple(missingEdges),
        extraEdges=tuple(extraEdges),
    )
