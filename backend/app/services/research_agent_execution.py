"""研究执行面（Task 6.5-1 / 6.5-3）：步 SQL 生成 → 只读查询 → 出图。

从 `research_agent_service.py` 抽出（Task 6.5 fix round 2）：该文件触 800 行硬上限，而
执行面（`runStep` / `generateStepSql` / `failedStep` / `chartStep`）是内聚的一组。依赖经
`ExecutionDeps`（绑定的协作者 + 两个回调）显式注入，模块内不持任何状态。

Step 0 依据（2026-10-04）：
- 生成面 = `Nl2SqlService.generateSql(question, classes, llmClient, modelConfig, *,
  session=None)`（公开面，前四位位置参数固定）；
- 执行面 = 注入的 `ResearchSqlRunner.executeReadonlySql(session, sql, adapter=...)`：
  `ValueError` 表 SQL Guard 拒绝、`TimeoutError` 表超时、`SQLAlchemyError` 表 DB/驱动层。

事务约定（Task 4）：执行失败路径会 rollback 注入的 session，故 `_stageExecute` 在进执行面
前先 `commit`（见 `research_agent_service.py`），本模块只负责单步的收敛与留痕。

**Task 13e（业务库接线）**：执行面的数据源是**逐请求**才确定的（`research_session.datasource_id`），
而 `ResearchSqlRunner` 在工厂里一次性构造 ⇒ 经 `ExecutionDeps.datasource` **按次送达**
（`depsForSession` 在本 turn 的相位边界产出 `replace(...)` 副本），不给 runner 加构造参数。
`adapterFor` 对缺失数据源**显式报错**（`RuntimeError`，刻意避开会被降级的 `ValueError`），
绝不静默回落到元数据库会话 —— 那正是本特性此前的根因。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchSession
from app.infrastructure.business_db_pool import get_adapter
from app.services.datasource_service import DataSourceService
from app.services.nl2sql_service import Nl2SqlService, _safeSchemaPrefix
from app.services.research_agent_ports import (
    EVENT_ERROR,
    EVENT_STEP_CHART,
    EVENT_STEP_DATA,
    EVENT_STEP_DONE,
    EVENT_STEP_SQL,
    EVENT_STEP_START,
    PURPOSE_CHART,
    SIGNAL_SQL_VALIDATION_FAILED,
    STEP_MISSING_SQL,
    Emit,
    MeteredClient,
    emitEvent,
    errorPayload,
    rollbackQuietly,
    selectClassesForTables,
    stepErrorCode,
    stepResult,
)
from app.services.research_agent_stages import eslClasses

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionDeps:
    """执行面依赖（构造期一次性绑定；`runStep` 只读不写）。

    `datasource` 是唯一的**按次**字段：构造期绑定的副本不带它，本 turn 的相位边界经
    `depsForSession(...)` 用 `replace` 产出带源副本（研究是多轮 + 可恢复的，源逐请求
    解析）。`None` 只出现在未接线的测试里 —— `adapterFor` 对它显式报错。
    """

    runner: Any
    chart: Any
    ontology: Any
    nl2sql: Any | None
    # `resolveClient(session, *, state, emit, sessionId) -> (client, config)`
    resolveClient: Callable[..., Awaitable[tuple[Any, Any]]]
    # `recordUsage(session, *, sessionId, purpose, promptTokens, completionTokens,
    #               modelName, cachedTokens=None) -> None`
    recordUsage: Callable[..., Awaitable[None]]
    datasource: Any | None = None


async def depsForSession(
    session: AsyncSession, sessionId: uuid.UUID, base: ExecutionDeps
) -> ExecutionDeps:
    """按会话落库的 `datasource_id` 解析业务数据源，产出本 turn 的执行面依赖。

    研究**多轮 + 可恢复**（`state` 不持久化），源在会话上一次性选定并全程沿用，故按
    `sessionId` 取。会话缺行 / `datasource_id` 为空 ⇒ 显式报错：**绝不**静默回落到应用
    元数据库会话（`data_source` 表在元数据库，业务表在业务库，两者不可混）。
    """
    row = await session.get(ResearchSession, sessionId)
    if row is None or row.datasource_id is None:
        logger.error("研究会话未绑定业务数据源: session=%s", sessionId)
        raise RuntimeError(f"研究会话未绑定业务数据源（datasource_id 为空）: {sessionId}")
    ds = await DataSourceService().get(session, row.datasource_id)
    return replace(base, datasource=ds)


def adapterFor(ds: Any | None) -> Any:
    """业务库适配器（配方与 chat 同源：`get_adapter(ds.id, ds)`，带进程内缓存）。

    `ds` 缺失 ⇒ `RuntimeError`（**不用** `ValueError`：后者会被 `runStep` 当成 SQL Guard
    拒绝降级成「该步无数据」，把配置缺失伪装成业务失败）。
    """
    if ds is None:
        raise RuntimeError("研究执行面缺少业务数据源（adapter 未按次送达）")
    return get_adapter(ds.id, ds)


def dialectArgsFor(ds: Any) -> dict[str, Any]:
    """`generateSql` 的方言参数（与 chat `_sampleValueDomains` 同款判定，逐字对齐）。

    `schemaPrefix` 与 schema 文本路径共用同一白名单 `_safeSchemaPrefix`：非法用户名
    （非字母/数字/下划线）不烘焙，避免提示注入。
    """
    dialect = Nl2SqlService.resolveDialect(ds.type, ds.oracle_version)
    return {
        "datasourceType": ds.type,
        "oracle_version": ds.oracle_version,
        "schemaPrefix": _safeSchemaPrefix(ds.username) if dialect.useSchemaPrefix else None,
    }


async def runStep(
    session: AsyncSession,
    step: dict[str, Any],
    *,
    sessionId: uuid.UUID,
    state: dict[str, Any],
    emit: Emit | None,
    deps: ExecutionDeps,
) -> dict[str, Any]:
    """执行单个计划步：无 SQL 先经 NL2SQL 生成 → 只读查询 + 出图。

    失败面**显式收窄**（Task 5 fix round 1）：只收敛 SQL Guard 拒绝（ValueError）、超时
    （TimeoutError）与 DB/驱动层（SQLAlchemyError）三类**运行时**失败；其它异常（编程错误）
    原样上抛 —— 不能被伪装成「该步无数据」而在 autoConfirm 下静默 done。

    Task 6.5-1：生成失败**不抛**（返回 None），落到既有 `STEP_MISSING_SQL` 分支。

    Task 8（MEDIUM-7）：逐步发 §4.5 全事件族 —— start → sql → data → chart → done；
    缺 SQL 的步没有 sql 事件（无 SQL 可发），失败步止于 `research.error`。
    """
    index = int(step.get("index", 0))
    await emitEvent(
        emit, EVENT_STEP_START, {"index": index, "description": step.get("description")}
    )
    sql = step.get("sql") or await generateStepSql(
        session, step, sessionId=sessionId, state=state, emit=emit, deps=deps
    )
    if not sql:
        logger.warning("执行步缺 SQL，按无数据跳过: session=%s step=%s", sessionId, index)
        return stepResult(step, rows=[], error=STEP_MISSING_SQL)
    # 不可变：把生成的 SQL 固化进本步副本（步结果/动态点据此可追溯）
    resolved = {**step, "sql": sql}
    await emitEvent(emit, EVENT_STEP_SQL, {"index": index, "sql": sql})
    # 业务库 adapter 在 try 之外解析：未送达是**接线缺失**（RuntimeError 上抛），
    # 不能被下面的三面收窄伪装成「该步无数据」
    adapter = adapterFor(deps.datasource)
    try:
        rows = await deps.runner.executeReadonlySql(session, sql, adapter=adapter)
    except ValueError as exc:
        # SQL Guard 拒绝（Task 4 裁定：ValueError 不属 DomainError 面，必须显式 catch）
        return await failedStep(
            session, resolved, f"{SIGNAL_SQL_VALIDATION_FAILED}: {exc}", emit=emit
        )
    except TimeoutError as exc:
        return await failedStep(session, resolved, f"执行超时: {exc}", emit=emit)
    except SQLAlchemyError as exc:
        logger.warning("执行步 DB 失败: session=%s step=%s err=%s", sessionId, index, exc)
        return await failedStep(session, resolved, str(exc), emit=emit)
    await emitEvent(
        emit, EVENT_STEP_DATA, {"index": index, "data": rows, "rowCount": len(rows)}
    )
    return await chartStep(
        session, resolved, rows, sessionId=sessionId, state=state, emit=emit, deps=deps
    )


async def generateStepSql(
    session: AsyncSession,
    step: dict[str, Any],
    *,
    sessionId: uuid.UUID,
    state: dict[str, Any],
    emit: Emit | None,
    deps: ExecutionDeps,
) -> str | None:
    """无 SQL 的计划步：走真实 NL2SQL 生成（Task 6.5-1）。

    未接线（`nl2sql is None`）、无可用 LLM、生成抛错或产物为空都返回 None —— 调用方据此
    走 `STEP_MISSING_SQL`（显式 warning，不静默）。子问题优先于主问题（多步场景下每一步
    的语义范围不同，与 chat `_executeDataStep` 同口径）。

    方言参数（Task 13e）：`datasourceType` / `oracle_version` / `schemaPrefix` 按本 turn
    的业务数据源注入 —— 缺源的会话在 `runStep` 已被 `adapterFor` 拦截，故此处非空。
    """
    if deps.nl2sql is None:
        return None
    client, config = await deps.resolveClient(
        session, state=state, emit=emit, sessionId=sessionId
    )
    if client is None:
        return None
    # 方言参数在 try 之外取：缺数据源是接线问题，不该落进下面的「生成失败」降级
    dialectArgs = dialectArgsFor(deps.datasource)
    question = str(step.get("sub_question") or "").strip() or str(state["question"])
    try:
        classes = selectClassesForTables(
            await deps.ontology.listClasses(session), eslClasses(state)
        )
        result = await deps.nl2sql.generateSql(
            question, classes, client, config, session=session, **dialectArgs
        )
    except Exception:  # noqa: BLE001 —— 生成失败降级为「该步无数据」，不炸整条链路
        logger.warning(
            "步 SQL 生成失败，按无数据跳过: session=%s step=%s",
            sessionId, step.get("index"), exc_info=True,
        )
        return None
    sql = getattr(result, "sql", None)
    if not sql:
        logger.warning("步 SQL 生成为空: session=%s step=%s", sessionId, step.get("index"))
        return None
    return str(sql)


async def failedStep(
    session: AsyncSession,
    step: dict[str, Any],
    error: str,
    *,
    emit: Emit | None,
) -> dict[str, Any]:
    """步失败收敛：清障 → 结果摘要 → `research.error` 事件（不抛，交给动态点决策）。"""
    await rollbackQuietly(session)
    result = stepResult(step, rows=[], error=error)
    await emitEvent(
        emit,
        EVENT_ERROR,
        errorPayload(stepErrorCode(error), error, stepIndex=result["index"]),
    )
    return result


async def chartStep(
    session: AsyncSession,
    step: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    sessionId: uuid.UUID,
    state: dict[str, Any],
    emit: Emit | None,
    deps: ExecutionDeps,
) -> dict[str, Any]:
    """出图（ChartService 契约：绝不抛错）并落 result + 发 step_chart / step_done。"""
    build = await _buildChartMetered(
        session, rows, sessionId=sessionId, state=state, emit=emit, deps=deps
    )
    result = stepResult(
        step,
        rows=rows,
        error=None,
        chartType=getattr(getattr(build, "chartType", None), "value", None),
        chartOption=getattr(build, "option", None),
    )
    await emitEvent(
        emit,
        EVENT_STEP_CHART,
        {
            "index": result["index"],
            "chartType": result["chartType"],
            "chartOption": result["chartOption"],
        },
    )
    await emitEvent(
        emit,
        EVENT_STEP_DONE,
        {"index": result["index"], "rowCount": result["rowCount"], "summary": result["summary"]},
    )
    return result


async def _buildChartMetered(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    sessionId: uuid.UUID,
    state: dict[str, Any],
    emit: Emit | None,
    deps: ExecutionDeps,
) -> Any:
    """调 ChartService 出图；LLM 调用经计量边界并按 `purpose=research_chart` 记账。

    Task 6.5-3：此前直接把裸客户端交给 ChartService —— 一旦 ChartService 真的走 LLM
    （`decision.ambiguous and modelConfig is not None`），调用就是未计量的。现包
    `MeteredClient` 并按 `purpose=research_chart` 记账（零消耗自动跳过）。
    """
    client, _config = await deps.resolveClient(
        session, state=state, emit=emit, sessionId=sessionId
    )
    metered = MeteredClient(client) if client is not None else None
    build = await deps.chart.buildChart(
        session=session,
        plan=None,
        columns=list(rows[0].keys()) if rows else [],
        data=rows,
        question=state["question"],
        llmClient=metered,
        modelConfig=None,
    )
    if metered is not None:
        await deps.recordUsage(
            session,
            sessionId=sessionId,
            purpose=PURPOSE_CHART,
            promptTokens=metered.promptTokens,
            completionTokens=metered.completionTokens,
            modelName=metered.modelName,
            cachedTokens=metered.cachedTokens,
        )
    return build
