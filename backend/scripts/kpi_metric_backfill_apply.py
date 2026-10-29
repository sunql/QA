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

破坏性更新语义（FIX-I1）
------------------------
- **默认非破坏性**：对已存在的指标只填 ``NULL`` / 空值字段；已有非空值**不覆盖**。
  来源行默认只**追加缺失**，不删除 plan 之外的任何行；KPI 治理列同理。
- **``overwrite=True``**（CLI ``--overwrite``）才全量重写 + 删除 plan 之外的
  source 行。若目标指标当前是 ``PUBLISHED``，写完**回退为 ``DRAFT``**
  （``reverted_to_draft``），必须重新发布并重新过发布闸门 —— 不允许保留
  ``PUBLISHED`` 却写出未经闸门的形态。

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
    """一次回填的结果汇总（幂等重跑时 created 为空、updated 有值）。

    字段统一 snake_case（M-5 / T15-U），与本仓 ORM / Pydantic 字段口径一致。
    """

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    unbound_data_source: tuple[str, ...] = ()
    unresolved_classes: tuple[str, ...] = ()
    reverted_to_draft: tuple[str, ...] = ()


@dataclass(frozen=True)
class _PlanOutcome:
    """单条 plan 的落库结局（created / updated / skipped / class-missing）。"""

    status: str
    degraded: bool = False
    unresolved: tuple[str, ...] = ()
    reverted: bool = False


@dataclass(frozen=True)
class _ResolvedClass:
    """``source_table`` ⇒ ``(id, 原样 source_table)``（M-1：断言别名一致用）。"""

    class_id: int
    source_table: str


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


def _columnsFor(tableName: str, columnIndex: Mapping[Any, frozenset[str]]) -> frozenset[str]:
    """按 source_table / 限定名 / 别名归一三种方式查列集合；查不到 ⇒ 空集合。"""
    bare = _normalizeTable(tableName)
    for key in (tableName, bare, f"THBI.{bare}"):
        if key in columnIndex:
            return columnIndex[key]
    for key, columns in columnIndex.items():
        if isinstance(key, str) and _normalizeTable(key) == bare:
            return columns
    return frozenset()


def _measureColumns(
    measure: MeasurePlan, columnIndex: Mapping[Any, frozenset[str]]
) -> frozenset[str]:
    """度量所在类的列集合（classId 键是旧口径的兜底，见 T15-N 取舍）。"""
    columns = _columnsFor(measure.classTable, columnIndex)
    if not columns and measure.classId is not None and measure.classId in columnIndex:
        return columnIndex[measure.classId]
    return columns


def verifyPlan(
    plan: KpiBackfillPlan, columnIndex: Mapping[Any, frozenset[str]]
) -> tuple[str, ...]:
    """校验物理列是否真实存在；返回缺失列（空 = 通过）。

    - 度量 ``expr``：用 ``measureColumnIdentifiers`` 拆标识符（T15-O）；
    - ``grain_keys``：M-6 要求至少做存在性校验 —— 只在**有结构化度量**的 plan
      上校验（C 档 / NONE 档没有物理源，粒度键无从校验，不应误报降级）；
    - ``filter`` 是自由谓词（可能引用别的类的列）⇒ 不校验，由审核清单人工核对。

    返回标识符形如 ``TABLE.COLUMN`` / ``GRAIN.KEY``（后者前缀区分来源）。
    """
    missing: list[str] = []
    for measure in (plan.numerator, plan.denominator):
        if measure is None:
            continue
        available = _measureColumns(measure, columnIndex)
        unknown = sorted(measureColumnIdentifiers(measure.expr) - {c.lower() for c in available})
        missing.extend(f"{measure.classAlias}.{column.upper()}" for column in unknown)
    missing.extend(_missingGrainKeys(plan, columnIndex))
    return tuple(missing)


def _missingGrainKeys(
    plan: KpiBackfillPlan, columnIndex: Mapping[Any, frozenset[str]]
) -> list[str]:
    """粒度键的存在性校验（无结构化度量 ⇒ 跳过：没有物理源可比）。"""
    if plan.numerator is None or not plan.grainKeys:
        return []
    available: set[str] = set()
    for table, _role in plan.classRefs:
        available |= _columnsFor(table, columnIndex)
    for measure in (plan.numerator, plan.denominator):
        if measure is not None:
            available |= _measureColumns(measure, columnIndex)
    return [f"GRAIN.{key}" for key in plan.grainKeys if key.upper() not in available]


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


