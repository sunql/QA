"""研究执行面（Task 6.5-1 / 6.5-3）：步 SQL 生成 → 只读查询 → 出图。

从 `research_agent_service.py` 抽出（Task 6.5 fix round 2）：该文件触 800 行硬上限，而
执行面（`runStep` / `generateStepSql` / `failedStep` / `chartStep`）是内聚的一组。依赖经
`ExecutionDeps`（绑定的协作者 + 两个回调）显式注入，模块内不持任何状态。

Step 0 依据（2026-10-04）：
- 生成面 = `Nl2SqlService.generateSql(question, classes, llmClient, modelConfig, *,
  session=None)`（公开面，前四位位置参数固定）；
- 执行面 = 注入的 `ResearchSqlRunner.executeReadonlySql(session, sql, adapter=...)`：
  `ValueError` 表 SQL Guard 拒绝、`TimeoutError` 表超时、驱动层 = `SQLAlchemyError`
  （PG/MySQL 方言）**与** `oracledb.Error`（Oracle 原生 async，见 `_DRIVER_ERRORS`）。

事务约定（Task 4）：执行失败路径会 rollback 注入的 session，故 `_stageExecute` 在进执行面
前先 `commit`（见 `research_agent_service.py`），本模块只负责单步的收敛与留痕。

**Task 13e（业务库接线）**：执行面的数据源是**逐请求**才确定的（`research_session.datasource_id`），
而 `ResearchSqlRunner` 在工厂里一次性构造 ⇒ 经 `ExecutionDeps.adapter` / `.dialectArgs` **按次送达**
（`depsForSession` 在本 turn 的**相位边界**一次算好，用 `replace(...)` 产副本），不给 runner 加构造参数。
`adapterFor` 对缺失数据源**显式报错**（`RuntimeError`，刻意避开会被降级的 `ValueError`），
绝不静默回落到元数据库会话 —— 那正是本特性此前的根因。

**相位边界解析（Task 13e fix round 2）**：adapter 与方言参数在**进循环之前**算好，循环内**零 ORM
属性访问** —— 失败步的 `failedStep` → `rollbackQuietly` 会顶层 rollback 并 expire identity map
（`expire_on_commit=False` 只护 commit，不护 rollback），若在循环内读 ORM 数据源对象，下一步就会
触发隐式 IO 抛 `MissingGreenlet`，把单步失败升级成整 turn 死。照 `_stageVerify` 的既有做法。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

import oracledb
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
    MSG_ALL_STEPS_FAILED,
    OPT_STEP_INDEX,
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

# 执行面的**驱动层**异常集合（Task 13e fix round 1）：两条驱动**并列**收窄 ——
# - `SQLAlchemyError`：PG / MySQL 走 SQLAlchemy async 方言；
# - `oracledb.Error`：Oracle 走 `business_db_pool._OracleAdapter` 的 **oracledb 原生 async**，
#   该适配器原样穿透驱动异常，且 `oracledb.Error` **不在** `SQLAlchemyError` 之下。
# 只收这两条，刻意**不**放开成 `except Exception`：编程错误（如 TypeError）必须原样上抛，
# 否则会被伪装成「该步无数据」而在 autoConfirm 下静默 done（见测试双侧守卫）。
_DRIVER_ERRORS: tuple[type[BaseException], ...] = (SQLAlchemyError, oracledb.Error)


@dataclass(frozen=True)
class ExecutionDeps:
    """执行面依赖（构造期一次性绑定；`runStep` 只读不写）。

    `adapter` / `dialectArgs` 是**按次**字段：构造期绑定的副本不带它们，本 turn 的相位边界经
    `depsForSession(...)` 用 `replace` 产出**已解析**副本（研究是多轮 + 可恢复的，源逐请求
    解析）。`None` 只出现在未接线的测试里 —— `businessBinding` 对它显式报错。

    两者**只能**在相位边界算好，循环内一律读这里：业务源是 ORM 对象，失败步的 rollback 会
    expire 它，循环内再读属性就会 `MissingGreenlet`（见模块 docstring）。`datasource` 本身保留
    给**同一条约定**下的其它相位边界（`_stageVerify` 在它的循环外解析 adapter）。
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
    # 相位边界算好的业务库绑定（`depsForSession` 一次性写入，二者同生共死）
    adapter: Any | None = None
    dialectArgs: dict[str, Any] | None = None


def buildExecutionDeps(
    runner: Any,
    chart: Any,
    ontology: Any,
    nl2sql: Any | None,
    resolveClient: Callable[..., Awaitable[tuple[Any, Any]]],
    recordUsage: Callable[..., Awaitable[None]],
) -> ExecutionDeps:
    """构造调用方的执行面依赖（一次绑定）。

    从 `research_agent_service._buildExec` 搬来（Task 17，A4.2 行数预算）：装配细节与
    `ExecutionDeps` / `depsForSession` 同域。`adapter` / `dialectArgs` 刻意留空 —— 它们是
    **按次**字段，由 `depsForSession` 在相位边界经 `replace` 补齐。
    """
    return ExecutionDeps(
        runner=runner,
        chart=chart,
        ontology=ontology,
        nl2sql=nl2sql,
        resolveClient=resolveClient,
        recordUsage=recordUsage,
    )


