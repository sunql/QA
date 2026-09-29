"""知识关系审核（feat-wiki-knowledge，Phase 8 M4）。

机制 2 产出的是**候选**，本服务负责把它们变成「生效」或「打回」。职责刻意
收得很窄：只有审核（confirm/reject），不含发现（在
``services/learning/relation_discovery.py``）。分开的理由是审核是**人的动作**、
要写学习反馈，发现是**机器的动作**、只写候选；两者的调用方、事务边界、
失败语义都不同。

审核动作同时是学习闭环的输入：每次 confirm/reject 都会在
``learning_feedback`` 落一条 ``mechanism=RELATE`` 的事件，带上当时的候选快照
（含置信度）——用于日后回答「多高的置信度阈值才不必人工审核」。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.wiki_models import KnowledgeRelation
from app.services.learning.feedback_loop import FeedbackLoop
from app.services.learning.progressive_upgrader import applyStructureStage
from app.services.messages_zh import (
    MSG_WIKI_RELATION_ALREADY_REVIEWED,
    MSG_WIKI_RELATION_NOT_FOUND,
    MSG_WIKI_RELATION_REVIEW_ACTION_INVALID,
)

logger = logging.getLogger(__name__)

# 审核动作白名单。与 learning_feedback 的 user_action 前两项刻意同名，
# 但**不复用**那个词表：feedback 的 MODIFY 在关系审核里没有对应动作，
# 把两个语义相近的集合绑在一起，日后加动作时会互相牵扯。
RELATION_REVIEW_ACTIONS: tuple[str, ...] = ("CONFIRM", "REJECT")

MECHANISM_RELATE = "RELATE"


class WikiRelationService:
    """关系候选的审核（confirm / reject）。"""

    async def review(
        self,
        session: AsyncSession,
        relationId: int,
        *,
        action: str,
        userId: int | None = None,
    ) -> KnowledgeRelation:
        """审核一条候选关系，返回更新后的行。

        - ``CONFIRM``：``confirmed=True`` 并清掉 ``rejected_at``
          （允许「打回后反悔」，方案 §设计原则 4「可逆」）
        - ``REJECT``：``rejected_at`` 盖章，并把 ``confirmed`` 置回 False
          ——**对已确认的关系执行 REJECT 会撤销确认**，这不是重复审核而是
          反向操作（UI 上是「撤回确认」）。对称于上面那条反悔路径。

        409 只在**重复同一个动作**时抛（已确认再 CONFIRM、已打回再 REJECT）：
        既挡住 UI 双击把反馈流刷成噪声，也避免调用方把「早就确认过了」误读成
        「刚刚确认成功」。反向动作不在此列——它是合法的新意图，要留下反馈。
        """
        if action not in RELATION_REVIEW_ACTIONS:
            raise ValidationError(
                MSG_WIKI_RELATION_REVIEW_ACTION_INVALID.format(action=action)
            )

        entity = await self._getRelation(session, relationId)
        isConfirmed = action == "CONFIRM"
        alreadyConfirmed = entity.confirmed and entity.rejected_at is None
        alreadyRejected = not entity.confirmed and entity.rejected_at is not None
        if (isConfirmed and alreadyConfirmed) or (not isConfirmed and alreadyRejected):
            raise ConflictError(
                MSG_WIKI_RELATION_ALREADY_REVIEWED.format(
                    relationId=relationId, action=action
                )
            )

        # 反馈快照必须在改写**之前**取：改完再读，系统当时输出的置信度就没了
        snapshot = _relationSnapshot(entity)

        if isConfirmed:
            entity.confirmed = True
            entity.rejected_at = None
        else:
            entity.confirmed = False
            entity.rejected_at = datetime.now(UTC)

        # 状态改写与反馈同事务：先 flush 由调用方统一 commit，避免只落一半
        await FeedbackLoop().record(
            session,
            mechanism=MECHANISM_RELATE,
            entityType="RELATION",
            entityId=str(entity.id),
            userAction=action,
            # inputSnapshot 留空是**如实**而非偷懒：审核这条反馈回答的是
            # 「用户如何处置系统的这条输出」，系统的输入（是哪篇正文产出的）
            # 由 upstreamPageId 指回去，在反馈表里再抄一份只会造成两份真相。
            inputSnapshot=None,
            systemOutput=snapshot,
            userModification=None,
            userId=userId,
        )

        # M6 机制 5：关系的确认/打回会改变「这条知识有没有被结构化过」的事实，
        # 故顺带同步一次阶段（**不 commit**，与上面的反馈同事务）。不在这里同步
        # 的话，页面会停在 MARKDOWN —— 那是对当前状态的一句假话，而用户没有任何
        # 理由知道要去点一次「重算」。反向动作（撤回确认）同样要走，否则阶段只升不降。
        await applyStructureStage(session, entity.upstream_page_id)

        await session.commit()
        await session.refresh(entity)
        return entity

    async def _getRelation(
        self, session: AsyncSession, relationId: int
    ) -> KnowledgeRelation:
        """取候选关系并**加行锁**（``SELECT ... FOR UPDATE``）。

        审核是「先读状态、判定是否重复、再改写」的 check-then-write。不加锁时
        两个并发的 confirm 会双双通过重复性检查，各写一条 CONFIRM 反馈——关系
        状态最终一致（都收敛到 confirmed），但反馈流里多出一条重复事件，而
        反馈流是学习闭环的训练输入，重复事件会污染「用户确认率」这类统计。

        锁的语义：第二个事务在此阻塞，等前者提交后**重新读到已更新的行**
        （READ COMMITTED），于是正确地抛 409。副作用是 409 在并发下也成立，
        而不只是顺序调用时的约定。
        """
        result = await session.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.id == relationId)
            .with_for_update()
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise NotFoundError(MSG_WIKI_RELATION_NOT_FOUND.format(relationId=relationId))
        return entity


def _relationSnapshot(entity: KnowledgeRelation) -> dict[str, Any]:
    """候选关系的完整快照（写进反馈的 system_output 栏）。

    ``confidence`` 转成 float 再存：DB 里是 ``Numeric``，直接序列化会得到
    JSON 字符串，下游按数值比较阈值时得先 parse —— 存成数字少一层隐式约定。
    """
    return {
        "upstreamPageId": entity.upstream_page_id,
        "downstreamType": entity.downstream_type,
        "downstreamId": entity.downstream_id,
        "relationType": entity.relation_type,
        "confidence": float(entity.confidence) if entity.confidence is not None else None,
        "autoDetected": entity.auto_detected,
    }


__all__ = ["WikiRelationService", "RELATION_REVIEW_ACTIONS", "MECHANISM_RELATE"]
