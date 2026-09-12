"""结构化建议的查询与审核（feat-wiki-knowledge，Phase 8 M5）。

与 ``services/learning/structure_suggester.py`` 分开，理由同 M5 的冲突模块：
**抽取是机器的动作**（读正文、调模型、落待处置建议），**审核是人的动作**（定终态、
写学习反馈），两者的调用方与失败语义不同。

审核结果落两处，各表职责不同（同 ``WikiConflictService``）：

- ``structure_suggestion.status`` —— 表达**当前状态**（这条建议还待处置吗）
- ``learning_feedback``（``mechanism=STRUCTURE``）—— 表达**历史**，并与其他机制的
  反馈同流，机制 4 的接受率（ACCEPTED 占比）直接从这一条流里算

**终态不可逆**（与 M4 的关系审核刻意不同）。关系审核允许撤回确认，因为那只是翻一个
状态位；而接受一条结构化建议意味着这条知识要升到新结构阶段、并（M6 起）物化出一条
可执行规则或流程草稿 —— 「反悔」得连带撤掉那个产物，是另一个动作。此处静默翻转状态
会让建议与产物脱节。故重复处置与反向处置一律 409。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.wiki_learning_models import SUGGESTION_STATUSES, StructureSuggestion
from app.domain.wiki_models import WikiPage
from app.services.learning.feedback_loop import FeedbackLoop
from app.services.learning.progressive_upgrader import (
    applyStructureStage,
    materializeArtifact,
)
from app.services.messages_zh import (
    MSG_WIKI_SUGGESTION_ALREADY_RESOLVED,
    MSG_WIKI_SUGGESTION_NOT_FOUND,
)

logger = logging.getLogger(__name__)

MECHANISM_STRUCTURE = "STRUCTURE"

# 建议的终态与「待处置」。筛选轴用 SUGGESTION_STATUSES 白名单，见 listForPage。
_PENDING = "PENDING"
_ACCEPTED = "ACCEPTED"

# 审核动作 → 目标状态 + 学习反馈动作。
# 接受 = 系统抽得对（CONFIRM）；拒绝 = 抽得不对或这条不该结构化（REJECT）。
# 没有 MODIFY：「改一改再接受」在 M6 的入口上做（物化时编辑规则），
# 而不是在这里改建议本身 —— 建议是模型当时说过的话，不该被人工覆盖成另一个样子。
_ACTIONS: dict[str, tuple[str, str]] = {
    "accept": ("ACCEPTED", "CONFIRM"),
    "reject": ("REJECTED", "REJECT"),
}


class WikiSuggestionService:
    """结构化建议的查询与审核。"""

    async def listForPage(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ) -> tuple[list[StructureSuggestion], int]:
        """列出某条目的建议（可不分页：单条知识的建议数天然是个位数）。

        ``status`` 取 ``SUGGESTION_STATUSES`` 白名单，白名单外的值抛 422 而不是
        返回空列表：静默返回空会让调用方以为「确实没有建议」，而实际是它把筛选值
        拼错了。列表的空态与参数错误必须是两种不同的反馈（同 ``WikiConflictService``）。
        """
        from app.services.wiki_page_service import WikiPageService

        await WikiPageService().getPage(session, pageId)  # 404 早失败

        conditions = [StructureSuggestion.page_id == pageId]
        if status is not None:
            if status not in SUGGESTION_STATUSES:
                raise ValidationError(f"建议状态「{status}」不合法")
            conditions.append(StructureSuggestion.status == status)

        totalStmt = select(func.count()).select_from(StructureSuggestion)
        rowsStmt = select(StructureSuggestion).order_by(
            # 待处置的排前面：这是审核工作台，已处置的只是留痕
            (StructureSuggestion.status != _PENDING),
            StructureSuggestion.suggested_at.desc(),
            StructureSuggestion.id.desc(),
        )
        for cond in conditions:
            totalStmt = totalStmt.where(cond)
            rowsStmt = rowsStmt.where(cond)

        total = (await session.execute(totalStmt)).scalar_one()
        rows = list(
            (await session.execute(rowsStmt.limit(limit))).scalars().all()
        )
        return rows, total

    async def listAll(
        self,
        session: AsyncSession,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[tuple[StructureSuggestion, str | None]], int]:
        """跨条目分页列出建议（带条目标题），供「建议工作台」总览。

        ``listForPage`` 是条目详情里的子列表；这里是独立工作台的入口 —— 审核人
        要先看到「全库有哪些待处置的建议」，而不是逐个条目点进去找。带标题是因为
        列表里只有 ``page_id`` 的话，审核人无法判断该不该接受。

        用 ``outerjoin`` 而不是 ``join``：外键是 ON DELETE CASCADE，正常路径下
        条目必然存在；但真出现孤儿建议时，inner join 会让它从工作台**静默消失**
        —— 一条谁也不处理的待办，比一条标题显示为空的待办危险得多。

        白名单外的 ``status`` 抛 422 而不是返回空列表，理由同 ``listForPage``。
        """
        conditions = []
        if status is not None:
            if status not in SUGGESTION_STATUSES:
                raise ValidationError(f"建议状态「{status}」不合法")
            conditions.append(StructureSuggestion.status == status)

        totalStmt = select(func.count()).select_from(StructureSuggestion)
        rowsStmt = (
            select(StructureSuggestion, WikiPage.title)
            .outerjoin(WikiPage, WikiPage.page_id == StructureSuggestion.page_id)
            .order_by(
                # 待处置的排前面：这是审核工作台，已处置的只是留痕
                (StructureSuggestion.status != _PENDING),
                StructureSuggestion.suggested_at.desc(),
                StructureSuggestion.id.desc(),
            )
        )
        for cond in conditions:
            totalStmt = totalStmt.where(cond)
            rowsStmt = rowsStmt.where(cond)

        total = (await session.execute(totalStmt)).scalar_one()
        rows = (
            await session.execute(rowsStmt.limit(limit).offset(offset))
        ).all()
        return [(row[0], row[1]) for row in rows], total

    async def accept(
        self, session: AsyncSession, suggestionId: int, *, userId: int | None = None
    ) -> StructureSuggestion:
        """接受一条建议（写学习反馈 CONFIRM）。"""
        return await self._resolve(session, suggestionId, action="accept", userId=userId)

    async def reject(
        self, session: AsyncSession, suggestionId: int, *, userId: int | None = None
    ) -> StructureSuggestion:
        """拒绝一条建议（写学习反馈 REJECT）。"""
        return await self._resolve(session, suggestionId, action="reject", userId=userId)

    async def _resolve(
        self,
        session: AsyncSession,
        suggestionId: int,
        *,
        action: str,
        userId: int | None,
    ) -> StructureSuggestion:
        """审核的共用落库路径：盖终态 + 记处置人 + 写反馈。

        状态改写与反馈**同事务**（由本方法统一 commit），否则会出现「状态已终态、
        反馈没落」的历史空洞 —— 而反馈流正是算机制 4 接受率的输入。
        """
        newStatus, feedbackAction = _ACTIONS[action]
        entity = await self._getSuggestion(session, suggestionId)
        if entity.status != _PENDING:
            raise ConflictError(
                MSG_WIKI_SUGGESTION_ALREADY_RESOLVED.format(
                    suggestionId=suggestionId, status=entity.status
                )
            )

        # 快照必须在改写**之前**取：改完再读，「这条被拒的建议当时建议的是什么维度、
        # 抽出了什么结构」就没了，而那个正是训练要的配对。
        snapshot = _suggestionSnapshot(entity)

        entity.status = newStatus
        entity.resolved_at = datetime.now(UTC)
        entity.resolved_by_user_id = userId

        await FeedbackLoop().record(
            session,
            mechanism=MECHANISM_STRUCTURE,
            entityType="SUGGESTION",
            entityId=str(entity.id),
            userAction=feedbackAction,
            # 与 M4/M5 冲突同理：系统的「输入」是那条知识本身，由 pageId 指回去，
            # 在反馈里再抄一份正文只会造成两份真相。
            inputSnapshot=None,
            systemOutput=snapshot,
            userModification=None,
            userId=userId,
        )

        # M6 机制 5：接受即物化。RULE → 可执行规则，PROCESS → 流程；METRIC/CONCEPT
        # 不产物（返回 None）。物化与阶段同步都在**同一个事务**里，与状态改写、
        # 反馈写入一起提交 —— 否则会出现「建议已终态、产物没落」的脱节，而建议
        # 终态不可逆，事后没有任何入口能补上这个产物。
        if newStatus == _ACCEPTED:
            await materializeArtifact(session, entity)
            await applyStructureStage(session, entity.page_id)

        await session.commit()
        await session.refresh(entity)
        return entity

    async def _getSuggestion(
        self, session: AsyncSession, suggestionId: int
    ) -> StructureSuggestion:
        """取建议并**加行锁**（``SELECT ... FOR UPDATE``）。

        与 M4/M5 冲突同一理由：审核是「先读状态 → 判重复 → 再改写」的
        check-then-write，不加锁时两个并发请求会双双通过检查，各写一条反馈事件
        —— 建议状态最终一致，但反馈流里多出一条重复事件，而反馈流是算接受率的
        输入。第二个事务会在锁上阻塞，等前者提交后重新读到已终态的行，于是正确
        地抛 409。
        """
        result = await session.execute(
            select(StructureSuggestion)
            .where(StructureSuggestion.id == suggestionId)
            .with_for_update()
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise NotFoundError(
                MSG_WIKI_SUGGESTION_NOT_FOUND.format(suggestionId=suggestionId)
            )
        return entity


def _suggestionSnapshot(entity: StructureSuggestion) -> dict[str, Any]:
    """建议的完整快照（写进反馈的 system_output 栏）。"""
    return {
        "pageId": entity.page_id,
        "suggestedDimension": entity.suggested_dimension,
        "extractedStructure": entity.extracted_structure,
        "confidence": float(entity.confidence) if entity.confidence is not None else None,
    }


__all__ = ["WikiSuggestionService", "MECHANISM_STRUCTURE"]