async def classIdByTable(
    session: AsyncSession,
) -> tuple[dict[str, _ResolvedClass], frozenset[str]]:
    """按 ``source_table`` 归一表名反查 ``ontology_class``。

    返回 ``(table → (id, source_table), 一对多的表名集合)``。一对多的表
    **不可用于落库**（T15-C：宁可跳过也不挂错类）。只 select 两列 ⇒ 对旧库
    （缺 0136 列）也安全。
    """
    rows = (
        await session.execute(select(OntologyClass.id, OntologyClass.source_table))
    ).all()
    byTable: dict[str, _ResolvedClass] = {}
    ambiguous: set[str] = set()
    for classId, sourceTable in rows:
        key = _normalizeTable(sourceTable)
        if not key:
            continue
        existing = byTable.get(key)
        if existing is not None and existing.class_id != classId:
            ambiguous.add(key)
            continue
        byTable[key] = _ResolvedClass(classId, (sourceTable or "").strip())
    return byTable, frozenset(ambiguous)


def _resolveClassRefs(
    plan: KpiBackfillPlan,
    byTable: Mapping[str, _ResolvedClass],
    ambiguous: Iterable[str],
) -> tuple[dict[str, _ResolvedClass], tuple[str, ...]]:
    """把 `(source_table, role)` 解析成 `table → 类`；返回未解析项。"""
    blocked = set(ambiguous)
    resolved: dict[str, _ResolvedClass] = {}
    unresolved: list[str] = []
    for tableName, role in plan.classRefs:
        key = _normalizeTable(tableName)
        if key in blocked or key not in byTable:
            unresolved.append(f"{tableName}（{role}）")
            continue
        resolved[tableName] = byTable[key]
    return resolved, tuple(unresolved)


def _resolvedFor(
    tableName: str, resolved: Mapping[str, _ResolvedClass]
) -> _ResolvedClass | None:
    """按归一表名查解析结果（plan 里的写法可能是裸名或 OWNER.TABLE）。"""
    key = _normalizeTable(tableName)
    for name, entry in resolved.items():
        if _normalizeTable(name) == key:
            return entry
    return None


def _measureProblems(
    plan: KpiBackfillPlan, resolved: Mapping[str, _ResolvedClass]
) -> tuple[str, ...]:
    """M-1：度量必须能解析出 class_id，且类 ``source_table`` 与 ``classAlias`` 一致。

    任一不满足 ⇒ 返回问题描述（调用方跳过并计数），**不静默写 class_id=None**。
    """
    problems: list[str] = []
    for measure in (plan.numerator, plan.denominator):
        if measure is None:
            continue
        entry = _resolvedFor(measure.classTable, resolved)
        if entry is None:
            problems.append(f"{measure.classTable}（度量 class_id 未解析）")
        elif _normalizeTable(entry.source_table) != _normalizeTable(measure.classAlias):
            problems.append(
                f"{measure.classTable}（source_table={entry.source_table!r} "
                f"≠ classAlias={measure.classAlias!r}）"
            )
    return tuple(problems)


def _measureSchema(
    measure: MeasurePlan | None, resolved: Mapping[str, _ResolvedClass]
) -> MeasureSpecSchema | None:
    """构造 DTO 用的度量；class_id 由落库前的解析结果注入（T15-C）。

    M-1：解析不到 / 别名不一致 ⇒ **抛错**，绝不静默写 ``class_id=None``
    （正常流程里 ``_measureProblems`` 已提前拦下，这里只是最后一道防线）。
    """
    if measure is None:
        return None
    entry = _resolvedFor(measure.classTable, resolved)
    if entry is None:
        raise ValueError(f"本体类未解析，不能写 class_id=None：{measure.classTable}")
    if _normalizeTable(entry.source_table) != _normalizeTable(measure.classAlias):
        raise ValueError(
            f"本体类 source_table={entry.source_table!r} 与度量 "
            f"classAlias={measure.classAlias!r} 不一致（T15-C）"
        )
    return measure.toSchema(classId=entry.class_id)


def _createDto(
    plan: KpiBackfillPlan, resolved: Mapping[str, _ResolvedClass]
) -> OntologyMetricCreate:
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