async def depsForSession(
    session: AsyncSession, sessionId: uuid.UUID, base: ExecutionDeps
) -> ExecutionDeps:
    """按会话落库的 `datasource_id` 解析业务数据源，产出本 turn 的执行面依赖。

    研究**多轮 + 可恢复**（`state` 不持久化），源在会话上一次性选定并全程沿用，故按
    `sessionId` 取。会话缺行 / `datasource_id` 为空 ⇒ 显式报错：**绝不**静默回落到应用
    元数据库会话（`data_source` 表在元数据库，业务表在业务库，两者不可混）。

    adapter 与方言参数在**这里**（= 相位边界）一次算好并写进返回副本：此后循环内不再碰
    ORM 数据源对象（失败步的 rollback 会 expire 它，循环内读属性即 `MissingGreenlet`）。
    `get_adapter` 带进程内缓存，按相位重复解析不建连、不重复解密。
    """
    row = await session.get(ResearchSession, sessionId)
    if row is None or row.datasource_id is None:
        logger.error("研究会话未绑定业务数据源: session=%s", sessionId)
        raise RuntimeError(f"研究会话未绑定业务数据源（datasource_id 为空）: {sessionId}")
    ds = await DataSourceService().get(session, row.datasource_id)
    # 两个 resolver 紧贴取源处调用：此刻 `ds` 必是新鲜的（尚未经历任何 rollback）
    return replace(
        base,
        datasource=ds,
        adapter=adapterFor(ds),
        dialectArgs=dialectArgsFor(ds),
    )


def adapterFor(ds: Any | None) -> Any:
    """业务库适配器（配方与 chat 同源：`get_adapter(ds.id, ds)`，带进程内缓存）。

    **相位边界**调用（`depsForSession` 取到源后立即调用），结果随 `ExecutionDeps` 副本送达执行面。
    `ds` 缺失 ⇒ `RuntimeError`（**不用** `ValueError`：后者会被 `runStep` 当成 SQL Guard 拒绝
    降级成「该步无数据」，把配置缺失伪装成业务失败）。
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


def businessBinding(deps: ExecutionDeps) -> tuple[Any, dict[str, Any]]:
    """取本相位边界算好的业务库绑定（adapter + 方言参数）；缺失 ⇒ `RuntimeError`。

    这是执行面的**唯一闸门**，且必须在生成面**之前**：缺源/未接线是配置问题，不能被误诊成
    编程错误（`AttributeError`），也不能被 `runStep` 的收窄面伪装成「该步无数据」——
    故刻意用 `RuntimeError` 而非会被降级的 `ValueError`。
    """
    if deps.adapter is None or deps.dialectArgs is None:
        raise RuntimeError("研究执行面缺少业务数据源（adapter / 方言参数未按次送达）")
    return deps.adapter, deps.dialectArgs


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
    （TimeoutError）与驱动层（`_DRIVER_ERRORS`：SQLAlchemy 方言 **或** oracledb 原生）三类
    **运行时**失败；其它异常（编程错误）原样上抛 —— 不能被伪装成「该步无数据」而在
    autoConfirm 下静默 done。

    Task 13e fix round 1：驱动层此前只列 `SQLAlchemyError`，而业务 SQL 改走 `_OracleAdapter`
    的 oracledb 原生 async 后，`oracledb.Error` 收不住 ⇒ 单步失败逃成**整 turn 失败**
    （真机实测 ORA-00920 / ORA-00933 / DPY-6005 三次）。现两条驱动并列收窄、共用同一
    「单步降级」handler（`logger.warning` + `failedStep`）。

    Task 6.5-1：生成失败**不抛**（返回 None），落到既有 `STEP_MISSING_SQL` 分支。

    Task 8（MEDIUM-7）：逐步发 §4.5 全事件族 —— start → sql → data → chart → done；
    缺 SQL 的步没有 sql 事件（无 SQL 可发），失败步止于 `research.error`。

    Task 13e fix round 2：业务库绑定在**最开头**取出（`businessBinding`，先于生成面与
    `step.start` 事件）—— 未送达是**接线缺失**（RuntimeError 上抛，不降级），且必须在
    `generateStepSql` 之前，否则缺源会被误诊成 `AttributeError`。此后循环内零 ORM 访问。
    """
    adapter, dialectArgs = businessBinding(deps)
    index = int(step.get("index", 0))
    await emitEvent(
        emit, EVENT_STEP_START, {"index": index, "description": step.get("description")}
    )
    sql = step.get("sql") or await generateStepSql(
        session,
        step,
        sessionId=sessionId,
        state=state,
        emit=emit,
        deps=deps,
        dialectArgs=dialectArgs,
    )
    if not sql:
        logger.warning("执行步缺 SQL，按无数据跳过: session=%s step=%s", sessionId, index)
        return stepResult(step, rows=[], error=STEP_MISSING_SQL)
    # 不可变：把生成的 SQL 固化进本步副本（步结果/动态点据此可追溯）
    resolved = {**step, "sql": sql}
    await emitEvent(emit, EVENT_STEP_SQL, {"index": index, "sql": sql})
    try:
        rows = await deps.runner.executeReadonlySql(session, sql, adapter=adapter)
    except ValueError as exc:
        # SQL Guard 拒绝（Task 4 裁定：ValueError 不属 DomainError 面，必须显式 catch）
        return await failedStep(
            session, resolved, f"{SIGNAL_SQL_VALIDATION_FAILED}: {exc}", emit=emit
        )
    except TimeoutError as exc:
        return await failedStep(session, resolved, f"执行超时: {exc}", emit=emit)
    except _DRIVER_ERRORS as exc:
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
    dialectArgs: dict[str, Any],
) -> str | None:
    """无 SQL 的计划步：走真实 NL2SQL 生成（Task 6.5-1）。

    未接线（`nl2sql is None`）、无可用 LLM、生成抛错或产物为空都返回 None —— 调用方据此
    走 `STEP_MISSING_SQL`（显式 warning，不静默）。子问题优先于主问题（多步场景下每一步
    的语义范围不同，与 chat `_executeDataStep` 同口径）。

    方言参数（Task 13e）：`datasourceType` / `oracle_version` / `schemaPrefix` 由**相位边界**
    算好经入参传入 —— 本函数不碰业务源（ORM 对象），缺源在 `runStep` 的 `businessBinding`
    已经拦下（先于本函数），故此处必非空。
    """
    if deps.nl2sql is None:
        return None
    client, config = await deps.resolveClient(
        session, state=state, emit=emit, sessionId=sessionId
    )
    if client is None:
        return None
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


