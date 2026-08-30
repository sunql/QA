"""lineage_extractor（Phase 2.2）。

从 ontology 数据（OntologyJoin + OntologyMetric.formula）+ schema introspection 推断 DataLineage 边。

覆盖规则：
1. JOIN → 字段级血缘（每对 (src_cols[i], tgt_cols[i]) 产生一条边）
2. Metric formula → 源端列引用 + 目标端 metric 聚合层血缘
3. schema introspection → SYSTEM→ODS 层边（表级 CDC 接入：源表 → ODS_ 前缀物理表）

层分配策略（Phase 2.2 启发式）：
- 表名按前缀约定推断层（ODS_/DWD_/DWS_/ADS_ → 对应分层），其余回落 SOURCE_SYSTEM
- 目标层视上下文而定：
  - JOIN：两侧各按 layerForSourceTable 推断（跨层 JOIN 自然产跨层边）
  - Metric：目标层 = KPI（聚合指标）
  - schema introspection：目标层 = ODS（贴源 CDC 接入，物理 ODS_ 表）
- 无前缀匹配的表（如 DIM_）仍归 SOURCE_SYSTEM，避免过度归类

重复边去重：
- 同一上下游 + 字段组合只生成一次（in-memory 去重 + 与现有 data_lineage 双向查重）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import LineageLayer, RefreshFrequency
from app.domain.models import DataLineage, OntologyClass, OntologyJoin, OntologyMetric, SchemaCache
from app.services.formula_parser import parseFormula

logger = logging.getLogger(__name__)


# ---- 配置：source_table → 业务系统简称的映射 ----
# Phase 2.2 默认全部归为 ERP；Phase 3+ 按 schema 或 class_name 前缀细分。
_SOURCE_SYSTEM_FOR_LAYER: dict[LineageLayer, str] = {
    LineageLayer.SOURCE_SYSTEM: "ERP",
}

# 表名前缀 → 分层约定（计划 Change 2.2 阶段 3：从 schema introspection 补 SYSTEM→ODS 层边）。
# 匹配顺序无关（前缀互斥）；无前缀匹配回落 SOURCE_SYSTEM。
_LAYER_PREFIXES: tuple[tuple[str, LineageLayer], ...] = (
    ("ODS_", LineageLayer.ODS),
    ("DWD_", LineageLayer.DWD),
    ("DWS_", LineageLayer.DWS),
    ("ADS_", LineageLayer.ADS),
)

# ODS 层边 transformation_rule：贴源 CDC 原样接入
_ODS_CDC_RULE = "CDC 原样接入"


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


def layerForSourceTable(sourceTable: str) -> LineageLayer:
    """根据 source_table 推断所属层。

    按表名前缀约定推断（计划 Change 2.2 阶段 3）：
    - ODS_ / DWD_ / DWS_ / ADS_ 前缀 → 对应分层（数仓分层物理表）
    - 其余（含 DIM_ 与无前缀业务表）→ SOURCE_SYSTEM（贴源层）

    大小写不敏感：表名统一转大写后匹配前缀。
    """
    upper = (sourceTable or "").upper()
    for prefix, layer in _LAYER_PREFIXES:
        if upper.startswith(prefix):
            return layer
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


async def _loadSchemaTableNames(session: AsyncSession) -> set[str]:
    """载入 schema introspection 缓存中的全部物理表名（计划 2.2 阶段 3）。

    供 _edgesFromSystemToOds 判断 ODS 表与源表是否存在。best-effort 降级：
    - schema_cache 不可用（迁移未跑 / 连接失败）→ 记 WARN + rollback 清事务 + 空集
      （不 rollback 会让 PG 事务进入 aborted 态，调用方后续 commit 报 InFailedSQLTransactionError）
    - 单行 schema_data 畸形（非 dict）→ 只跳过该行，保留其余有效表名
    """
    try:
        stmt = select(SchemaCache)
        result = await session.execute(stmt)
        tables: set[str] = set()
        for cache in result.scalars().all():
            for table in cache.schema_data or []:
                if not isinstance(table, dict):
                    logger.warning("schema_cache 含非 dict 行，跳过: %r", table)
                    continue
                name = table.get("table_name")
                if name:
                    tables.add(name)
        return tables
    except (ProgrammingError, OperationalError) as exc:
        # introspection 不可用不阻断血缘提取；但必须先 rollback 再返回，
        # 否则残留 aborted 事务污染调用方后续写入。
        logger.warning("schema introspection 缓存不可用，跳过 SYSTEM→ODS 层边: %s", exc)
        await session.rollback()
        return set()


def _edgesFromSystemToOds(schemaTables: set[str]) -> list[ExtractedEdge]:
    """从 schema introspection 物理表清单补 SYSTEM→ODS 层边（计划 2.2 阶段 3）。

    规则：对每个以 ODS_ 开头的物理表，去掉前缀得源表名（ODS_PORDER → PORDER）；
    若源表也存在于 schema（贴源 CDC 来源真实），生成表级 CDC 边：
        SOURCE_SYSTEM.PORDER → ODS.ODS_PORDER（transformation_rule=CDC 原样接入）

    大小写不敏感：统一转大写做前缀匹配与源表存在性判断；
    输出保留 schema 原始拼写（PG 带引号小写标识符场景也正确）。
    表级边：source_field / target_field 均为 None。
    """
    upper = {t.upper(): t for t in schemaTables if t}
    edges: list[ExtractedEdge] = []
    for upper_name in sorted(upper):
        if not upper_name.startswith("ODS_"):
            continue
        source_upper = upper_name[len("ODS_"):]
        if not source_upper or source_upper not in upper:
            continue  # 源表为空串（表名恰为 ODS_）或不在 schema → 不能凭空造上游
        edges.append(
            ExtractedEdge(
                source_layer=LineageLayer.SOURCE_SYSTEM,
                source_system=_systemForLayer(LineageLayer.SOURCE_SYSTEM),
                source_object=upper[source_upper],
                source_field=None,
                target_layer=LineageLayer.ODS,
                target_system=_systemForLayer(LineageLayer.SOURCE_SYSTEM),
                target_object=upper[upper_name],
                target_field=None,
                transformation_rule=_ODS_CDC_RULE,
                refresh_frequency=RefreshFrequency.REALTIME,
            )
        )
    return edges


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
    schemaTables = await _loadSchemaTableNames(session)

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

    # 计划 2.2 阶段 3：schema introspection 补 SYSTEM→ODS 层边（表级 CDC）
    for edge in _edgesFromSystemToOds(schemaTables):
        key = _edgeIdentity(edge)
        if key in seen:
            continue
        seen.add(key)
        edges.append(edge)

    return edges