def _desiredValues(
    plan: KpiBackfillPlan, resolved: Mapping[str, _ResolvedClass]
) -> dict[str, Any]:
    """plan 期望写进 ``ontology_metric`` 的全部字段（不含 status）。"""
    return {
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


def _isBlank(value: Any) -> bool:
    """空值判定：``None`` / 空串 / 空 list / 空 tuple ⇒ 可被默认模式填充。"""
    return value is None or value == "" or value == [] or value == ()


def _fillDto(
    plan: KpiBackfillPlan,
    resolved: Mapping[str, _ResolvedClass],
    metric: OntologyMetric,
) -> OntologyMetricUpdate | None:
    """FIX-I1 默认模式：只填**空值**字段，已有非空值一律不覆盖。"""
    values = {
        name: value
        for name, value in _desiredValues(plan, resolved).items()
        if _isBlank(getattr(metric, name, None))
    }
    return OntologyMetricUpdate(**values) if values else None


def _updateDto(
    plan: KpiBackfillPlan,
    resolved: Mapping[str, _ResolvedClass],
    *,
    revertPublished: bool,
) -> OntologyMetricUpdate:
    """``overwrite`` 模式的更新 DTO。

    ``revertPublished``（目标当前是 PUBLISHED）⇒ 写完全量重写的形态后把状态
    **降回 DRAFT**，强制重新过发布闸门（FIX-I1 第 3 点）；其余状态不动。
    """
    values = _desiredValues(plan, resolved)
    if revertPublished:
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
    resolved: Mapping[str, _ResolvedClass],
    *,
    overwrite: bool,
) -> None:
    """把 ``ontology_metric_source`` 对齐到 plan 的类映射（幂等）。

    默认（``overwrite=False``）**只追加缺失行，不删除**任何现有行（FIX-I1）；
    只有显式 overwrite 才删除 plan 之外的 source 行。
    """
    existing = (
        await session.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == metricId
            )
        )
    ).scalars().all()
    resolvedByKey = {_normalizeTable(table): entry for table, entry in resolved.items()}
    wanted = {
        (resolvedByKey[_normalizeTable(table)].class_id, role)
        for table, role in plan.classRefs
        if _normalizeTable(table) in resolvedByKey
    }
    present = {(row.class_id, row.role) for row in existing}
    if overwrite:
        for row in existing:
            if (row.class_id, row.role) not in wanted:
                await session.delete(row)
    for classId, role in sorted(wanted - present):
        session.add(
            OntologyMetricSource(metric_id=metricId, class_id=classId, role=role)
        )
    await session.flush()


def _governanceValues(
    plan: KpiBackfillPlan, metricId: int, kpi: KpiCatalog, *, overwrite: bool
) -> dict[str, Any]:
    """KPI 治理列的目标值；默认模式只保留 KPI 上为空的字段（FIX-I1）。"""
    desired: dict[str, Any] = {
        "metric_id": metricId,
        "grain_keys": list(plan.governanceGrain),
        "numerator_columns": list(plan.governanceNumerator),
        "denominator_columns": list(plan.governanceDenominator),
    }
    if overwrite:
        return desired
    return {
        name: value
        for name, value in desired.items()
        if _isBlank(getattr(kpi, name, None))
    }


