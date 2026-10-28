"""B2：KPI 治理契约 ↔ 本体指标技术公式 编译期一致性对账。

提案 `Harness/changes/2026-10-27-kpi-governance-into-nl2sql-proposal.md` §B2。
挂靠在 KnowledgeCompilerService._compileSqlMetadata 的 metric 编译钩子之后，
对每个挂靠 KPI 检查三项（只读，有 findings 才落 audit_log）：

- formula_semantics：numerator/denominator 列名集合 ⊆ metric.formula 列集合；
  结构解析不可判定 → verdict=unknown（不谎称一致，同 cost_source 口径）。
  提案原设计还有「解析不了 → LLM 判等」——knowledge compiler 约束
  「不新增任何 LLM 调用」，v1 不引入 LLM，unknown 诚实标注，LLM 判等延后。
- grain：kpi.grain 是否覆盖 metric.dimension_defaults 键（复用 B5 别名表）。
- formula_mixing：kpi.formula 与 metric.formula 文本相同 → 概念混用嫌疑
  （kpi.formula 的契约语义是 FeatureDefinition 锚文本，不应存 SQL 公式）。

无 findings 不落条目；有 findings 落一行 audit_log（entity_type=
kpi_metric_reconcile），自 commit（与三库对账 reconcile() 同款，避免随调用方
事务回滚丢失巡检结果）。全程不修改 metric / KPI 任何字段，不阻断编译。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.services.audit_service import AuditService

logger = logging.getLogger(__name__)

_RECON_ACTOR = "system:knowledge-compiler"
_RECON_KPI_ENTITY_TYPE = "kpi_metric_reconcile"
_RECON_CHECK_FORMULA = "formula_semantics"
_RECON_CHECK_GRAIN = "grain"
_RECON_CHECK_MIXING = "formula_mixing"
_RECON_VERDICT_MISMATCH = "mismatch"
_RECON_VERDICT_UNKNOWN = "unknown"
_MSG_RECON_FORMULA_MISMATCH = (
    "numerator/denominator 中的列 {extras} 不在 metric.formula 列集合 {cols} 内"
)
_MSG_RECON_FORMULA_UNKNOWN = (
    "numerator/denominator（{kpi_side!r}）与 metric.formula（{metric_side!r}）"
    "结构解析不可判定（无列名可抽取）——未判等，标注 unknown"
)
_MSG_RECON_GRAIN = (
    "grain={grain!r} 未覆盖 metric.dimension_defaults 的键 {missing}"
    "（复用 B5 中文别名表判定）"
)
_MSG_RECON_MIXING = (
    "kpi.formula 与 metric.formula 文本相同——概念混用嫌疑"
    "（kpi.formula 的契约语义是 FeatureDefinition 锚文本，不应存 SQL 公式）"
)

# SQL 标识符抽取：只认 [a-z_][a-z0-9_]* 形态的 token；SQL 关键字/常见函数名剔除。
# numerator/denominator 多为中文业务文本 → 抽不出 token 是常态 → verdict=unknown。
_SQL_IDENTIFIER_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
_SQL_STOPWORDS = frozenset({
    "sum", "count", "avg", "max", "min", "case", "when", "then", "else", "end",
    "and", "or", "not", "null", "as", "on", "by", "distinct", "over", "partition",
    "cast", "interval", "coalesce", "nullif", "round", "abs", "greatest", "least",
    "select", "from", "where", "group", "order", "having", "join", "left", "right",
    "inner", "outer", "desc", "asc", "limit",
})


def _extractSqlIdentifiers(text: str | None) -> set[str]:
    """从公式/声明文本抽取列名候选集合（小写、剔 SQL 关键字与函数名）。"""
    if not text:
        return set()
    return {t.lower() for t in _SQL_IDENTIFIER_RE.findall(text)} - _SQL_STOPWORDS


def _checkFormulaSemantics(kpi: KpiCatalog, metric: OntologyMetric) -> dict | None:
    """numerator/denominator 列集合 ⊆ metric.formula 列集合；判不了 → unknown。

    KPI 侧未声明（numerator/denominator 全空，如 B5 自动 DRAFT 户头）→ 跳过；
    unknown 只留给「有声明但结构解析不出列名」的情形，避免对账噪声。
    """
    if not (kpi.numerator and kpi.numerator.strip()) and not (
        kpi.denominator and kpi.denominator.strip()
    ):
        return None
    kpiSide = " ".join(part for part in (kpi.numerator, kpi.denominator) if part)
    kpiTokens = _extractSqlIdentifiers(kpiSide)
    metricTokens = _extractSqlIdentifiers(metric.formula)
    if not kpiTokens or not metricTokens:
        return {
            "check": _RECON_CHECK_FORMULA,
            "verdict": _RECON_VERDICT_UNKNOWN,
            "detail": _MSG_RECON_FORMULA_UNKNOWN.format(
                kpi_side=kpiSide, metric_side=metric.formula,
            ),
        }
    extras = sorted(kpiTokens - metricTokens)
    if not extras:
        return None
    return {
        "check": _RECON_CHECK_FORMULA,
        "verdict": _RECON_VERDICT_MISMATCH,
        "detail": _MSG_RECON_FORMULA_MISMATCH.format(
            extras=extras, cols=sorted(metricTokens),
        ),
    }


def _checkGrainCoverage(kpi: KpiCatalog, metric: OntologyMetric) -> dict | None:
    """kpi.grain 未覆盖 metric.dimension_defaults 键 → mismatch（弱校验口径）。"""
    from app.services.kpi_catalog_service import grainCoverageGaps

    missing = grainCoverageGaps(kpi.grain, metric.dimension_defaults)
    if not missing:
        return None
    return {
        "check": _RECON_CHECK_GRAIN,
        "verdict": _RECON_VERDICT_MISMATCH,
        "detail": _MSG_RECON_GRAIN.format(grain=kpi.grain, missing=missing),
    }


def _checkFormulaMixing(kpi: KpiCatalog, metric: OntologyMetric) -> dict | None:
    """kpi.formula 与 metric.formula 文本相同 → 概念混用嫌疑（两列契约语义不同）。"""
    if (
        not kpi.formula
        or not kpi.formula.strip()
        or not metric.formula
        or kpi.formula.strip() != metric.formula.strip()
    ):
        return None
    return {
        "check": _RECON_CHECK_MIXING,
        "verdict": _RECON_VERDICT_MISMATCH,
        "detail": _MSG_RECON_MIXING,
    }


async def reconcileKpiContract(
    session: AsyncSession,
    audit: AuditService,
    metric: OntologyMetric,
    kpiRows: list[KpiCatalog],
) -> None:
    """对每个挂靠 KPI 做治理↔技术一致性对账（只读，有 findings 才落 audit）。"""
    findingsByKpi: list[dict] = []
    for kpi in kpiRows:
        findings = [
            finding
            for finding in (
                _checkFormulaSemantics(kpi, metric),
                _checkGrainCoverage(kpi, metric),
                _checkFormulaMixing(kpi, metric),
            )
            if finding is not None
        ]
        if findings:
            findingsByKpi.append({"kpi_code": kpi.kpi_code, "findings": findings})
    if not findingsByKpi:
        return
    await audit.record(
        session,
        entity_type=_RECON_KPI_ENTITY_TYPE,
        entity_id=metric.id,
        action="CREATE",
        actor=_RECON_ACTOR,
        after={"metric_id": metric.id, "kpi_findings": findingsByKpi},
    )
    await session.commit()
    logger.warning(
        "KPI 契约对账发现不一致/不可判定 metric_id=%d：%d 个 KPI 有 findings，"
        "已落 audit（entity_type=%s）",
        metric.id, len(findingsByKpi), _RECON_KPI_ENTITY_TYPE,
    )
