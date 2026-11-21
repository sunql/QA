"""采购域 ``property_alias`` 回填的纯规划 / 校验层（零 IO）。

为什么先规划再落库
------------------
``property_alias`` 只影响两处：展示名（schema 文本 / 前端标签）与 resolver 的
**阶段 3**。当前 12 条 KPI 的治理 token 都在阶段 1/2 就命中了，所以回填
**不会**改变对账结论 —— 但它有两条硬风险：

1. **覆盖**：ASSET 域已有人工维护的 8 条 alias，本批绝不能被改写；
2. **歧义**：同一 KPI 的源类作用域内，两个物理列若得到同一 alias，
   resolver 的 ``_decide`` 会返回 unknown —— 把一个本来能解析的 token
   变成「不可判定」，等于把工具修坏。

本模块只做三件纯函数的事：解析映射、规划填充、查作用域歧义。
读库 / 落库 / 模拟对账全部留给 :mod:`scripts.backfill_property_alias`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ACTION_FILL = "fill"
ACTION_KEEP = "keep"


class AliasBackfillError(Exception):
    """映射与库内属性不一致 / 映射自身非法（拒绝写库）。"""


@dataclass(frozen=True)
class AliasEntry:
    """映射文件的一行：某类的某物理列 → 中文业务名。"""

    class_name: str
    source_column: str
    alias: str
    rationale: str = ""


@dataclass(frozen=True)
class PropertyRow:
    """落库前读到的 ``ontology_property`` 现状（只带规划需要的列）。

    ``class_id`` 是**写库定位键**：``class_name`` 不唯一（同名可有多版本，
    墓碑行仍带属性）⇒ UPDATE / 复核必须按 ``class_id`` 走。
    """

    class_name: str
    property_name: str
    source_column: str | None
    current_alias: str | None
    class_id: int = 0


@dataclass(frozen=True)
class AliasPlan:
    """单列的回填计划；``ACTION_KEEP`` 一律不改库。"""

    class_name: str
    source_column: str
    alias: str
    current_alias: str | None
    action: str
    rationale: str = ""
    class_id: int = 0

    @property
    def has_change(self) -> bool:
        return self.action == ACTION_FILL


@dataclass(frozen=True)
class ScopeCollision:
    """同一 KPI 作用域内，一个 alias 指向了多个物理列（会造成 resolver unknown）。

    ``fill_columns`` = 本批**新填**的列。存量冲突（两列都已有 alias 且相同）
    不是本批造成的，只报不拦；只有 ``fill_columns`` 非空才拦写库。
    """

    kpi_code: str
    alias: str
    columns: tuple[str, ...]
    fill_columns: tuple[str, ...] = ()

    @property
    def isIntroduced(self) -> bool:
        return bool(self.fill_columns)


@dataclass(frozen=True)
class ExcludedTarget:
    """映射里刻意留空的 (类, 物理列) —— 只用于覆盖率校验。"""

    class_name: str
    source_column: str
    reason: str = ""


def _text(value: Any, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AliasBackfillError(f"{what} 必须是非空字符串（实际 {value!r}）")
    return value.strip()


def parseMapping(raw: Any) -> tuple[AliasEntry, ...]:
    """校验映射 JSON（``{"entries": [...]}`` 或裸数组）并去重。"""
    rows = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(rows, list) or not rows:
        raise AliasBackfillError("映射必须是非空数组，或含非空 entries 数组的对象")
    seen: set[tuple[str, str]] = set()
    entries: list[AliasEntry] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AliasBackfillError(f"第 {index} 行不是对象：{row!r}")
        entry = AliasEntry(
            class_name=_text(row.get("className"), what=f"第 {index} 行 className"),
            source_column=_text(row.get("sourceColumn"), what=f"第 {index} 行 sourceColumn"),
            alias=_text(row.get("alias"), what=f"第 {index} 行 alias"),
            rationale=str(row.get("rationale") or ""),
        )
        key = (_normalizeIdentity(entry.class_name), _normalizeIdentity(entry.source_column))
        if key in seen:
            raise AliasBackfillError(f"映射重复：{entry.class_name}.{entry.source_column}")
        seen.add(key)
        entries.append(entry)
    return tuple(entries)


def parseExcluded(raw: Any) -> tuple[ExcludedTarget, ...]:
    """校验映射的 ``excluded`` 段（刻意留空的列）；缺省 ⇒ 空元组。"""
    rows = raw.get("excluded") if isinstance(raw, dict) else None
    if rows is None:
        return ()
    if not isinstance(rows, list):
        raise AliasBackfillError("excluded 必须是数组")
    seen: set[tuple[str, str]] = set()
    result: list[ExcludedTarget] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise AliasBackfillError(f"excluded 第 {index} 行不是对象：{row!r}")
        target = ExcludedTarget(
            class_name=_text(row.get("className"), what=f"excluded 第 {index} 行 className"),
            source_column=_text(
                row.get("sourceColumn"), what=f"excluded 第 {index} 行 sourceColumn"
            ),
            reason=str(row.get("reason") or ""),
        )
        key = (_normalizeIdentity(target.class_name), _normalizeIdentity(target.source_column))
        if key in seen:
            raise AliasBackfillError(
                f"excluded 重复：{target.class_name}.{target.source_column}"
            )
        seen.add(key)
        result.append(target)
    return tuple(result)


def assertMappingCoverage(
    entries: tuple[AliasEntry, ...],
    excluded: tuple[ExcludedTarget, ...],
    referenced: set[tuple[str, str]],
) -> None:
    """``referenced`` 里的每个 (类, 列) 必须被 ``entries ∪ excluded`` 覆盖。

    没有这道校验，「45 = 32 + 13」就只是文档里的手工对账：映射漏掉一列、
    或 ``excluded`` 写错类名，dry-run 仍然全绿。
    """
    covered = {
        (_normalizeIdentity(row.class_name), _normalizeIdentity(row.source_column))
        for row in (*entries, *excluded)
    }
    uncovered = sorted(
        f"{className}.{column}"
        for className, column in referenced
        if (_normalizeIdentity(className), _normalizeIdentity(column)) not in covered
    )
    if uncovered:
        raise AliasBackfillError(
            "映射未覆盖以下被引用的物理列（entries + excluded 合计 "
            f"{len(covered)} 列，引用 {len(referenced)} 列）：" + ", ".join(uncovered)
        )


def _normalizeIdentity(value: str) -> str:
    return value.strip().upper()


def planAliasFill(
    entries: tuple[AliasEntry, ...], properties: tuple[PropertyRow, ...]
) -> tuple[AliasPlan, ...]:
    """映射 ⋈ 库内属性 → 逐行计划；目标不存在 / 不唯一或已有人工 alias ⇒ 报错 / 保留。"""
    byKey: dict[tuple[str, str], list[PropertyRow]] = {}
    for row in properties:
        if row.source_column is None:
            continue
        key = (_normalizeIdentity(row.class_name), _normalizeIdentity(row.source_column))
        byKey.setdefault(key, []).append(row)

    plans: list[AliasPlan] = []
    missing: list[str] = []
    ambiguous: list[str] = []
    for entry in entries:
        key = (_normalizeIdentity(entry.class_name), _normalizeIdentity(entry.source_column))
        candidates = byKey.get(key, [])
        if not candidates:
            missing.append(f"{entry.class_name}.{entry.source_column}")
            continue
        if len(candidates) > 1:
            # 同名类的多个有效版本都带这列 ⇒ 无法确定写哪一行，拒绝猜测。
            ambiguous.append(
                f"{entry.class_name}.{entry.source_column}"
                f"（class_id={sorted(row.class_id for row in candidates)}）"
            )
            continue
        row = candidates[0]
        current = (row.current_alias or "").strip() or None
        if current is not None:
            # 已有人工维护的 alias：绝不覆盖（差异只报告，由人决定）。
            plans.append(
                AliasPlan(
                    class_name=entry.class_name,
                    source_column=entry.source_column,
                    alias=entry.alias,
                    current_alias=current,
                    action=ACTION_KEEP,
                    rationale=entry.rationale,
                    class_id=row.class_id,
                )
            )
            continue
        plans.append(
            AliasPlan(
                class_name=entry.class_name,
                source_column=entry.source_column,
                alias=entry.alias,
                current_alias=None,
                action=ACTION_FILL,
                rationale=entry.rationale,
                class_id=row.class_id,
            )
        )
    if missing or ambiguous:
        parts: list[str] = []
        if missing:
            parts.append(
                "映射指向库内不存在的属性（类名或物理列写错）：" + ", ".join(sorted(missing))
            )
        if ambiguous:
            parts.append("同名类存在多条有效记录，无法确定写入目标：" + ", ".join(ambiguous))
        raise AliasBackfillError("；".join(parts))
    return tuple(plans)


def findScopeCollisions(
    plans: tuple[AliasPlan, ...],
    scopes: dict[str, set[tuple[str, str]]],
    existingAliases: dict[tuple[str, str], str] | None = None,
) -> tuple[ScopeCollision, ...]:
    """``scopes`` = ``{kpi_code: {(CLASS, COLUMN), ...}}``；查同别名多物理列。

    ⚠️ 只把「本批新填的列」当作目标是不够的：若**库里已有**的 alias 与本批
    回填值相同，写完后同一作用域就有两列共享一个 alias ⇒ resolver 返回
    unknown。所以有效 alias = 现有 alias ∪ 本批 fills，且只要冲突组里
    含本批 fills 就报（``isIntroduced``）。存量冲突不是本批造成的，只报告。
    """
    existing = existingAliases or {}
    filled: dict[tuple[str, str], str] = {
        (_normalizeIdentity(plan.class_name), _normalizeIdentity(plan.source_column)): plan.alias
        for plan in plans
        if plan.has_change
    }

    def _aliasOf(className: str, column: str) -> str | None:
        key = (_normalizeIdentity(className), _normalizeIdentity(column))
        planned = filled.get(key)
        if planned is not None:
            return planned
        current = existing.get(key)
        return (current or "").strip() or None

    collisions: list[ScopeCollision] = []
    for kpiCode in sorted(scopes):
        byAlias: dict[str, list[tuple[str, bool]]] = {}
        for className, column in sorted(scopes[kpiCode]):
            alias = _aliasOf(className, column)
            if alias is None:
                continue
            isFill = (
                _normalizeIdentity(className),
                _normalizeIdentity(column),
            ) in filled
            byAlias.setdefault(alias, []).append((f"{className}.{column}", isFill))
        for alias in sorted(byAlias):
            members = byAlias[alias]
            if len(members) < 2:
                continue
            collisions.append(
                ScopeCollision(
                    kpi_code=kpiCode,
                    alias=alias,
                    columns=tuple(member for member, _ in members),
                    fill_columns=tuple(member for member, isFill in members if isFill),
                )
            )
    return tuple(collisions)


def renderReport(
    plans: tuple[AliasPlan, ...], collisions: tuple[ScopeCollision, ...]
) -> str:
    """人读报告：逐行动作 + 冲突；冲突非空 ⇒ 调用方必须拒绝写库。"""
    fills = [plan for plan in plans if plan.has_change]
    keeps = [plan for plan in plans if not plan.has_change]
    introduced = [collision for collision in collisions if collision.isIntroduced]
    lines = [
        f"计划：fill={len(fills)} keep（已有人工 alias）={len(keeps)} "
        f"alias 冲突={len(introduced)}（存量 {len(collisions) - len(introduced)}）"
    ]
    for plan in plans:
        marker = "+" if plan.has_change else "="
        current = "" if plan.current_alias is None else f"（现有 {plan.current_alias!r}，保留）"
        lines.append(f"  [{marker}] {plan.class_name}.{plan.source_column} → {plan.alias}{current}")
    for collision in collisions:
        kind = "冲突" if collision.isIntroduced else "存量冲突（不拦）"
        lines.append(
            f"  [{kind}] {collision.kpi_code}: alias {collision.alias!r} 指向 "
            + " / ".join(collision.columns)
            + (
                f"（本批新填：{' / '.join(collision.fill_columns)}）"
                if collision.fill_columns
                else ""
            )
        )
    return "\n".join(lines)


__all__ = [
    "ACTION_FILL",
    "ACTION_KEEP",
    "AliasBackfillError",
    "AliasEntry",
    "AliasPlan",
    "ExcludedTarget",
    "PropertyRow",
    "ScopeCollision",
    "assertMappingCoverage",
    "findScopeCollisions",
    "parseExcluded",
    "parseMapping",
    "planAliasFill",
    "renderReport",
]
