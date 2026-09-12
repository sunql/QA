"""知识冲突的查询与处置（feat-wiki-knowledge，Phase 8 M5）。

与 ``services/learning/conflict_detector.py`` 分开，理由同 M4 的关系审核：
**检测是机器的动作**（产出待处理记录、写候选），**处置是人的动作**（改状态、
写学习反馈），两者的调用方、事务边界、失败语义都不同。

处置动作会**同时**落两处，这不是冗余，是两张表的职责不同：

- ``knowledge_conflict`` 自身记 ``resolved_at`` / ``resolution_action``
  ——表达**当前状态**（这条冲突还开着吗）
- ``learning_feedback`` 记一条 ``mechanism=CONFLICT`` 的事件
  ——表达**历史**（谁在什么时候怎么处置的），并与其他机制的反馈同流，
  机制 3 的准确率（IGNORED 占比 = 误报率）直接从这一条流里算

动作映射到反馈的语义：``RESOLVED`` / ``MERGED`` = 系统报得对（CONFIRM）；
``IGNORED`` = 系统误报（REJECT）。这正是「IGNORED 与 RESOLVED 必须分开」的
原因——合并成「已关闭」，误报率就永远算不出来。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.wiki_learning_models import (
    CONFLICT_RESOLUTION_ACTIONS,
    CONFLICT_SEVERITIES,
    CONFLICT_TYPES,
    KnowledgeConflict,
)
from app.services.learning.feedback_loop import FeedbackLoop
from app.services.messages_zh import (
    MSG_WIKI_CONFLICT_ACTION_INVALID,
    MSG_WIKI_CONFLICT_ALREADY_RESOLVED,
    MSG_WIKI_CONFLICT_NOT_FOUND,
)

logger = logging.getLogger(__name__)

MECHANISM_CONFLICT = "CONFLICT"

# 列表的「状态」筛选轴。刻意不用 ``OPEN`` 以外的措辞（如 PENDING）：冲突没有
# 「进行中」这类中间态，只有「还没处理」与「处理过了」两种。
CONFLICT_LIST_STATUSES: tuple[str, ...] = ("OPEN", "RESOLVED")

# 处置动作 → 学习反馈动作。见模块 docstring。
_ACTION_TO_FEEDBACK: dict[str, str] = {
    "RESOLVED": "CONFIRM",
    "MERGED": "CONFIRM",
    "IGNORED": "REJECT",
}


class WikiConflictService:
    """冲突的查询与处置。"""

    async def listConflicts(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        severity: str | None = None,
        conflictType: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[KnowledgeConflict], int]:
        """分页列出冲突；``status`` 取 OPEN / RESOLVED，``None`` 表示不过滤。

        白名单外的 ``status`` / ``severity`` / ``conflictType`` 一律 422 而不是
        「返回空列表」：静默返回空会让调用方以为「确实没有」，而实际是它把筛选值
        拼错了。看板的空态与参数错误必须是两种不同的反馈。
        """
        conditions = []
        if status is not None:
            if status not in CONFLICT_LIST_STATUSES:
                raise ValidationError(f"冲突状态「{status}」不合法")
            conditions.append(
                KnowledgeConflict.resolved_at.is_(None)
                if status == "OPEN"
                else KnowledgeConflict.resolved_at.is_not(None)
            )
        if severity is not None:
            if severity not in CONFLICT_SEVERITIES:
                raise ValidationError(f"冲突严重度「{severity}」不合法")
            conditions.append(KnowledgeConflict.severity == severity)
        if conflictType is not None:
            if conflictType not in CONFLICT_TYPES:
                raise ValidationError(f"冲突类型「{conflictType}」不合法")
            conditions.append(KnowledgeConflict.conflict_type == conflictType)

        totalStmt = select(func.count()).select_from(KnowledgeConflict)
        rowsStmt = select(KnowledgeConflict).order_by(
            KnowledgeConflict.resolved_at.is_(None).desc(),
            KnowledgeConflict.auto_detected_at.desc(),
            KnowledgeConflict.id.desc(),
        )
        for cond in conditions:
            totalStmt = totalStmt.where(cond)
            rowsStmt = rowsStmt.where(cond)

        total = (await session.execute(totalStmt)).scalar_one()
        rows = list(
            (await session.execute(rowsStmt.limit(limit).offset(offset))).scalars().all()
        )
        return rows, total

    async def listForPage(
        self, session: AsyncSession, pageId: str, *, limit: int = 50
    ) -> list[KnowledgeConflict]:
        """列出与某条目相关的全部冲突（未处置的排前面）。

        用数组包含（``@>``，命中 ``ix_knowledge_conflict_page_ids`` 这个 GIN 索引）
        而不是把 ``page_ids`` 拉回来在内存里筛：后者要全表扫描，而冲突表随检测
        次数单调增长。
        """
        from app.services.wiki_page_service import WikiPageService

        await WikiPageService().getPage(session, pageId)  # 404 早失败
        result = await session.execute(
            select(KnowledgeConflict)
            .where(KnowledgeConflict.page_ids.contains([pageId]))
            .order_by(
                KnowledgeConflict.resolved_at.is_(None).desc(),
                KnowledgeConflict.auto_detected_at.desc(),
                KnowledgeConflict.id.desc(),
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def resolve(
        self,
        session: AsyncSession,
        conflictId: int,
        *,
        action: str,
        userId: int | None = None,
    ) -> KnowledgeConflict:
        """处置一条冲突：盖 ``resolved_at`` + 记动作 + 写一条学习反馈。

        重复处置抛 409（与 M4 的审核幂等语义一致）：静默改写上一次结论会让
        「谁在什么时候认定它是误报」这段历史被覆盖，而误报率正是靠它算出来的。
        """
        if action not in CONFLICT_RESOLUTION_ACTIONS:
            raise ValidationError(MSG_WIKI_CONFLICT_ACTION_INVALID.format(action=action))

        entity = await self._getConflict(session, conflictId)
        if entity.resolved_at is not None:
            raise ConflictError(
                MSG_WIKI_CONFLICT_ALREADY_RESOLVED.format(
                    conflictId=conflictId, action=entity.resolution_action
                )
            )

        # 快照必须在改写**之前**取：改完再读，冲突当时的严重度/检出方就没了，
        # 而「这条被用户判为误报的冲突是什么类型、哪个机制报的」正是训练要的。
        snapshot = _conflictSnapshot(entity)

        entity.resolved_at = datetime.now(UTC)
        entity.resolution_action = action
        entity.resolved_by_user_id = userId

        # 状态改写与反馈同事务：先 flush 由本方法统一 commit，避免只落一半
        await FeedbackLoop().record(
            session,
            mechanism=MECHANISM_CONFLICT,
            entityType="CONFLICT",
            entityId=str(entity.id),
            userAction=_ACTION_TO_FEEDBACK[action],
            # 与 M4 同理：系统的「输入」是这两条知识本身，由 snapshot 里的
            # pageIds 指回去，在反馈里再抄一份正文只会造成两份真相。
            inputSnapshot=None,
            systemOutput=snapshot,
            userModification=None,
            userId=userId,
        )
        await session.commit()
        await session.refresh(entity)
        return entity

    async def _getConflict(
        self, session: AsyncSession, conflictId: int
    ) -> KnowledgeConflict:
        """取冲突并**加行锁**（``SELECT ... FOR UPDATE``）。

        与 M4 的 ``WikiRelationService._getRelation`` 同一个理由：处置是
        「先读状态 → 判重复 → 再改写」的 check-then-write，不加锁时两个并发请求
        会双双通过重复性检查，各写一条反馈事件——冲突状态最终一致，但反馈流里
        多出一条重复事件，而反馈流是算误报率的输入。第二个事务会在锁上阻塞，
        等前者提交后重新读到已处置的行，于是正确地抛 409。
        """
        result = await session.execute(
            select(KnowledgeConflict)
            .where(KnowledgeConflict.id == conflictId)
            .with_for_update()
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise NotFoundError(MSG_WIKI_CONFLICT_NOT_FOUND.format(conflictId=conflictId))
        return entity


def _conflictSnapshot(entity: KnowledgeConflict) -> dict[str, Any]:
    """冲突的完整快照（写进反馈的 system_output 栏）。"""
    return {
        "conflictType": entity.conflict_type,
        "pageIds": list(entity.page_ids),
        "severity": entity.severity,
        "detectedBy": entity.detected_by,
        "description": entity.description,
    }


__all__ = ["WikiConflictService", "CONFLICT_LIST_STATUSES", "MECHANISM_CONFLICT"]
