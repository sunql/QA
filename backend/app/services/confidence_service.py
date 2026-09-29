"""M3 置信度 4 级（v3.1 蓝图 §12.2 / 任务 B4）。

离散四级 HIGH / MEDIUM / LOW / REFUSE 淘汰 0-1 伪精确展示：

- 前置三项（无证据 / 数据未更新 / 多源冲突）任一命中 → REFUSE（必给具体原因）
- 计分四项（完整证据 / 规则匹配 / 历史使用量≥10 / 历史准确率>0.9）求和，
  映射 3:HIGH 2:MEDIUM 1:LOW 0:REFUSE；全项命中封顶 HIGH（score 0-3）
- 纯函数 + 全参数注入：``calculateConfidence`` 的输入全是标量/快照，
  不做 DB 查询（unit 测试零 DB 依赖）；查询由编排层 ``calculateClaimConfidence``
  / ``calculatePageClaimConfidence`` 完成
- 历史两项以注入式 provider（``ConfidenceHistorySnapshot``）供给：v1 生产无
  claim 级学习闭环数据源（learning_feedback.entity_type 不含 claim），调用方
  传 None → 冷启动豁免（两项结构性不可达）；未来接学习闭环只需换 provider
- 表级 freshness（借 DQ 评分时效字段）v1 不做：REFUSE 的 UI 话术统一注明
  「表级时效未评估」（见 ``refuseReasonForUi``）

LLM 注入 helper（``formatConfidenceForPrompt``）当前无生产消费者
（wiki_qa_service 零 claim 引用），仅供未来特性接线，绝不输出数值。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy import select

from app.domain.wiki_models import Evidence

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# 算法常量（蓝图 §12.2）
# ---------------------------------------------------------------------------

HIGH_SCORE = 3
MIN_HISTORY_USAGE = 10
AccuracyThreshold = 0.9

REFUSE_NO_EVIDENCE = "无证据，无法判定"
REFUSE_DATA_STALE = "数据未更新（三元组已标记过期）"
REFUSE_CONFLICT = "存在未解决的多源知识冲突"
REFUSE_INSUFFICIENT = "证据与规则匹配均不足，无法判定"
# v1 只评估 claim 级 triple_stale，表级时效未评估——REFUSE 话术统一注明
REFUSE_TABLE_STALENESS_NOTE = "表级时效未评估（v1 仅评估 claim 级）"

class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    REFUSE = "REFUSE"


_LEVEL_BY_SCORE = {3: ConfidenceLevel.HIGH, 2: ConfidenceLevel.MEDIUM, 1: ConfidenceLevel.LOW, 0: ConfidenceLevel.REFUSE}

_LEVEL_PROMPT_PHRASE = {
    ConfidenceLevel.HIGH: "高置信度，可作为决策依据",
    ConfidenceLevel.MEDIUM: "中等置信度，建议复核",
    ConfidenceLevel.LOW: "低置信度，仅供参考",
    ConfidenceLevel.REFUSE: "无法判定",
}


# ---------------------------------------------------------------------------
# 数据容器（全部 frozen：不可变数据红线）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceHistorySnapshot:
    """注入式历史 provider 快照（v1 生产调用方传 None = 冷启动豁免）。"""

    usageCount: int
    accuracy: float | None


@dataclass(frozen=True)
class ConfidenceBreakdown:
    """各前置/计分项的布尔明细（可解释性）。"""

    hasEvidence: bool
    isStale: bool
    hasOpenConflict: bool
    hasCompleteEvidence: bool
    rulesMatched: bool
    hasSufficientHistory: bool
    hasHighHistoricalAccuracy: bool
    score: int


@dataclass(frozen=True)
class ConfidenceResult:
    """置信度判定结果；REFUSE 时 ``refuseReason`` 必填具体原因（面向 UI）。"""

    level: ConfidenceLevel
    score: int
    refuseReason: str | None
    breakdown: ConfidenceBreakdown


# ---------------------------------------------------------------------------
# 纯函数核心
# ---------------------------------------------------------------------------


def _historyPoints(
    snapshot: ConfidenceHistorySnapshot | None,
) -> tuple[int, bool, bool]:
    """历史两项计分；snapshot=None（冷启动）时结构性不可达。"""
    if snapshot is None:
        return 0, False, False
    hasSufficient = snapshot.usageCount >= MIN_HISTORY_USAGE
    isAccurate = snapshot.accuracy is not None and snapshot.accuracy > AccuracyThreshold
    return int(hasSufficient) + int(isAccurate), hasSufficient, isAccurate


def _refuse(refuseReason: str, breakdown: ConfidenceBreakdown) -> ConfidenceResult:
    return ConfidenceResult(
        level=ConfidenceLevel.REFUSE, score=0, refuseReason=refuseReason, breakdown=breakdown
    )


def calculateConfidence(
    *,
    evidenceCount: int,
    isStale: bool,
    hasOpenConflict: bool,
    hasCompleteEvidence: bool,
    rulesMatched: bool,
    historySnapshot: ConfidenceHistorySnapshot | None = None,
) -> ConfidenceResult:
    """蓝图 §12.2 逐字实现（纯函数，无 DB / 无 LLM）。"""
    hasEvidence = evidenceCount > 0
    historyPoint, hasSufficient, isAccurate = _historyPoints(historySnapshot)
    breakdown = ConfidenceBreakdown(
        hasEvidence=hasEvidence,
        isStale=isStale,
        hasOpenConflict=hasOpenConflict,
        hasCompleteEvidence=hasCompleteEvidence,
        rulesMatched=rulesMatched,
        hasSufficientHistory=hasSufficient,
        hasHighHistoricalAccuracy=isAccurate,
        score=0,
    )
    if not hasEvidence:
        return _refuse(REFUSE_NO_EVIDENCE, breakdown)
    if isStale:
        return _refuse(REFUSE_DATA_STALE, breakdown)
    if hasOpenConflict:
        return _refuse(REFUSE_CONFLICT, breakdown)
    score = min(
        int(hasCompleteEvidence) + int(rulesMatched) + historyPoint,
        HIGH_SCORE,
    )
    breakdown = replace(breakdown, score=score)
    if score == 0:
        return _refuse(REFUSE_INSUFFICIENT, breakdown)
    return ConfidenceResult(
        level=_LEVEL_BY_SCORE[score], score=score, refuseReason=None, breakdown=breakdown
    )


def refuseReasonForUi(result: ConfidenceResult) -> str | None:
    """REFUSE 的 UI 话术：具体原因 + 表级时效未评估注记；非 REFUSE 恒 None。"""
    if result.level is not ConfidenceLevel.REFUSE:
        return None
    reason = result.refuseReason or REFUSE_INSUFFICIENT
    return f"{reason}（{REFUSE_TABLE_STALENESS_NOTE}）"


# ---------------------------------------------------------------------------
# rules_matched：_hasTriple 判定提取的可复用纯函数
# （WikiPageService._hasTriple 改为委托这里，避免私有符号导入——A5 R1 教训）
# ---------------------------------------------------------------------------


def hasTripleFilled(
    *,
    subjectId: str | None,
    predicate: str | None,
    objectValue: str | None,
    objectType: str | None,
) -> bool:
    """三元组核心字段（subject_id/predicate/object_value/object_type）任一非空。"""
    return any(bool(v) for v in (subjectId, predicate, objectValue, objectType))


def claimRulesMatched(claim: Any) -> bool:
    """claim 级 rules_matched 判定（鸭子类型，避免 import ORM 造成耦合）。"""
    return hasTripleFilled(
        subjectId=getattr(claim, "subject_id", None),
        predicate=getattr(claim, "predicate", None),
        objectValue=getattr(claim, "object_value", None),
        objectType=getattr(claim, "object_type", None),
    )


# ---------------------------------------------------------------------------
# has_complete_evidence：≥1 条且每条 payload / source_id 至少一项非空
# ---------------------------------------------------------------------------


def _evidenceHasAnchor(payload: Any, sourceId: Any) -> bool:
    """单条证据锚点判定：payload（含空 dict 视为空）或 source_id 至少一项非空。"""
    if payload:
        return True
    return bool(sourceId)


def evaluateEvidenceCompleteness(evidences: Iterable[Any]) -> bool:
    """≥1 条证据且**每条**都有锚点才算完整；空列表 False。"""
    rows = [
        (
            getattr(evidence, "payload", None),
            getattr(evidence, "source_id", None),
        )
        for evidence in evidences
    ]
    if not rows:
        return False
    return all(_evidenceHasAnchor(payload, sourceId) for payload, sourceId in rows)


# ---------------------------------------------------------------------------
# 编排包装（DB 查询只在这一层；纯函数之上）
# ---------------------------------------------------------------------------


async def pageHasOpenConflict(session: AsyncSession, pageId: str) -> bool:
    """knowledge_conflict 存在未解决且 page_ids 包含 pageId 的行。

    用数组包含 ``@>``（走 ix_knowledge_conflict_page_ids GIN 索引）；
    只读查询，不改 wiki_conflict_service（B4 硬约束 8）。
    """
    from app.domain.wiki_learning_models import KnowledgeConflict

    result = await session.execute(
        select(KnowledgeConflict.id)
        .where(KnowledgeConflict.resolved_at.is_(None))
        .where(KnowledgeConflict.page_ids.contains([pageId]))
        .limit(1)
    )
    return result.first() is not None


async def calculateClaimConfidence(
    session: AsyncSession,
    claim: Any,
    *,
    hasOpenConflict: bool | None = None,
    evidences: Iterable[Any] | None = None,
    historySnapshot: ConfidenceHistorySnapshot | None = None,
) -> ConfidenceResult:
    """单条 claim 的置信度编排：取证据数、查冲突、组参数后调纯函数。

    ``evidences=None`` 时按 claim_id 查 evidence 表（显式列查询，避免 async
    上下文触发惰性加载 MissingGreenlet）；``hasOpenConflict=None`` 时现查；
    ``historySnapshot`` 生产 v1 恒 None（冷启动豁免），未来接学习闭环时由
    provider 注入。
    """
    if hasOpenConflict is None:
        hasOpenConflict = await pageHasOpenConflict(session, claim.page_id)
    if evidences is None:
        result = await session.execute(
            select(Evidence.payload, Evidence.source_id).where(Evidence.claim_id == claim.id)
        )
        rows = result.all()
    else:
        rows = [(e.payload, e.source_id) for e in evidences]
    return calculateConfidence(
        evidenceCount=len(rows),
        isStale=bool(claim.triple_stale),
        hasOpenConflict=hasOpenConflict,
        hasCompleteEvidence=evaluateEvidenceCompleteness(
            [SimpleNamespace(payload=payload, source_id=sourceId) for payload, sourceId in rows]
        ),
        rulesMatched=claimRulesMatched(claim),
        historySnapshot=historySnapshot,
    )


async def calculatePageClaimConfidence(
    session: AsyncSession,
    pageId: str,
    claims: Iterable[Any],
    *,
    historySnapshot: ConfidenceHistorySnapshot | None = None,
) -> dict[int, ConfidenceResult]:
    """一个 page 的 N 条 claim：冲突查询只做一次（禁 N+1）。

    要求 claims 的 ``evidences`` 关系已预取（listClaims 的 selectinload），
    否则会触发 async 惰性加载异常。
    """
    hasOpenConflict = await pageHasOpenConflict(session, pageId)
    return {
        claim.id: await calculateClaimConfidence(
            session,
            claim,
            hasOpenConflict=hasOpenConflict,
            evidences=claim.evidences,
            historySnapshot=historySnapshot,
        )
        for claim in claims
    }


# ---------------------------------------------------------------------------
# LLM 注入 helper（只做 helper，不接线；当前无生产消费者）
# ---------------------------------------------------------------------------


def formatConfidenceForPrompt(result: ConfidenceResult) -> str:
    """prompt 用一行话术：级别词 + 一句话，绝不输出数值（淘汰伪精确）。"""
    phrase = _LEVEL_PROMPT_PHRASE[result.level]
    if result.level is ConfidenceLevel.REFUSE:
        reason = result.refuseReason or REFUSE_INSUFFICIENT
        return f"置信度：REFUSE——无法判定，原因：{reason}"
    return f"置信度：{result.level.value}——{phrase}"
