"""KPI Tier A 执行路由与确定性执行（设计 §6）。

路由只读 ontology_metric.execution_source，不嗅探 formula 文本；开关关闭时
整个模块不参与决策（调用方回旧 L1 行为）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.domain.exceptions import SqlSafetyError
from app.domain.models import DataSource
from app.infrastructure.business_db_pool import (
    BusinessDbAdapter,
    _assert_read_only,
    get_adapter,
)
from app.services.evidence_record_service import scheduleMetricResultEvidence

logger = logging.getLogger(__name__)

TIER_A = "A"
TIER_B = "B"
TIER_C = "C"
TIER_LEGACY = "LEGACY"

SOURCE_ESCAPE_HATCH = "ESCAPE_HATCH"
SOURCE_COMPILED = "COMPILED"
SOURCE_NONE = "NONE"

REASON_TIER_A_DISABLED = "tier_a_disabled"
REASON_KPI_NOT_FOUND = "kpi_not_found"
REASON_METRIC_MISSING = "metric_missing"
REASON_EXECUTION_SOURCE_MISSING = "execution_source_missing"
REASON_EXECUTION_SOURCE_NONE = "execution_source_none"
REASON_EXECUTION_SOURCE_UNKNOWN = "execution_source_unknown"
REASON_DATASOURCE_MISSING = "datasource_missing"
REASON_EXPLICIT_TIME_SCOPE = "explicit_time_scope"
REASON_RECONCILE_NOT_ELIGIBLE = "reconcile_not_eligible"
REASON_FORMULA_EMPTY = "formula_empty"


@dataclass(frozen=True)
class KpiExecutionPlan:
    tier: str
    reason: str | None = None


def planKpiExecution(
    *,
    switch_on: bool,
    kpi_found: bool,
    metric_present: bool,
    execution_source: str | None,
    tier_a_eligible: bool,
    has_datasource: bool,
    formula_present: bool,
    has_explicit_time_scope: bool,
    time_param_ready: bool = False,
) -> KpiExecutionPlan:
    """按 §6.1/§6.2/§6.4 顺序短路（见 plan.md §3 决策表）。"""
    if not switch_on:
        return KpiExecutionPlan(TIER_LEGACY, REASON_TIER_A_DISABLED)
    if not kpi_found:
        return KpiExecutionPlan(TIER_LEGACY, REASON_KPI_NOT_FOUND)
    if not metric_present:
        return KpiExecutionPlan(TIER_C, REASON_METRIC_MISSING)
    source = (execution_source or "").strip().upper()
    if not source:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_MISSING)
    if source == SOURCE_NONE:
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_NONE)
    # COMPILED 与 ESCAPE_HATCH 同属可执行源，共享后半段全部检查；编译器本身
    # 失败时由执行层降级，而不是在路由期一票否决。
    if source not in (SOURCE_ESCAPE_HATCH, SOURCE_COMPILED):
        return KpiExecutionPlan(TIER_C, REASON_EXECUTION_SOURCE_UNKNOWN)
    if not has_datasource:
        return KpiExecutionPlan(TIER_C, REASON_DATASOURCE_MISSING)
    # P4a：formula 声明 {time_scope} 且 time_column 就绪时，显式时间问句不再
    # 一票降级 Tier B（渲染失败在执行层降级，见 time_scope_renderer）。
    if has_explicit_time_scope and not time_param_ready:
        return KpiExecutionPlan(TIER_B, REASON_EXPLICIT_TIME_SCOPE)
    if not formula_present:
        return KpiExecutionPlan(TIER_B, REASON_FORMULA_EMPTY)
    if not tier_a_eligible:
        return KpiExecutionPlan(TIER_B, REASON_RECONCILE_NOT_ELIGIBLE)
    return KpiExecutionPlan(TIER_A)


# =============================================================================
# 显式时间限定检测
# =============================================================================

# 一期只做“有/无”判定，不做区间解析（P4 参数化才解析）。
# 不调用 time_scope_resolver.parseTimeScope：它只认季度/半年，且这些形态已被下面的
# 正则覆盖；复用会把「检测口径」与「注入口径」耦合，P4 改解析器时会连带改变路由。
_EXPLICIT_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(近|最近|过去|前)\s*[0-9一二三四五六七八九十两]+\s*(天|日|周|星期|个月|月|年|季度)"),
    re.compile(r"(近|最近|过去|这)\s*半\s*年"),
    re.compile(r"[0-9]{4}\s*年"),
    re.compile(r"[0-9]{4}\s*[-/]\s*[0-9]{1,2}"),
    re.compile(r"[0-9]{1,2}\s*月份?"),
    re.compile(r"[一二三四五六七八九十]{1,2}\s*月份?"),
    re.compile(r"(本|上|下|这|当前)(个)?(周|星期|个月|月|季度|年|年度)"),
    re.compile(r"(今年|去年|明年|前年|本年|上年)"),
    re.compile(r"第?\s*[一二三四1-4]\s*季度"),
    re.compile(r"(上|下)半年"),
    # CJK 字符在 Python 里算 \w，\bQ[1-4]\b 对「Q2交」不成立；用字母/数字边界代替。
    re.compile(r"(?<![A-Za-z])Q[1-4](?![0-9])", re.IGNORECASE),
    re.compile(r"(本|上|下|这)?财年"),
    re.compile(r"(年|月|季度|半年)(初|底|末)"),
    # 终审补：同/环比、半年度（H1/H2）、财年缩写（FY25 / FY2025）、滚动 12 个月
    # CJK 下 \b 不可用（中文字符算 \w），沿用字母/数字 lookaround 写法。
    re.compile(r"(同|环)比"),
    # H1/H2 的年份嵌入形态：`2025H1` / `24H1`（年份在前，H1/H2 在尾）与
    # `1H25`（1H/2H + 两位年份）。前一分支排除 H10/H1X；后一分支仅在前置数字时
    # 允许 H1/H2 后跟数字，才能覆盖 1H25 而不误判 H10。
    re.compile(
        r"(?:(?<![A-Za-z])H[12](?![0-9A-Za-z])|(?<=\d)H[12](?![A-Za-z]))"
    ),
    re.compile(r"(?<![A-Za-z])FY\s*\d{0,4}(?![0-9])", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z])(TTM|LTM)(?![A-Za-z])", re.IGNORECASE),
)


def hasExplicitTimeScope(question: str) -> bool:
    """问句是否含显式时间限定（设计 §6.4：有 ⇒ 不路由 Tier A）。

    宁可多判（误判 ⇒ Tier B，多一次 LLM）不可漏判（漏判 ⇒ 时段错的数字看起来合理）。
    匹配到任一形态即视为显式；无数字的「近半年/这半年」也计入（滚动窗口语义有分歧，
    交给 LLM 按用户原话处理）。季度支持 Q2/2025Q2，月份支持阿拉伯数字与中文数字，
    另覆盖「上个季度」「本财年」「年初/月底」等相对与期间边缘形态。
    """
    return any(p.search(question) for p in _EXPLICIT_TIME_PATTERNS)


# =============================================================================
# Tier A 确定性执行器
# =============================================================================

KPI_RESULT_ROW_LIMIT = 1000

REASON_FORMULA_NOT_READ_ONLY = "formula_not_read_only"
REASON_EXECUTION_TIMEOUT = "execution_timeout"
REASON_EXECUTION_ERROR = "execution_error"
REASON_EMPTY_RESULT = "empty_result"

AdapterProvider = Callable[[int, Any], BusinessDbAdapter]


@dataclass(frozen=True)
class KpiExecutionResult:
    ok: bool
    rows: tuple[dict[str, Any], ...] = ()
    value: Any | None = None
    rowCount: int = 0
    truncated: bool = False
    elapsedMs: int = 0
    reason: str | None = None


def firstPresentValue(rows: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> Any | None:
    """首行第一个非 None 值（KPI 结果读值的唯一口径）。"""
    if not rows:
        return None
    return next((v for v in rows[0].values() if v is not None), None)


class KpiExecutionService:
    """执行 metric.formula：只读校验 → adapter（超时/行数由 adapter 兜底）→ 归一化。"""

    def __init__(
        self,
        *,
        adapterProvider: AdapterProvider | None = None,
        evidenceScheduler: Callable[..., Any] = scheduleMetricResultEvidence,
    ) -> None:
        self._adapterProvider = adapterProvider or get_adapter
        self._evidenceScheduler = evidenceScheduler

    async def execute(
        self,
        *,
        formula: str,
        datasourceId: int,
        datasource: DataSource,
        metricCode: str,
        metricName: str,
        confidence: float,
        rowLimit: int = KPI_RESULT_ROW_LIMIT,
    ) -> KpiExecutionResult:
        try:
            _assert_read_only(formula)  # 执行器侧第一道；adapter 内部还会再验
        except SqlSafetyError:
            logger.warning(
                "TierA formula 未通过只读校验 kpi=%s datasource_id=%s",
                metricCode, datasourceId, exc_info=True,
            )
            return KpiExecutionResult(ok=False, reason=REASON_FORMULA_NOT_READ_ONLY)

        started = time.monotonic()
        try:
            # adapter 构造也可能失败（如 SECRET_KEY 轮换后 decryptApiKey 抛
            # InvalidToken）；与执行同属可降级路径，绝不能让 Tier A 降级变成请求失败。
            adapter = self._adapterProvider(datasourceId, datasource)
            # 多取 1 行用于**精确**判断截断（len==rowLimit 不代表被截断）。
            # QUERY_ROW_LIMIT 默认 0（不限行），故 rowLimit + 1 的取数是精确的；
            # 若运维把 QUERY_ROW_LIMIT 配到 rowLimit 或更小，adapter 会先按配置
            # 截断，此时截断可能被少报（under-reported）。
            raw = await adapter.execute_read_only(formula, maxRows=rowLimit + 1)
        except TimeoutError:
            logger.warning(
                "TierA 执行超时 kpi=%s datasource_id=%s elapsed_ms=%d",
                metricCode, datasourceId, _elapsedMs(started), exc_info=True,
            )
            return KpiExecutionResult(
                ok=False, elapsedMs=_elapsedMs(started), reason=REASON_EXECUTION_TIMEOUT
            )
        except Exception:
            logger.warning(
                "TierA 执行失败 kpi=%s datasource_id=%s elapsed_ms=%d",
                metricCode, datasourceId, _elapsedMs(started), exc_info=True,
            )
            return KpiExecutionResult(
                ok=False, elapsedMs=_elapsedMs(started), reason=REASON_EXECUTION_ERROR
            )

        fetched = tuple(dict(r) for r in (raw or []))
        truncated = len(fetched) > rowLimit
        rows = fetched[:rowLimit] if truncated else fetched
        elapsed = _elapsedMs(started)
        value = firstPresentValue(rows)
        if not rows or value is None:
            logger.warning(
                "TierA 空结果 kpi=%s datasource_id=%s rows=%d elapsed_ms=%d",
                metricCode, datasourceId, len(rows), elapsed,
            )
            return KpiExecutionResult(
                ok=False, rows=rows, rowCount=len(rows), elapsedMs=elapsed,
                reason=REASON_EMPTY_RESULT,
            )

        result = KpiExecutionResult(
            ok=True, rows=rows, value=value, rowCount=len(rows), truncated=truncated,
            elapsedMs=elapsed,
        )
        logger.info(
            "TierA 执行成功 kpi=%s datasource_id=%s rows=%d elapsed_ms=%d truncated=%s",
            metricCode, datasourceId, len(rows), elapsed, truncated,
        )
        self._scheduleEvidence(
            metricCode=metricCode, metricName=metricName, confidence=confidence,
            rows=rows, datasourceId=datasourceId,
        )
        return result

    def _scheduleEvidence(self, **kwargs: Any) -> None:
        """METRIC_RESULT best-effort：调度失败只 warning，不影响主链路。"""
        try:
            self._evidenceScheduler(**kwargs)
        except Exception:
            logger.warning("TierA METRIC_RESULT 证据调度失败（忽略）", exc_info=True)


def _elapsedMs(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
