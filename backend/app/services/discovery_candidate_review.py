"""候选/问题的**业务决断**（G5 前台签收的写口）。

前台「发现评审」页每行点「采纳 / 驳回 / 待证」就是调用这里的两个方法：

- `decideCandidate`：`CANDIDATE|NEEDS_EVIDENCE|ACCEPTED|REJECTED` 之间切换，
  记 `decided_by/decided_time` 与 `decision_note`；
- `resolveFinding`：`OPEN|ACKNOWLEDGED|RESOLVED|IGNORED`，记 `decision_note`。

## 允许改判

业务先判「待证」、补证据后改判「采纳」是正常流程 ⇒ 不锁死单向状态机，
只校验取值合法。历史由 `audit_log`（API 层记录）承担，本表只保留**最近一次**决断。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.discovery_pipeline_models import (
    DiscoveryCandidate,
    DiscoveryFinding,
)
from app.domain.exceptions import NotFoundError, ValidationError

CANDIDATE_DECISIONS = frozenset({"ACCEPTED", "REJECTED", "NEEDS_EVIDENCE"})
FINDING_DECISIONS = frozenset({"OPEN", "ACKNOWLEDGED", "RESOLVED", "IGNORED"})

MSG_CANDIDATE_NOT_FOUND = "候选 {id} 不存在"
MSG_CANDIDATE_STATUS_INVALID = (
    "候选决断非法：{status}（仅 ACCEPTED / REJECTED / NEEDS_EVIDENCE）"
)
MSG_FINDING_NOT_FOUND = "问题 {id} 不存在"
MSG_FINDING_STATUS_INVALID = "问题状态非法：{status}（仅 OPEN / ACKNOWLEDGED / RESOLVED / IGNORED）"

MAX_NOTE_LENGTH = 2000


class DiscoveryCandidateReviewService:
    async def decideCandidate(
        self,
        session: AsyncSession,
        *,
        candidateId: int,
        status: str,
        note: str | None = None,
        actor: str,
    ) -> DiscoveryCandidate:
        normalized = (status or "").strip().upper()
        if normalized not in CANDIDATE_DECISIONS:
            raise ValidationError(MSG_CANDIDATE_STATUS_INVALID.format(status=status))
        candidate = await session.get(DiscoveryCandidate, candidateId)
        if candidate is None:
            raise NotFoundError(MSG_CANDIDATE_NOT_FOUND.format(id=candidateId))
        candidate.status = normalized
        candidate.decided_by = actor
        candidate.decided_time = datetime.now(UTC)
        candidate.decision_note = _normalizeNote(note)
        await session.commit()
        await session.refresh(candidate)
        return candidate

    async def resolveFinding(
        self,
        session: AsyncSession,
        *,
        findingId: int,
        status: str,
        note: str | None = None,
        actor: str,
    ) -> DiscoveryFinding:
        normalized = (status or "").strip().upper()
        if normalized not in FINDING_DECISIONS:
            raise ValidationError(MSG_FINDING_STATUS_INVALID.format(status=status))
        finding = await session.get(DiscoveryFinding, findingId)
        if finding is None:
            raise NotFoundError(MSG_FINDING_NOT_FOUND.format(id=findingId))
        finding.status = normalized
        finding.decision_note = _normalizeNote(note)
        await session.commit()
        await session.refresh(finding)
        return finding


def _normalizeNote(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = note.strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_NOTE_LENGTH:
        raise ValidationError(f"备注过长：{len(cleaned)} > {MAX_NOTE_LENGTH}")
    return cleaned
