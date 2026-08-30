"""lineage_extractor（Phase 2.2）。

从 ontology 数据（OntologyJoin + OntologyMetric.formula）推断 DataLineage 边。

覆盖规则：
1. JOIN → 字段级血缘（每对 (src_cols[i], tgt_cols[i]) 产生一条边）
2. Metric formula → 源端列引用 + 目标端 metric 聚合层血缘

层分配策略（Phase 2.2 启发式）：
- 当前 ontology 数据全部为 SOURCE_SYSTEM 层（来自 ERP/SRM/WMS 业务表）
- 目标层视上下文而定：
  - JOIN：两边均为 SOURCE_SYSTEM（跨业务表 JOIN 视为逻辑视图）
  - Metric：目标层 = KPI（聚合指标）
- Phase 3+ ODS/DWD 层落地后，扩展 layerForSourceTable 函数即可启用更多层

重复边去重：
- 同一上下游 + 字段组合只生成一次（in-memory 去重 + 与现有 data_lineage 双向查重）
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LineageLayer, RefreshFrequency
from app.domain.models import DataLineage, OntologyClass, OntologyJoin, OntologyMetric
from app.services.formula_parser import parseFormula


# ---- 配置：source_table → 业务系统简称的映射 ----
# Phase 2.2 默认全部归为 ERP；Phase 3+ 按 schema 或 class_name 前缀细分。
_SOURCE_SYSTEM_FOR_LAYER: dict[LineageLayer, str] = {
    LineageLayer.SOURCE_SYSTEM: "ERP",
}


@dataclass(frozen=True)
class ExtractedEdge:
    """抽取出的血缘边（待写入 data_lineage）。"""

    source_layer: LineageLayer
    source_system: str
    source_object: str
    source_field: str | None
    target_layer: LineageLayer
    target_system: str
    target_object: str
    target_field: str | None
    transformation_rule: str | None
    refresh_frequency: RefreshFrequency


def layerForSourceTable(_sourceTable: str) -> LineageLayer:
    """根据 source_table 推断所属层。

    Phase 2.2：所有已知业务表 → SOURCE_SYSTEM。
    Phase 3+ 扩展点：可通过 source_table 前缀或 ontology class metadata 推断层。
    """
    return LineageLayer.SOURCE_SYSTEM


def _systemForLayer(layer: LineageLayer) -> str:
    """根据层推断业务系统简称。"""
    return _SOURCE_SYSTEM_FOR_LAYER.get(layer, "ERP")


def _edgeIdentity(edge: ExtractedEdge) -> tuple:
    """血缘边身份键：与 DataLineageService._edgeIdentityKey 保持一致。"""
    return (
        edge.source_layer,
        edge.source_system,
        edge.source_object,
        edge.source_field,
        edge.target_layer,
        edge.target_system,
        edge.target_object,
        edge.target_field,
    )


async def _loadClasses(
    session: AsyncSession,
) -> dict[int, OntologyClass]:
    """载入所有 OntologyClass（按 id 索引）。"""
    stmt = select(OntologyClass)
    result = await session.execute(stmt)
    return {c.id: c for c in result.scalars().all()}


async def _loadJoins(session: AsyncSession) -> list[OntologyJoin]:
    """载入所有 OntologyJoin。"""
    stmt = select(OntologyJoin)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _loadMetrics(session: AsyncSession) -> list[OntologyMetric]:
    """载入所有 OntologyMetric。"""
    stmt = select(OntologyMetric)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _loadExistingEdges(session: AsyncSession) -> set[tuple]:
    """载入已存在的血缘边身份集合（去重写入用）。"""
    stmt = select(DataLineage)
    result = await session.execute(stmt)
    existing: set[tuple] = set()
    for edge in result.scalars().all():
        existing.add(
            (
                edge.source_layer,
                edge.source_system,
                edge.source_object,
                edge.source_field,
                edge.target_layer,
                edge.target_system,
                edge.target_object,
                edge.target_field,
            )
        )
    return existing


def _edgesFromJoin(
    join: OntologyJoin,
    classes: dict[int, OntologyClass],
) -> list[ExtractedEdge]:
    """从单条 OntologyJoin 抽取血缘边。

    多列 JOIN：每对 (src_cols[i], tgt_cols[i]) 产生一条边。
    """
    src_class = classes.get(join.source_class_id)
    tgt_class = classes.get(join.target_class_id)
    if src_class is None or tgt_class is None:
        return []
    if not src_class.source_table or not tgt_class.source_table:
        return []

    src_layer = layerForSourceTable(src_class.source_table)
    tgt_layer = layerForSourceTable(tgt_class.source_table)
    src_system = _systemForLayer(src_layer)
    tgt_system = _systemForLayer(tgt_layer)

    # transformation_rule 描述 JOIN 来源（foreign_key / business）与说明
    desc = f" JOIN: {join.description}" if join.description else ""
    rule = f"JOIN[{join.relation_type}]{desc}"

    edges: list[ExtractedEdge] = []
    src_cols = join.source_columns or []
    tgt_cols = join.target_columns or []
    # 配对：zip 自动截断到较短列，避免错位
    for src_col, tgt_col in zip(src_cols, tgt_cols, strict=False):
        edges.append(
            ExtractedEdge(
                source_layer=src_layer,
                source_system=src_system,
                source_object=src_class.source_table,
                source_field=src_col,
                target_layer=tgt_layer,
                target_system=tgt_system,
                target_object=tgt_class.source_table,
                target_field=tgt_col,
                transformation_rule=rule,
                refresh_frequency=RefreshFrequency.DAILY,
            )
        )
    return edges


def _edgesFromMetric(
    metric: OntologyMetric,
    classes: dict[int, OntologyClass],
) -> list[ExtractedEdge]:
    """从单条 OntologyMetric 抽取血缘边。

    解析 formula 中的列引用：每列生成一条「源端 = source_table → 目标端 = KPI.metric」边。
    """
    if not metric.target_class_id:
        return []
    target_class = classes.get(metric.target_class_id)
    if target_class is None or not target_class.source_table:
        return []

    parsed = parseFormula(metric.formula)
    if not parsed.column_refs:
        return []

    # 目标端：KPI 层 + 目标类的 source_table
    # 当前目标对象名：metric_name（业务标识）+ 聚合
    target_object = f"KPI_{metric.metric_name}"

    # 源端：因 column_refs 中 alias 不一定对应 ontology class，
    # 本期采用简化策略：所有 source_field 均指向 target_class.source_table（同表多 metric 场景）。
    # Phase 2.3 扩展点：基于 OntologyJoin 解析 alias → class 映射
    src_layer = layerForSourceTable(target_class.source_table)
    src_system = _systemForLayer(src_layer)

    edges: list[ExtractedEdge] = []
    for alias, column in parsed.column_refs:
        edges.append(
            ExtractedEdge(
                source_layer=src_layer,
                source_system=src_system,
                source_object=target_class.source_table,
                source_field=column,
                target_layer=LineageLayer.KPI,
                target_system="KPI",
                target_object=target_object,
                target_field=metric.metric_name,
                transformation_rule=metric.formula,
                refresh_frequency=RefreshFrequency.DAILY,
            )
        )
    return edges


async def extractEdges(session: AsyncSession) -> list[ExtractedEdge]:
    """从 ontology 抽取血缘边。

    Args:
        session: AsyncSession

    Returns:
        待写入 data_lineage 的边集合（已去重：与现有 + 同 run 内均不重复）。

    Notes:
        不抛错：单条 metric 解析失败不阻断其他 metric；缺失 class 引用跳过对应 join。
    """
    classes = await _loadClasses(session)
    joins = await _loadJoins(session)
    metrics = await _loadMetrics(session)
    existing = await _loadExistingEdges(session)

    edges: list[ExtractedEdge] = []
    seen: set[tuple] = set(existing)

    for join in joins:
        for edge in _edgesFromJoin(join, classes):
            key = _edgeIdentity(edge)
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)

    for metric in metrics:
        try:
            for edge in _edgesFromMetric(metric, classes):
                key = _edgeIdentity(edge)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(edge)
        except Exception:  # noqa: BLE001
            # 单 metric 解析失败不阻断；运维侧从日志/警告发现
            continue

    return edges