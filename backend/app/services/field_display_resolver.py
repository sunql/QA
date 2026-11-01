"""字段显示名 / 单位解析（本体 + KPI/特征 + 代码字典）。

**它是什么**：把结果集列名翻译成「中文名 + 单位 + 是否数值」，供表头、图例与
治理提示共用。一次调用最多 4 条批量查询（类 / 属性 / KPI / 特征），不做 N+1。

**优先级**（已确认的设计）：
- 中文名：`property_alias` → `business_aliases[0]` → plan 聚合中文别名 →
  `DISPLAY_COLUMN_DICT` → None；
- 单位：`ontology_property.unit` → `kpi_catalog.unit` / `feature_definition.unit`
  （名字归一后**精确匹配**，不做模糊匹配）→ None。

**歧义不猜**：同名属性命中多个类且取值不一致 ⇒ 视为「无中文含义」。
**无上下文不提示**：既无 plan 也无 sourceTable ⇒ 返回空 map（调用方据此不发提示）。

**绝不抛错**：查询失败记 warning 并返回空 map —— 出图/出表不该被治理数据拖垮。
读取包在 `begin_nested()` 保存点内：失败只回滚本次查询，调用方事务仍可继续使用。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    FeatureDefinition,
    KpiCatalog,
    OntologyClass,
    OntologyProperty,
)
from app.domain.query_plan import QueryPlan
from app.services.chart_spec_builder import aliasByField
from app.services.display_name_resolver import DISPLAY_COLUMN_DICT

logger = logging.getLogger(__name__)

SOURCE_ONTOLOGY_ALIAS = "ontology_alias"
SOURCE_BUSINESS_ALIAS = "business_alias"
SOURCE_PLAN_ALIAS = "plan_alias"
SOURCE_DICT = "dict"
SOURCE_NONE = "none"

__all__ = [
    "FieldDisplay",
    "SOURCE_BUSINESS_ALIAS",
    "SOURCE_DICT",
    "SOURCE_NONE",
    "SOURCE_ONTOLOGY_ALIAS",
    "SOURCE_PLAN_ALIAS",
    "missingFieldHints",
    "resolveFieldDisplays",
]


@dataclass(frozen=True)
class FieldDisplay:
    """单列的显示信息。`label` 不含单位；None = 无中文含义。"""

    label: str | None
    unit: str | None
    isNumeric: bool
    source: str


async def resolveFieldDisplays(
    session: AsyncSession,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    plan: QueryPlan | None = None,
    sourceTable: str | None = None,
) -> dict[str, FieldDisplay]:
    """解析每一列的显示信息；无解析上下文时返回 `{}`。"""
    if not columns:
        return {}
    class_names = _classNamesOf(plan)
    if not class_names and not sourceTable:
        return {}
    try:
        # 保存点：查询失败只回滚本次读取。直接吞掉 DB 错误会让调用方的 PG 事务
        # 停在 aborted 状态，同一 session 的后续语句全部 InFailedSQLTransaction ——
        # 「绝不打断出图」就必须连事务状态一起恢复。
        async with session.begin_nested():
            return await _resolve(
                session,
                columns=list(columns),
                rows=rows,
                class_names=class_names,
                source_table=sourceTable,
                plan=plan,
            )
    except Exception:  # noqa: BLE001 — 治理数据缺失不应打断出图
        logger.warning("字段显示解析失败，全部按未解析处理", exc_info=True)
        return {}


def _classNamesOf(plan: QueryPlan | None) -> list[str]:
    if plan is None:
        return []
    return [name.strip().upper() for name in plan.selectedClasses if name and name.strip()]


def _planAliasByColumn(
    plan: QueryPlan | None, columns: list[str]
) -> dict[str, str]:
    """plan 聚合别名（仅指标列）；复用 `aliasByField` 的同一口径。"""
    return aliasByField(plan, tuple(columns))


async def _loadClasses(
    session: AsyncSession, class_names: list[str], source_table: str | None
) -> list[OntologyClass]:
    statement = select(OntologyClass).where(OntologyClass.valid_to.is_(None))
    if class_names:
        statement = statement.where(func.upper(OntologyClass.class_name).in_(class_names))
    else:
        statement = statement.where(
            func.upper(OntologyClass.source_table) == (source_table or "").strip().upper()
        )
    result = await session.execute(statement)
    return list(result.scalars().all())


async def _loadProperties(
    session: AsyncSession, class_ids: list[int]
) -> list[OntologyProperty]:
    if not class_ids:
        return []
    result = await session.execute(
        select(OntologyProperty).where(OntologyProperty.class_id.in_(class_ids))
    )
    return list(result.scalars().all())


def _pick(values: Iterable[str | None]) -> str | None:
    """去空去重后恰好一个值才返回；0 个或 ≥2 个（歧义）一律 None。"""
    cleaned = {value.strip() for value in values if value and value.strip()}
    if len(cleaned) != 1:
        return None
    return next(iter(cleaned))


def _propertyLabels(properties: list[OntologyProperty]) -> dict[str, str | None]:
    """属性名（大写）→ 中文名（多行歧义 ⇒ None）。"""
    byName: dict[str, list[str | None]] = {}
    for prop in properties:
        alias = prop.property_alias
        if not alias and prop.business_aliases:
            alias = prop.business_aliases[0]
        byName.setdefault(prop.property_name.strip().upper(), []).append(alias)
    return {name: _pick(values) for name, values in byName.items()}


def _propertyUnits(properties: list[OntologyProperty]) -> dict[str, str | None]:
    byName: dict[str, list[str | None]] = {}
    for prop in properties:
        byName.setdefault(prop.property_name.strip().upper(), []).append(prop.unit)
    return {name: _pick(values) for name, values in byName.items()}


def _labelSource(properties: list[OntologyProperty], name: str, label: str) -> str:
    for prop in properties:
        if prop.property_name.strip().upper() != name:
            continue
        if prop.property_alias == label:
            return SOURCE_ONTOLOGY_ALIAS
    return SOURCE_BUSINESS_ALIAS


def _numericColumns(
    columns: list[str], rows: list[dict[str, Any]]
) -> set[str]:
    numeric: set[str] = set()
    for column in columns:
        for row in rows:
            value = row.get(column)
            if value is None:
                continue
            if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
                numeric.add(column)
            break
    return numeric


def _normalizedNames(values: Iterable[str]) -> set[str]:
    return {value.strip().upper() for value in values if value and value.strip()}


def _addUnits(
    buckets: dict[str, list[str]], keys: tuple[str | None, ...], unit: str
) -> None:
    for key in keys:
        if key and key.strip():
            buckets.setdefault(key.strip().upper(), []).append(unit)


def _pickUnits(buckets: dict[str, list[str]]) -> dict[str, str]:
    """多行歧义（同名不同单位）⇒ 该名字不提供单位。"""
    picked: dict[str, str] = {}
    for name, values in buckets.items():
        unit = _pick(values)
        if unit:
            picked[name] = unit
    return picked


async def _loadUnitCatalog(
    session: AsyncSession, names: set[str]
) -> dict[str, str]:
    """KPI/特征单位：按归一后的名字精确匹配（不做模糊匹配）。"""
    if not names:
        return {}
    buckets: dict[str, list[str]] = {}
    kpi_rows = await session.execute(
        select(KpiCatalog.kpi_code, KpiCatalog.kpi_name, KpiCatalog.unit).where(
            KpiCatalog.unit.is_not(None),
            or_(
                func.upper(KpiCatalog.kpi_code).in_(names),
                func.upper(KpiCatalog.kpi_name).in_(names),
            ),
        )
    )
    for code, name, unit in kpi_rows.all():
        _addUnits(buckets, (code, name), unit)
    feature_rows = await session.execute(
        select(
            FeatureDefinition.feature_name,
            FeatureDefinition.feature_alias,
            FeatureDefinition.unit,
        ).where(
            FeatureDefinition.unit.is_not(None),
            or_(
                func.upper(FeatureDefinition.feature_name).in_(names),
                func.upper(FeatureDefinition.feature_alias).in_(names),
            ),
        )
    )
    for name, alias, unit in feature_rows.all():
        _addUnits(buckets, (name, alias), unit)
    return _pickUnits(buckets)


def _dictLabel(source_tables: set[str], column: str) -> str | None:
    if len(source_tables) != 1:
        return None
    table = next(iter(source_tables)).strip().upper()
    return DISPLAY_COLUMN_DICT.get((table, column.strip().upper()))


async def _resolve(
    session: AsyncSession,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    class_names: list[str],
    source_table: str | None,
    plan: QueryPlan | None,
) -> dict[str, FieldDisplay]:
    classes = await _loadClasses(session, class_names, source_table)
    if not classes:
        return {}
    properties = await _loadProperties(session, [cls.id for cls in classes])
    labels = _propertyLabels(properties)
    units = _propertyUnits(properties)
    plan_aliases = _planAliasByColumn(plan, columns)
    source_tables = {
        cls.source_table.strip().upper()
        for cls in classes
        if cls.source_table and cls.source_table.strip()
    }
    numeric = _numericColumns(columns, rows)
    candidate_names = _normalizedNames(
        list(columns) + list(plan_aliases.values())
    )
    catalog_units = await _loadUnitCatalog(session, candidate_names)

    displays: dict[str, FieldDisplay] = {}
    for column in columns:
        name = column.strip().upper()
        ontology_label = labels.get(name)
        plan_label = plan_aliases.get(column)
        if ontology_label:
            label, source = ontology_label, _labelSource(properties, name, ontology_label)
        elif plan_label:
            label, source = plan_label, SOURCE_PLAN_ALIAS
        else:
            dict_label = _dictLabel(source_tables, column)
            label, source = dict_label, (SOURCE_DICT if dict_label else SOURCE_NONE)

        unit = units.get(name)
        if not unit:
            unit = catalog_units.get(name) or catalog_units.get(
                (plan_label or "").strip().upper()
            )
        displays[column] = FieldDisplay(
            label=label,
            unit=unit or None,
            isNumeric=column in numeric,
            source=source,
        )
    return displays


def missingFieldHints(
    columnLabels: Mapping[str, str], displays: Mapping[str, FieldDisplay]
) -> dict[str, list[str]]:
    """未解析出中文名的列 + 无单位的数值列（按输入顺序，便于稳定断言）。"""
    missing_labels = [
        column
        for column, display in displays.items()
        if display.label is None and column not in columnLabels
    ]
    missing_units = [
        column
        for column, display in displays.items()
        if display.isNumeric and display.unit is None
    ]
    return {"missingLabels": missing_labels, "missingUnits": missing_units}
