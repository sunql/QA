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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import getSettings
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


# ---------------------------------------------------------------------------
# B3：KPI 契约块装配（问句匹配 → B2 门禁 → 渲染；注入 nl2sql plan system prompt）
# ---------------------------------------------------------------------------

_KPI_CONTRACT_MAX_KPIS = 3
_KPI_CONTRACT_FIELD_MAX_LEN = 120


def _truncField(text: str | None) -> str | None:
    """治理字段截断（numerator/denominator 是 Text 无界列，prompt 预算封顶）。"""
    if not text or not text.strip():
        return None
    text = " ".join(text.strip().split())
    return text[:_KPI_CONTRACT_FIELD_MAX_LEN]


async def buildKpiContractBlock(
    session: AsyncSession, question: str
) -> str | None:
    """按问句装配 KPI 治理契约块（B3 注入点 B：plan 之前注入，影响选类与聚合）。

    链路：KpiSemanticMatchService.matchAll（KpiMatchCache，只索引 PUBLISHED，
    冷缓存安全降级空列表）→ confidence 降序 Top-3 → 逐个过 B2 门禁 → 渲染条目。

    门禁口径：任何 verdict=mismatch 的检查 → 该 KPI 不注入（宁可少注入不注入
    错的）；unknown 允许注入（治理声明无法被技术公式反驳，恰是治理文本唯一
    可用的场景——诚实口径，不把 unknown 当「不通过」）。

    零回归契约：无匹配 / 空问句 / 开关关闭 → None（调用方 prompt 侧零改动）；
    装配器内部异常由调用方（nl2sql_service）兜底降级，不阻断 plan。
    """
    if not question or not question.strip():
        return None
    if not getSettings().kpiContractInjectionEnabled:
        return None

    from app.services.kpi_match_cache import get_kpi_match_cache
    from app.services.kpi_semantic_match_service import getKpiSemanticMatchService

    matches = await getKpiSemanticMatchService().matchAll(
        question.strip(), limit=_KPI_CONTRACT_MAX_KPIS
    )
    if not matches:
        return None

    codes = [m.code for m in matches]
    rows = (
        await session.execute(
            select(KpiCatalog).where(KpiCatalog.kpi_code.in_(codes))
        )
    ).scalars().all()
    byCode = {row.kpi_code: row for row in rows}

    entries: list[str] = []
    for match in matches:
        kpi = byCode.get(match.code)
        if kpi is None:
            continue
        # B2 门禁：mismatch → 不注入
        metric: OntologyMetric | None = None
        if kpi.metric_id is not None:
            metric = await session.get(OntologyMetric, kpi.metric_id)
        if metric is not None:
            verdicts = [
                finding["verdict"]
                for finding in (
                    _checkFormulaSemantics(kpi, metric),
                    _checkGrainCoverage(kpi, metric),
                    _checkFormulaMixing(kpi, metric),
                )
                if finding is not None
            ]
            if _RECON_VERDICT_MISMATCH in verdicts:
                logger.warning(
                    "KPI %s 对账 mismatch，B3 不注入其治理契约", kpi.kpi_code
                )
                continue
        entry = f"- {_truncField(kpi.kpi_name) or kpi.kpi_code} ({kpi.kpi_code}"
        if getattr(kpi, "version", None):
            entry += f", {kpi.version}"
        entry += ")"
        fields: list[str] = []
        for label, value in (
            ("分子", _truncField(kpi.numerator)),
            ("分母", _truncField(kpi.denominator)),
            ("粒度", _truncField(kpi.grain)),
            ("单位", _truncField(kpi.unit)),
        ):
            if value:
                fields.append(f"{label}: {value}")
        if fields:
            entry += "\n  " + " | ".join(fields)
        if metric is not None:
            formula = _truncField(metric.formula)
            if formula:
                entry += f"\n  技术公式: {formula}（聚合函数 {metric.agg_function}）"
        entries.append(entry)

    if not entries:
        return None
    return "\n".join(entries)
