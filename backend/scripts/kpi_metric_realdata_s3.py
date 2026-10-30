"""S3 三层对账的判定 oracle（纯函数 + 结果载体）。

**为什么单列一个模块**：入口 ``kpi_metric_realdata.py`` 有 800 行上限，而 S3
的判定件（列差异 oracle、分母判定、formula 覆盖）与入口主流程无关，且需要
单测直接覆盖（真库实跑只覆盖「12 条全部有偏差」这一条路径）。

⚠️ 本模块 **可以 import app**（与 ``_lib`` 不同）：它只在入口完成
``os.environ["DATABASE_URL"]`` 设置之后才被导入。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.kpi_metric_reconcile import (
    _TYPES_REQUIRING_DENOMINATOR,
    REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
    REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,
)
from app.services.nl2sql_semantic_guard import extractSqlIdentifiers

# 列级 finding 的两个 code（S3.4 按 code 反查「列比对是否真的跑了」）
COLUMN_FINDING_CODES = frozenset(
    {
        REASON_GOVERNANCE_COLUMN_NOT_IN_TECHNICAL,
        REASON_TECHNICAL_COLUMN_NOT_IN_GOVERNANCE,
    }
)


def normTokens(values: object) -> set[str]:
    """把列/粒度数组归一化成小写集合（与对账引擎的 ``_tokens`` 同语义）。

    非 list/tuple 一律返回空集（PLAN/库内字段可能缺失或类型漂移，不抛）。
    """
    if not isinstance(values, (list, tuple)):
        return set()
    return {v.strip().lower() for v in values if isinstance(v, str) and v.strip()}


def roleColumnDiffers(declared: object, measure: object) -> bool:
    """治理声明的列与技术度量列**都非空且不相等** ⇒ 对账必须出列级 finding。

    ``measure`` 是 ``MeasureSpec | None``：``None`` 时引擎给的是
    ``governance_measure_absent``（不是列级 code）⇒ 这里返回 ``False``。
    """
    declaredSet = normTokens(declared)
    technical = measure.columns() if measure is not None else set()
    return bool(declaredSet) and bool(technical) and declaredSet != technical


def needsDenominatorCheck(governance: object, technique: object) -> bool:
    """复刻层 2 的分母判定：比率/差值型，或治理侧显式声明了分母列。

    ⚠️ 分母类型集合**复用引擎常量** ``_TYPES_REQUIRING_DENOMINATOR``：这里曾
    手抄一份 ``(RATIO, DIFFERENCE)``，引擎扩类型时两边会静默漂移。
    """
    metricType = getattr(technique, "metric_type", None)
    if metricType in _TYPES_REQUIRING_DENOMINATOR:
        return True
    return bool(normTokens(getattr(governance, "denominator_columns", ())))


def anyColumnDiffers(governance: object, technique: object) -> bool:
    """分子或（需要判定时）分母的治理列 ≠ 技术列 ⇒ 必须有列级 finding。"""
    if roleColumnDiffers(
        getattr(governance, "numerator_columns", ()),
        getattr(technique, "numerator", None),
    ):
        return True
    if not needsDenominatorCheck(governance, technique):
        return False
    return roleColumnDiffers(
        getattr(governance, "denominator_columns", ()),
        getattr(technique, "denominator", None),
    )


def formulaCoverage(
    declared: set[str], formula: str | None
) -> tuple[bool, list[str]]:
    """tier A 的 formula 是否覆盖全部结构化列（T16-17）。"""
    if not declared:
        return False, ["结构化度量为空（可能已降级）"]
    missing = sorted(declared - extractSqlIdentifiers(formula))
    return not missing, missing


@dataclass(frozen=True)
class ReconcileOutcome:
    """单个已挂靠 KPI 的层 2 结果 + 列差异 oracle 的期望值。"""

    code: str
    verdict: str
    reasons: tuple[str, ...]
    columnDiffers: bool


def expectedColumnCodes(outcomes: Sequence[ReconcileOutcome]) -> set[str]:
    """按 oracle：列差异为真的 KPI 集合（S3.4 期望值）。"""
    return {outcome.code for outcome in outcomes if outcome.columnDiffers}


def actualColumnCodes(outcomes: Sequence[ReconcileOutcome]) -> set[str]:
    """按实际 findings：含任一列级 code 的 KPI 集合（S3.4 实测值）。"""
    return {
        outcome.code
        for outcome in outcomes
        if COLUMN_FINDING_CODES & set(outcome.reasons)
    }


__all__ = [
    "COLUMN_FINDING_CODES",
    "ReconcileOutcome",
    "actualColumnCodes",
    "anyColumnDiffers",
    "expectedColumnCodes",
    "formulaCoverage",
    "needsDenominatorCheck",
    "normTokens",
    "roleColumnDiffers",
]
