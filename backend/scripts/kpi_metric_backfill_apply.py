"""KPI 指标技术形态回填的**落库层**（物理列校验 + service 链路写入）。

拆分理由见 T15-I：单文件 916 行 > 800 上限，且 ``applyPlans`` 原 125 行 > 50 行
上限 ⇒ 纯数据在 ``kpi_metric_backfill_plan``，落库在本模块，CLI 在
``kpi_metric_backfill``。

四条硬约束（都在本模块落地）：

1. **物理列独立校验兜底**（T15-N / T15-O）：``classColumnIndex`` 从
   ``schema_cache`` 读**真实**列集合（裸表名 + ``owner.table`` 两套键、
   按数据源取并集而不是后者覆盖前者），``verifyPlan`` 用
   ``measureColumnIdentifiers`` 拆表达式（算式不会整串误判缺列）。
2. **class_id 不硬编码**（T15-C）：``classIdByTable`` 按 ``source_table``
   反查 ``ontology_class.id``；查不到 / 一对多 ⇒ 该条**跳过**（宁可少写，
   不可挂错类）。
3. **回填不碰外部副作用**：向量同步默认关闭（T15-G），图谱同步默认关闭
   （T15-F：图谱留给 Task 16 的显式同步步骤）。
4. **不可变**：``ApplySummary`` / ``_PlanOutcome`` 都是 frozen dataclass，
   降级用 ``dataclasses.replace`` 产出新 plan，不原地改。

T15-Q 例外登记：回填脚本没有 API 端点（运维脚本），集成测试直接调
``applyPlans`` 是本任务性质决定的豁免；用「真 PG + 真 service 链路 +
真 ORM 事件 + 每例 TRUNCATE」替代「完整 HTTP 链路」。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    MetricAggregationOrder,
    MetricExecutionSource,
    MetricStatus,
    MetricType,
)
from app.domain.models import (
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    SchemaCache,
)
from app.domain.schemas import (
    KpiCatalogUpdate,
    MeasureSpecSchema,
    OntologyMetricCreate,
    OntologyMetricUpdate,
)
from app.services import ontology_service as ontologyModule
from app.services.acl_service import ADMIN_ROLE
from app.services.kpi_catalog_service import KpiCatalogService
from app.services.nl2sql_semantic_guard import measureColumnIdentifiers
from app.services.ontology_service import OntologyService
from scripts.kpi_metric_backfill_plan import (
    BACKFILL_ACTOR_ID,
    KpiBackfillPlan,
    MeasurePlan,
)

# 图谱同步在 `OntologyService.createMetric` 内联调用，没有开关；
# 回填默认关闭（T15-F：图谱留给 Task 16 的显式同步步骤）。
_GRAPH_SYNC_FUNCTIONS = (
    "upsertMetricNode",
    "linkMetricDerivedFrom",
    "reconcileMetricDerivedFrom",
)


@dataclass(frozen=True)
class ApplySummary:
    """一次回填的结果汇总（幂等重跑时 created 为空、updated 有值）。"""

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    unboundDataSource: tuple[str, ...] = ()
    unresolvedClasses: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PlanOutcome:
    """单条 plan 的落库结局（created / updated / skipped / class-missing）。"""

    status: str
    degraded: bool = False
    unresolved: tuple[str, ...] = ()


def _normalizeTable(name: str | None) -> str:
    """表名归一：大写 + 去掉 ``OWNER.`` 前缀（DW 层 owner 是 ``THBI``）。"""
    return (name or "").rsplit(".", 1)[-1].strip().upper()


def _tableKeys(tableName: str, owner: str | None) -> tuple[str, ...]:
    """同一张表的两种寻址方式（与 ``schema_introspection_service`` 同口径）。"""
    bare = tableName.strip().upper()
    if not bare:
        return ()
    qualified = f"{owner.strip().upper()}.{bare}" if owner and owner.strip() else None
    return (bare, qualified) if qualified else (bare,)


async def classColumnIndex(session: AsyncSession) -> dict[str, frozenset[str]]:
    """构建「表名 → 物理列集合（大写）」的索引。

    来源：``schema_cache.schema_data`` 的 ``table_name`` / ``owner`` /
    ``columns[].column_name``。

    - **两种键**：裸表名（``DWD_X``）与 ``OWNER.TABLE``（``THBI.DWD_X``）——
      本体 ``source_table`` 两种写法都存在（T15-N）；
    - **按数据源取并集**：同名表出现在多个数据源时**不覆盖**（T15-N）。
      并集只会让「列存在」的判定更宽松，不会把已存在的列误判缺失。
    """
    rows = (await session.execute(select(SchemaCache))).scalars().all()
    merged: dict[str, set[str]] = {}
    for row in rows:
        for table in row.schema_data or []:
            tableName = str(table.get("table_name") or "")
            owner = table.get("owner")
            columns = {
                str(col.get("column_name") or "").upper()
                for col in (table.get("columns") or [])
                if col.get("column_name")
            }
            for key in _tableKeys(tableName, owner if isinstance(owner, str) else None):
                merged.setdefault(key, set()).update(columns)
    return {key: frozenset(columns) for key, columns in merged.items()}


def _measureColumns(
    measure: MeasurePlan, columnIndex: Mapping[Any, frozenset[str]]
) -> frozenset[str]:
    """按 source_table / 限定名 / classId 三种方式查列集合；查不到 ⇒ 空集合。"""
    bare = _normalizeTable(measure.classTable)
    for key in (measure.classTable, bare, f"THBI.{bare}"):
        if key in columnIndex:
            return columnIndex[key]
    if measure.classId is not None and measure.classId in columnIndex:
        return columnIndex[measure.classId]
    for key, columns in columnIndex.items():
        if isinstance(key, str) and _normalizeTable(key) == bare:
            return columns
    return frozenset()


def verifyPlan(
    plan: KpiBackfillPlan, columnIndex: Mapping[Any, frozenset[str]]
) -> tuple[str, ...]:
    """校验结构化度量的每个物理列是否真实存在；返回缺失列（空 = 通过）。

    只校验 ``expr``。``filter`` 是自由谓词（可能引用别的类的列）⇒ 不校验，
    由审核清单的人工核对兜住。``expr`` 用 ``measureColumnIdentifiers`` 拆成
    标识符集合（T15-O：算术式不整串比对）。
    """
    missing: list[str] = []
    for measure in (plan.numerator, plan.denominator):
        if measure is None:
            continue
        available = _measureColumns(measure, columnIndex)
        unknown = sorted(measureColumnIdentifiers(measure.expr) - {c.lower() for c in available})
        missing.extend(f"{measure.classAlias}.{column.upper()}" for column in unknown)
    return tuple(missing)


def withoutStructured(plan: KpiBackfillPlan, missing: Sequence[str]) -> KpiBackfillPlan:
    """物理列缺失 ⇒ 降级为「只有口径声明」，并记录降级原因。

    **治理列保留业务原声明**（T15-L）：降级只清空技术侧结构化度量，不清治理列
    ⇒ 层 2 判 mismatch（不是 unknown）⇒ 发布闸门必然拦下 ⇒ 人工核对，
    这正是「不自己给自己签字」的设计。
    """
    reason = "物理列缺失，结构化度量已清空：" + "、".join(missing)
    return replace(
        plan,
        numerator=None,
        denominator=None,
        deviations=plan.deviations + (reason,),
    )


async def classIdByTable(session: AsyncSession) -> tuple[dict[str, int], frozenset[str]]:
    """按 ``source_table`` 归一表名反查 ``ontology_class.id``。

    返回 ``(table → id, 一对多的表名集合)``。一对多的表**不可用于落库**
    （T15-C：宁可跳过也不挂错类）。只 select 两列 ⇒ 对旧库（缺 0136 列）
    也安全。
    """
    rows = (
        await session.execute(select(OntologyClass.id, OntologyClass.source_table))
    ).all()
    byTable: dict[str, int] = {}
    ambiguous: set[str] = set()
    for classId, sourceTable in rows:
        key = _normalizeTable(sourceTable)
        if not key:
            continue
        if key in byTable and byTable[key] != classId:
            ambiguous.add(key)
            continue
        byTable[key] = classId
    return byTable, frozenset(ambiguous)


def _resolveClassRefs(
    plan: KpiBackfillPlan,
    byTable: Mapping[str, int],
    ambiguous: Iterable[str],
) -> tuple[dict[str, int], tuple[str, ...]]:
    """把 `(source_table, role)` 解析成 `table → class_id`；返回未解析项。"""
    blocked = set(ambiguous)
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for tableName, role in plan.classRefs:
        key = _normalizeTable(tableName)
        if key in blocked or key not in byTable:
            unresolved.append(f"{tableName}（{role}）")
            continue
        resolved[tableName] = byTable[key]
    return resolved, tuple(unresolved)


def _measureSchema(
    measure: MeasurePlan | None, resolved: Mapping[str, int]
) -> MeasureSpecSchema | None:
    """构造 DTO 用的度量；class_id 由落库前的解析结果注入（T15-C）。"""
    if measure is None:
        return None
    return measure.toSchema(classId=resolved.get(measure.classTable))


def _createDto(plan: KpiBackfillPlan, resolved: Mapping[str, int]) -> OntologyMetricCreate:
    return OntologyMetricCreate(
        metric_name=plan.metricName,
        metric_alias=plan.kpiCode,
        formula=plan.formula,
        agg_function=plan.aggFunction,
        created_by=BACKFILL_ACTOR_ID,
        metric_type=MetricType(plan.metricType),
        numerator_measure=_measureSchema(plan.numerator, resolved),
        denominator_measure=_measureSchema(plan.denominator, resolved),
        aggregation_order=MetricAggregationOrder(plan.aggregationOrder),
        grain_keys=list(plan.grainKeys),
        status=MetricStatus.DRAFT,
        owner=BACKFILL_ACTOR_ID,
        execution_source=MetricExecutionSource(plan.executionSource),
    )


def _updateDto(
    plan: KpiBackfillPlan, resolved: Mapping[str, int], *, keepStatus: bool
) -> OntologyMetricUpdate:
    """更新 DTO。已 PUBLISHED 的既有指标**不降级**（只补结构化字段）。"""
    values: dict[str, Any] = {
        "metric_name": plan.metricName,
        "metric_alias": plan.kpiCode,
        "formula": plan.formula,
        "agg_function": plan.aggFunction,
        "metric_type": MetricType(plan.metricType),
        "numerator_measure": _measureSchema(plan.numerator, resolved),
        "denominator_measure": _measureSchema(plan.denominator, resolved),
        "aggregation_order": MetricAggregationOrder(plan.aggregationOrder),
        "grain_keys": list(plan.grainKeys),
        "owner": BACKFILL_ACTOR_ID,
        "execution_source": MetricExecutionSource(plan.executionSource),
    }
    if not keepStatus:
        values["status"] = MetricStatus.DRAFT
    return OntologyMetricUpdate(**values)


async def _findMetric(
    session: AsyncSession, kpi: KpiCatalog, metricName: str
) -> OntologyMetric | None:
    """定位既有指标：先按 KPI 的 ``metric_id``，再按 ``metric_name``（唯一）。"""
    if kpi.metric_id is not None:
        metric = await session.get(OntologyMetric, kpi.metric_id)
        if metric is not None:
            return metric
    return (
        await session.execute(
            select(OntologyMetric).where(OntologyMetric.metric_name == metricName)
        )
    ).scalar_one_or_none()


async def _writeSources(
    session: AsyncSession,
    metricId: int,
    plan: KpiBackfillPlan,
    resolved: Mapping[str, int],
) -> None:
    """把 ``ontology_metric_source`` 对齐到 plan 的类映射（幂等）。"""
    existing = (
        await session.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == metricId
            )
        )
    ).scalars().all()
    wanted = {(resolved[table], role) for table, role in plan.classRefs if table in resolved}
    for row in existing:
        if (row.class_id, row.role) not in wanted:
            await session.delete(row)
    present = {(row.class_id, row.role) for row in existing}
    for classId, role in sorted(wanted - present):
        session.add(
            OntologyMetricSource(metric_id=metricId, class_id=classId, role=role)
        )
    await session.flush()


async def _linkKpi(
    session: AsyncSession,
    kpiService: KpiCatalogService,
    kpi: KpiCatalog,
    metricId: int,
    plan: KpiBackfillPlan,
    actor: Any,
) -> None:
    """KPI 挂 metric_id + 写**业务原声明**的治理列（T15-L）。不改 status。"""
    await kpiService.updateKpi(
        session,
        kpi.id,
        KpiCatalogUpdate(
            metric_id=metricId,
            grain_keys=list(plan.governanceGrain),
            numerator_columns=list(plan.governanceNumerator),
            denominator_columns=list(plan.governanceDenominator),
        ),
        actor,
    )


@contextmanager
def _graphSyncSuppressed() -> Any:
    """临时关闭本体图谱同步（T15-F）。

    `OntologyService.createMetric` 内联调用 ``neo4j.upsertMetricNode`` 且没有
    开关；回填是批量合成候选，图谱同步留给 Task 16 的显式步骤 ⇒ 调用期间
    把这几个模块级函数换成 no-op，``finally`` 无条件还原。
    """
    originals = {
        name: getattr(ontologyModule.neo4j, name) for name in _GRAPH_SYNC_FUNCTIONS
    }
    for name in _GRAPH_SYNC_FUNCTIONS:
        setattr(ontologyModule.neo4j, name, lambda *args, **kwargs: None)
    try:
        yield
    finally:
        for name, original in originals.items():
            setattr(ontologyModule.neo4j, name, original)


class _NoEmbeddingOntologyService(OntologyService):
    """回填专用：默认不同步指标向量（T15-G，避免真连 Milvus + 调 embedding）。"""

    async def _syncMetricEmbeddingBestEffort(self, entity: Any) -> None:  # noqa: ARG002
        return None


def _backfillActor() -> Any:
    """脚本用系统身份（admin）—— 回填是运维动作，不归属某个自然人。

    ``KpiCatalogService.updateKpi`` 的 ACL 走 ``assertCanModify``：
    owner 不匹配且非 admin ⇒ PermissionDeniedError ⇒ 必须带 admin 角色。
    """
    from app.dependencies import CurrentUser

    return CurrentUser(userId=BACKFILL_ACTOR_ID, roles=(ADMIN_ROLE,), departments=())


async def _applyOne(
    session: AsyncSession,
    plan: KpiBackfillPlan,
    *,
    columnIndex: Mapping[Any, frozenset[str]],
    classTables: tuple[Mapping[str, int], Iterable[str]],
    ontologyService: OntologyService,
    kpiService: KpiCatalogService,
    actor: Any,
) -> _PlanOutcome:
    """单条 plan 的落库：校验 → 解析类 → upsert 指标 → 对齐 source → 挂 KPI。"""
    byTable, ambiguous = classTables
    resolved, unresolved = _resolveClassRefs(plan, byTable, ambiguous)
    if unresolved:
        return _PlanOutcome("class-missing", unresolved=unresolved)

    kpi = (
        await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == plan.kpiCode)
        )
    ).scalar_one_or_none()
    if kpi is None:
        return _PlanOutcome("skipped")

    missing = verifyPlan(plan, columnIndex)
    effective = withoutStructured(plan, missing) if missing else plan

    metric = await _findMetric(session, kpi, effective.metricName)
    if metric is None:
        metric = await ontologyService.createMetric(
            session, _createDto(effective, resolved), actor=BACKFILL_ACTOR_ID
        )
        status = "created"
    else:
        keepStatus = metric.status == MetricStatus.PUBLISHED.value
        metric = await ontologyService.updateMetric(
            session,
            metric.id,
            _updateDto(effective, resolved, keepStatus=keepStatus),
            actor=BACKFILL_ACTOR_ID,
        )
        status = "updated"

    await _writeSources(session, metric.id, effective, resolved)
    await _linkKpi(session, kpiService, kpi, metric.id, effective, actor)
    return _PlanOutcome(status, degraded=bool(missing))


async def applyPlans(
    session: AsyncSession,
    plans: Sequence[KpiBackfillPlan],
    *,
    ontologyService: OntologyService | None = None,
    syncEmbedding: bool = False,
) -> ApplySummary:
    """把候选技术形态落库（幂等；全部走 service，不写裸 SQL）。

    默认关闭向量同步（T15-G）与图谱同步（T15-F）；``syncEmbedding=True`` 供
    运维显式开启（CLI ``--embed``）。
    """
    columnIndex = await classColumnIndex(session)
    classTables = await classIdByTable(session)
    service = ontologyService or (
        OntologyService() if syncEmbedding else _NoEmbeddingOntologyService()
    )
    kpiService = KpiCatalogService()
    actor = _backfillActor()

    created: list[str] = []
    updated: list[str] = []
    degraded: list[str] = []
    skipped: list[str] = []
    unbound: list[str] = []
    unresolved: list[str] = []

    with _graphSyncSuppressed():
        for plan in plans:
            outcome = await _applyOne(
                session,
                plan,
                columnIndex=columnIndex,
                classTables=classTables,
                ontologyService=service,
                kpiService=kpiService,
                actor=actor,
            )
            if outcome.degraded:
                degraded.append(plan.kpiCode)
            if outcome.status == "created":
                created.append(plan.kpiCode)
            elif outcome.status == "updated":
                updated.append(plan.kpiCode)
            else:
                skipped.append(plan.kpiCode)
            unresolved.extend(outcome.unresolved)
            await _noteUnbound(session, plan, unbound)

    await session.commit()
    return ApplySummary(
        created=tuple(created),
        updated=tuple(updated),
        degraded=tuple(degraded),
        skipped=tuple(skipped),
        unboundDataSource=tuple(unbound),
        unresolvedClasses=tuple(unresolved),
    )


async def _noteUnbound(
    session: AsyncSession, plan: KpiBackfillPlan, bucket: list[str]
) -> None:
    """记录未绑定数据源的 KPI（发布闸门会拦，需人工绑定）。"""
    datasourceId = (
        await session.execute(
            select(KpiCatalog.datasource_id).where(
                KpiCatalog.kpi_code == plan.kpiCode
            )
        )
    ).scalar_one_or_none()
    if datasourceId is None:
        bucket.append(plan.kpiCode)
