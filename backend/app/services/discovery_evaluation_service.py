"""发现评测（WP-ONT-AI A3 / G4-2）。

评测 = 「一次运行的候选质量摘要 + 结论」，落 `discovery_evaluation`。

## 指标口径（可复算，不引入人工打分）

- `candidateCount` / `findingCount` / `byType`：直接数行；
- `evidenceCompleteRate`：候选证据里能**在本次剖析事实中解析到 (table, column)**
  的比例。证据本身已在候选入库时校验过一次（指向不存在的列整条丢弃），
  这里是对**已落库数据**的再次复核（防绕过写路径的脏数据 / 后续人工改库）；
- `unresolvableEvidenceCandidates`：证据解析不了的候选 id 列表（最多记 20 个）。

## verdict 三态

| 结论 | 条件 | 含义 |
|---|---|---|
| `PASS` | 有候选且证据完整率 = 1 | 可用于业务签收评审 |
| `FAIL` | 有候选但存在证据解析不了的候选 | 质量不合格，需修数据/重跑 |
| `INCONCLUSIVE` | **没有候选** | 没结论（可能只是没跑出东西）——不等于质量差 |

## 不做什么

不做「候选准确率 / 召回率」：那需要业务标注集（G5 试点签收后才存在）。
本模块只提供**不依赖人工标注**的可复算指标；标注类指标由业务在 G5 补。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryEvaluation,
    DiscoveryFinding,
    DiscoveryProfile,
    DiscoveryRun,
)
from app.domain.exceptions import NotFoundError, ValidationError

logger = logging.getLogger(__name__)

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICTS = frozenset({VERDICT_PASS, VERDICT_FAIL, VERDICT_INCONCLUSIVE})

MSG_RUN_NOT_FOUND = "发现运行 {runId} 不存在"
MSG_DATASET_VERSION_REQUIRED = "评测数据集版本不能为空（最多 64 字）"
MSG_VERDICT_INVALID = "评测结论非法：{verdict}（仅 PASS / FAIL / INCONCLUSIVE）"
MSG_METRICS_NOT_OBJECT = "评测指标必须是 JSON 对象"

_MAX_DATASET_VERSION = 64
_MAX_UNRESOLVABLE_IDS = 20


@dataclass(frozen=True, slots=True)
class RunEvaluation:
    """一次运行的可复算评测摘要。"""

    runId: int
    candidateCount: int
    findingCount: int
    byType: dict[str, int]
    evidenceCompleteRate: float
    unresolvableEvidenceCandidates: tuple[int, ...]
    verdict: str

    def asMetrics(self) -> dict[str, Any]:
        return {
            "runId": self.runId,
            "candidateCount": self.candidateCount,
            "findingCount": self.findingCount,
            "byType": dict(sorted(self.byType.items())),
            "evidenceCompleteRate": self.evidenceCompleteRate,
            "unresolvableEvidenceCandidates": list(self.unresolvableEvidenceCandidates),
        }


class DiscoveryEvaluationService:
    """评测计算与落表。"""

    async def evaluateRun(
        self,
        session: AsyncSession,
        *,
        runId: int,
        datasetVersion: str,
        proposalId: int | None = None,
        note: str | None = None,
        createdBy: str | None = None,
    ) -> DiscoveryEvaluation:
        normalizedVersion = _normalizeDatasetVersion(datasetVersion)
        run = await session.get(DiscoveryRun, runId)
        if run is None:
            raise NotFoundError(MSG_RUN_NOT_FOUND.format(runId=runId))
        candidates = list(
            (
                await session.execute(
                    select(DiscoveryCandidate).where(DiscoveryCandidate.run_id == runId)
                )
            )
            .scalars()
            .all()
        )
        findingCount = await self._countFindings(session, runId)
        facts = await self._factIndex(session, runId)
        summary = _summarize(runId, candidates, findingCount, facts)
        row = DiscoveryEvaluation(
            proposal_id=proposalId,
            dataset_version=normalizedVersion,
            metrics_json=summary.asMetrics(),
            verdict=summary.verdict,
            note=(note or "").strip() or None,
            created_by=createdBy,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info(
            "发现评测已落库：run=%s dataset=%s verdict=%s rate=%s",
            runId,
            normalizedVersion,
            summary.verdict,
            summary.evidenceCompleteRate,
        )
        return row

    async def recordEvaluation(
        self,
        session: AsyncSession,
        *,
        datasetVersion: str,
        metrics: Mapping[str, Any],
        verdict: str,
        proposalId: int | None = None,
        note: str | None = None,
        createdBy: str | None = None,
    ) -> DiscoveryEvaluation:
        """人工/脚本上报的评测（指标由调用方给，本方法只做契约校验）。"""
        normalizedVersion = _normalizeDatasetVersion(datasetVersion)
        if not isinstance(metrics, Mapping):
            raise ValidationError(MSG_METRICS_NOT_OBJECT)
        normalizedVerdict = (verdict or "").strip().upper()
        if normalizedVerdict not in VERDICTS:
            raise ValidationError(MSG_VERDICT_INVALID.format(verdict=verdict))
        row = DiscoveryEvaluation(
            proposal_id=proposalId,
            dataset_version=normalizedVersion,
            metrics_json=dict(metrics),
            verdict=normalizedVerdict,
            note=(note or "").strip() or None,
            created_by=createdBy,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row

    async def _countFindings(self, session: AsyncSession, runId: int) -> int:
        rows = (
            await session.execute(
                select(DiscoveryFinding.id).where(DiscoveryFinding.run_id == runId)
            )
        ).scalars().all()
        return len(rows)

    async def _factIndex(
        self, session: AsyncSession, runId: int
    ) -> dict[str, frozenset[str | None]]:
        rows = (
            await session.execute(
                select(DiscoveryProfile.table_name, DiscoveryProfile.column_name).where(
                    DiscoveryProfile.run_id == runId
                )
            )
        ).all()
        index: dict[str, set[str | None]] = {}
        for tableName, columnName in rows:
            index.setdefault(str(tableName), set()).add(
                None if columnName is None else str(columnName)
            )
        return {table: frozenset(columns) for table, columns in index.items()}


# ---------------------------------------------------------------------------
# 内部：指标与判定
# ---------------------------------------------------------------------------


def _summarize(
    runId: int,
    candidates: list[DiscoveryCandidate],
    findingCount: int,
    facts: Mapping[str, frozenset[str | None]],
) -> RunEvaluation:
    byType = Counter(str(candidate.candidate_type) for candidate in candidates)
    unresolvable: list[int] = []
    for candidate in candidates:
        if not _evidenceResolvable(candidate.evidence_refs, facts):
            unresolvable.append(candidate.id)
    total = len(candidates)
    rate = 1.0 if total == 0 else 1 - len(unresolvable) / total
    if total == 0:
        verdict = VERDICT_INCONCLUSIVE
    elif unresolvable:
        verdict = VERDICT_FAIL
    else:
        verdict = VERDICT_PASS
    return RunEvaluation(
        runId=runId,
        candidateCount=total,
        findingCount=findingCount,
        byType=dict(byType),
        evidenceCompleteRate=round(rate, 6),
        unresolvableEvidenceCandidates=tuple(unresolvable[:_MAX_UNRESOLVABLE_IDS]),
        verdict=verdict,
    )


def _evidenceResolvable(
    evidence: Any, facts: Mapping[str, frozenset[str | None]]
) -> bool:
    if not isinstance(evidence, list) or not evidence:
        return False
    for ref in evidence:
        if not isinstance(ref, Mapping):
            return False
        table = ref.get("table")
        if not isinstance(table, str) or table not in facts:
            return False
        column = ref.get("column")
        if column is not None and str(column) not in facts[table]:
            return False
    return True


def _normalizeDatasetVersion(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized or len(normalized) > _MAX_DATASET_VERSION:
        raise ValidationError(MSG_DATASET_VERSION_REQUIRED)
    return normalized