# ---------------------------------------------------------------------------
# 步结果语义（Task 17 / N1'）：`degraded` 口径的**数据源** + 全部步失败的急停闸门
#
# 缺陷：`stepsExecuted=len(results)` 把**带 error 的步**也算成「已执行」⇒ 执行步全失败的
# turn 仍报 `degraded:false` 并出报告（用户裁定：全部步失败 ⇒ fail loud；部分失败 ⇒ 降级）。
# `isDegraded` 的表达式不改 —— 只改喂给它的步数（见 `executedStepCount`）。
# 步失败 ⇔ `result["error"]` 为真值（`stepResult` 恒带该键，见 `ports.stepResult`）。
# ---------------------------------------------------------------------------


def countSuccessfulSteps(results: list[dict[str, Any]]) -> int:
    """无 error 的步数（`error` 为真值 ⇒ 失败步）。

    只认 **error 维度**，不看行数：「成功执行但 0 行」仍是成功步（Task 14 口径，不得回退）。
    """
    return sum(1 for result in results if not result.get("error"))


def allStepsFailed(results: list[dict[str, Any]]) -> bool:
    """非空且**每一步**都带 error（N1' 急停判据）。

    `results` 为空**不算**全部失败（例如全部步被 `startIndex` 跳过）—— 那不是「失败」而是
    「无步」，归 N1 的空计划闸门管，此处不得替它抛。
    """
    return bool(results) and all(result.get("error") for result in results)


def executedStepCount(results: list[dict[str, Any]]) -> int:
    """喂给 `isDegraded` 的步数（N1'）：**有任何一步失败 ⇒ 0**，否则 = 步数。

    用户裁定「全部步失败 ⇒ fail loud；部分失败 ⇒ `degraded=true`」，而 `isDegraded` 的表达式
    （`llmUnavailable or not stepsExecuted`）**不改** —— 故「部分失败 ⇒ 降级」只能由**数据源**表达：
    本 turn 一旦有失败步，就不是「健康地执行了 N 步」，记 0。
    「成功执行但 0 行」不在失败之列 ⇒ 仍计步 ⇒ `degraded=false`（Task 14 口径不回退）。
    """
    successful = countSuccessfulSteps(results)
    return successful if successful == len(results) else 0


def finalizeStepResults(state: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """执行相位收尾：**全部步失败 ⇒ 急停**；否则把步结果与步数写回 `state`（degraded 口径依据）。

    急停沿用本服务**既有**的终态失败通道（与 `_stageExecute` 的 N1 空计划闸门同一写法：
    `raise RuntimeError`）—— 由 `_guardedRun` 转 `research.error{turn_failed}` + `markFailed`，
    **不出报告**。不新增失败通道，不静默 `return None`。
    """
    if allStepsFailed(results):
        logger.error(
            "研究计划全部步失败（无一成功），本 turn 终止: steps=%s",
            [result.get("index") for result in results],
        )
        raise RuntimeError(MSG_ALL_STEPS_FAILED)
    state.update(stepResults=results, stepsExecuted=executedStepCount(results))


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
        errorPayload(stepErrorCode(error), error, **{OPT_STEP_INDEX: result["index"]}),
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
