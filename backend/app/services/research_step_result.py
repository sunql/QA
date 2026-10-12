"""研究步结果持久化（Task 6 / controller D23–D27）：单步执行快照落库，供报告回放。

**为什么单开一个模块**：`buildStepResult` 是**纯构造器**（无 IO / 无状态），落库与读取是
一组内聚的 repository 面。放在执行面（`research_agent_execution.py`）或相位面会把两种
职责混在一起，且二者都已逼近 800 行硬上限。

**注入点**（D23c）：`_stageExecute` 的**循环**（`research_agent_phases.py`），**不是**
`runStep` —— `runStep` 的三条失败 / 缺 SQL 返回路径拿不到 `runStateId`，且中途 checkpoint
暂停会绕过收尾 `finalizeStepResults`。循环里 `session` 与每步 `result` 都在手。

**证据摘要**（D24）：直接取 `result["evidence"]`（`chartStep` 已用
`evidence.model_dump(by_alias=True)` 算好的 JSON 原生 dict，`Decimal → str`），**禁止**
`summary.__dict__`（Task 5 D19 修掉的三重错：snake_case + 驱动原始值 + `_coerceJsonSafe`
变 float）。

**原始行**（D23）：`chartStep` 把驱动原始行挂在结果 dict 的
`STEP_RESULT_RAW_ROWS_KEY` 载体上；循环取回后交本模块。原始行过 `research_json`（D14
口径：`Decimal → str` 无损、`date/datetime → isoformat`）再落 `raw_result_ref` —— 驱动标量
（`Decimal`/`date`）直接进 JSONB 会 `TypeError`。超过 `MAX_FINDING_ROWS` 时截断（与
`findingData` 同上限，bound JSONB 体积）。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchStepResult
from app.services.research_agent_ports import MAX_FINDING_ROWS
from app.services.research_json_encoder import research_json

logger = logging.getLogger(__name__)

# 步状态（三分支，见 `buildStepResult` 派生规则）
STEP_STATUS_SUCCESS = "success"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_EMPTY = "empty"

# `step_type` 列宽（与 `research_step_result.step_type` String(50) 对齐）。
STEP_TYPE_LEN = 50
# 计划步无 `description` 时的缺省 `step_type`（D23：真实语义信号是 `description`，
# 结果 dict 里**没有** `type` 键）。
DEFAULT_STEP_TYPE = "sql"
# `raw_result_ref` 的 schema 版本（D8：结果契约随 runner 侧演进时据此判定）。
RAW_RESULT_SCHEMA_VERSION = 1


def stepTypeOf(step: dict[str, Any]) -> str:
    """`step_type` ← 步 `description`（D23）：空 → `"sql"`，超列宽则截断 + warning。

    步结果 dict **没有** `type` 键（`stepResult` 的键集固定），故 plan 的
    ``step.get("type", "sql")`` 恒得 `"sql"`；真实语义信号是 `description`。
    """
    raw = str(step.get("description") or "").strip()
    if not raw:
        return DEFAULT_STEP_TYPE
    if len(raw) > STEP_TYPE_LEN:
        logger.warning(
            "步描述超长，step_type 截断: len=%s limit=%s", len(raw), STEP_TYPE_LEN
        )
        return raw[:STEP_TYPE_LEN]
    return raw


def _jsonSafeRows(raw_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """驱动原始行 → JSON 安全行（`research_json` 口径），并按 `MAX_FINDING_ROWS` 截断。"""
    truncated = raw_rows[:MAX_FINDING_ROWS]
    if len(raw_rows) > MAX_FINDING_ROWS:
        logger.warning(
            "步结果原始行超上限，raw_result_ref 截断: rows=%s limit=%s",
            len(raw_rows),
            MAX_FINDING_ROWS,
        )
    return json.loads(research_json(truncated))


def buildStepResult(
    *,
    run_state_id: uuid.UUID,
    step_index: int,
    step_type: str,
    sql_text: str | None,
    raw_rows: list[dict[str, Any]],
    evidence_summary: dict[str, Any] | None,
    error: str | None,
) -> ResearchStepResult:
    """纯构造一个 `ResearchStepResult`（无 IO / 无状态；commit 由调用方负责）。

    `status` 派生（D23）：`error` 真值 ⇒ ``failed``；否则 `raw_rows` 为空 ⇒ ``empty``；
    否则 ``success``。三者互斥且覆盖全部返回路径（失败 / 空数据 / 有数据）。
    """
    status = STEP_STATUS_SUCCESS
    if error:
        status = STEP_STATUS_FAILED
    elif not raw_rows:
        status = STEP_STATUS_EMPTY

    return ResearchStepResult(
        id=uuid.uuid4(),
        run_state_id=run_state_id,
        step_index=step_index,
        step_type=step_type,
        sql_text=sql_text,
        raw_result_ref={
            "rows": _jsonSafeRows(raw_rows),
            "schemaVersion": RAW_RESULT_SCHEMA_VERSION,
        },
        evidence_summary=evidence_summary,
        status=status,
        error=error,
    )


async def saveStepResult(
    session: AsyncSession, result: ResearchStepResult
) -> ResearchStepResult:
    """`session.add`（**不** flush / commit —— 提交由 `persistStepResult` 逐级负责）。"""
    session.add(result)
    return result


async def loadStepResults(
    session: AsyncSession, run_state_id: uuid.UUID
) -> list[ResearchStepResult]:
    """按 `run_state_id` 取回步结果，按 `step_index` 升序（报告回放用）。"""
    rows = await session.execute(
        select(ResearchStepResult)
        .where(ResearchStepResult.run_state_id == run_state_id)
        .order_by(ResearchStepResult.step_index)
    )
    return list(rows.scalars().all())


async def persistStepResult(
    session: AsyncSession,
    *,
    state: dict[str, Any],
    step: dict[str, Any],
    result: dict[str, Any],
    raw_rows: list[dict[str, Any]],
) -> None:
    """单步落库（D23c）：`buildStepResult` + `saveStepResult` + **每步一次 commit**。

    `runStateId`（唯一来源 `state["runStateId"]`，D23b）为空 ⇒ **跳过持久化 + warning**，
    不崩、不抛 —— 未接线的测试路径不该被持久化拦住。

    **每步 commit 是承重的**：`runStep` 失败路径（`failedStep`）会 `rollbackQuietly` 整个
    session；若不逐级提交，下一步的失败会把上一步已 `add` 未提交的行一起回滚掉。
    """
    run_state_id = state.get("runStateId")
    if not run_state_id:
        logger.warning(
            "步结果跳过持久化（state 缺 runStateId）: step=%s", step.get("index")
        )
        return
    await saveStepResult(
        session,
        buildStepResult(
            run_state_id=run_state_id,
            step_index=int(result.get("index", 0)),
            step_type=stepTypeOf(step),
            sql_text=result.get("sql"),
            raw_rows=raw_rows,
            evidence_summary=result.get("evidence"),
            error=result.get("error"),
        ),
    )
    await session.commit()