async def _linkKpi(
    session: AsyncSession,
    kpiService: KpiCatalogService,
    kpi: KpiCatalog,
    metricId: int,
    plan: KpiBackfillPlan,
    actor: Any,
    *,
    overwrite: bool,
) -> None:
    """KPI 挂 metric_id + 写**业务原声明**的治理列（T15-L）。不改 status。"""
    values = _governanceValues(plan, metricId, kpi, overwrite=overwrite)
    if not values:
        return
    await kpiService.updateKpi(
        session, kpi.id, KpiCatalogUpdate(**values), actor
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


async def _upsertMetric(
    session: AsyncSession,
    plan: KpiBackfillPlan,
    resolved: Mapping[str, _ResolvedClass],
    ontologyService: OntologyService,
    kpi: KpiCatalog,
    *,
    overwrite: bool,
) -> tuple[OntologyMetric, str, bool]:
    """新建 / 更新指标；返回 ``(metric, status, revertedToDraft)``。

    - 新建：一律 DRAFT；
    - 默认模式：只填空值（``_fillDto`` 返回 None ⇒ 不调用 service，避免无谓写）；
    - overwrite：全量重写；原先 PUBLISHED 的指标回退为 DRAFT 并标记 reverted。
    """
    metric = await _findMetric(session, kpi, plan.metricName)
    if metric is None:
        created = await ontologyService.createMetric(
            session, _createDto(plan, resolved), actor=BACKFILL_ACTOR_ID
        )
        return created, "created", False
    if overwrite:
        revert = metric.status == MetricStatus.PUBLISHED.value
        updated = await ontologyService.updateMetric(
            session,
            metric.id,
            _updateDto(plan, resolved, revertPublished=revert),
            actor=BACKFILL_ACTOR_ID,
        )
        return updated, "updated", revert
    dto = _fillDto(plan, resolved, metric)
    if dto is None:
        return metric, "updated", False
    updated = await ontologyService.updateMetric(
        session, metric.id, dto, actor=BACKFILL_ACTOR_ID
    )
    return updated, "updated", False


async def _applyOne(
    session: AsyncSession,
    plan: KpiBackfillPlan,
    *,
    columnIndex: Mapping[Any, frozenset[str]],
    classTables: tuple[Mapping[str, _ResolvedClass], Iterable[str]],
    ontologyService: OntologyService,
    kpiService: KpiCatalogService,
    actor: Any,
    overwrite: bool,
) -> _PlanOutcome:
    """单条 plan 的落库：校验 → 解析类 → upsert 指标 → 对齐 source → 挂 KPI。"""
    byTable, ambiguous = classTables
    resolved, unresolved = _resolveClassRefs(plan, byTable, ambiguous)
    problems = unresolved + _measureProblems(plan, resolved)
    if problems:
        return _PlanOutcome("class-missing", unresolved=problems)

    kpi = (
        await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code == plan.kpiCode)
        )
    ).scalar_one_or_none()
    if kpi is None:
        return _PlanOutcome("skipped")

    missing = verifyPlan(plan, columnIndex)
    effective = withoutStructured(plan, missing) if missing else plan

    metric, status, reverted = await _upsertMetric(
        session, effective, resolved, ontologyService, kpi, overwrite=overwrite
    )
    await _writeSources(
        session, metric.id, effective, resolved, overwrite=overwrite
    )
    await _linkKpi(
        session, kpiService, kpi, metric.id, effective, actor, overwrite=overwrite
    )
    return _PlanOutcome(status, degraded=bool(missing), reverted=reverted)


async def applyPlans(
    session: AsyncSession,
    plans: Sequence[KpiBackfillPlan],
    *,
    overwrite: bool = False,
    ontologyService: OntologyService | None = None,
    syncEmbedding: bool = False,
) -> ApplySummary:
    """把候选技术形态落库（幂等；全部走 service，不写裸 SQL）。

    默认**非破坏性**（只填空值、只追加 source 行）；``overwrite=True`` 才全量
    重写 + 删 source + 把 PUBLISHED 指标回退为 DRAFT（FIX-I1）。
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
    reverted: list[str] = []

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
                overwrite=overwrite,
            )
            if outcome.degraded:
                degraded.append(plan.kpiCode)
            if outcome.reverted:
                reverted.append(plan.kpiCode)
            if outcome.status == "created":
                created.append(plan.kpiCode)
            elif outcome.status == "updated":
                updated.append(plan.kpiCode)
            else:
                skipped.append(plan.kpiCode)
            unresolved.extend(outcome.unresolved)
            if outcome.status in ("created", "updated"):
                await _noteUnbound(session, plan, unbound)

    await session.commit()
    return ApplySummary(
        created=tuple(created),
        updated=tuple(updated),
        degraded=tuple(degraded),
        skipped=tuple(skipped),
        unbound_data_source=tuple(unbound),
        unresolved_classes=tuple(unresolved),
        reverted_to_draft=tuple(reverted),
    )


async def _noteUnbound(
    session: AsyncSession, plan: KpiBackfillPlan, bucket: list[str]
) -> None:
    """记录未绑定数据源的 KPI（发布闸门会拦，需人工绑定）。

    M-7：只对**已存在**的 KPI 调用（KPI 行不存在时 ``datasource_id`` 同样是
    None ⇒ 会被误报成「未绑定数据源」）。
    """
    datasourceId = (
        await session.execute(
            select(KpiCatalog.datasource_id).where(
                KpiCatalog.kpi_code == plan.kpiCode
            )
        )
    ).scalar_one_or_none()
    if datasourceId is None:
        bucket.append(plan.kpiCode)
