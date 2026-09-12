"""学习闭环：把「用户如何处置系统建议」写成反馈事件（feat-wiki-knowledge M3）。

**本模块的定位**：机制 1~4 各有一套建议产出，但「用户满不满意」的表达方式只有
一套 —— CONFIRM / REJECT / MODIFY + 改成了什么。所有机制都经由 ``FeedbackLoop``
落反馈，保证口径一致（否则训练数据会按机制各说各话）。

**只写不读**：M3 不提供反馈的查询/训练接口。反馈表是 append-only 的事件流，
消费者是后续的分类器训练任务（用 SQL 直读）。此处刻意不做「建议表」式的
状态机，见 ``wiki_learning_models.LearningFeedback`` 的说明。

**事务边界**：``record`` 只 ``flush`` 不 ``commit``，把提交权交给调用方。
这不是风格选择而是正确性要求：分类反馈必须与「维度被改写」在同一个事务里，
否则维度改了、反馈没落（或反过来），学习闭环的数据就与事实不一致了。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ValidationError
from app.domain.wiki_learning_models import (
    LEARNING_ENTITY_TYPES,
    LEARNING_MECHANISMS,
    LEARNING_USER_ACTIONS,
    LearningFeedback,
)
from app.services.messages_zh import (
    MSG_WIKI_FEEDBACK_ACTION_INVALID,
    MSG_WIKI_FEEDBACK_ENTITY_TYPE_INVALID,
    MSG_WIKI_FEEDBACK_MECHANISM_INVALID,
    MSG_WIKI_FEEDBACK_MODIFICATION_REQUIRED,
    MSG_WIKI_FEEDBACK_MODIFICATION_UNEXPECTED,
)

if TYPE_CHECKING:  # 仅类型标注用，避免 service 间循环导入
    from app.domain.wiki_models import WikiPage

logger = logging.getLogger(__name__)

# 输入快照里正文的截断长度。存整篇正文既无必要（分类只看主旨）又让
# 反馈表随知识量线性膨胀；200 字符足够人判断「当时喂的是哪段内容」。
_SNAPSHOT_CONTENT_CHARS = 200

# 机制 1 对应的机制标识（与 LEARNING_MECHANISMS 对齐）
MECHANISM_CLASSIFY = "CLASSIFY"


def decideClassificationAction(
    autoClassification: dict[str, Any] | None, newDimension: str | None
) -> str | None:
    """把「用户给维度」这个动作判定成对系统建议的处置。

    三态是一组**全序划分**（任何一次处置恰好命中一个）：

    - ``None``：没有可反馈的建议（从未分类过 / 建议结构损坏）→ 不记事件
    - ``REJECT``：清空维度 = 我不要这个分类（优先于比对，先判「无建议」）
    - ``CONFIRM``：给的维度就是建议的 primary
    - ``MODIFY``：给了别的维度

    比对基准刻意选**建议的 primary** 而不是「改动前的维度」：反馈要回答的是
    「系统说得对不对」。若拿前一版维度做基准，用户连续两次改成同一个值时
    第二次会被记成 CONFIRM——那是在确认自己上一次的判断，不是确认系统。
    """
    if not isinstance(autoClassification, dict):
        return None
    primary = autoClassification.get("primary")
    if not isinstance(primary, str) or not primary:
        return None
    if newDimension is None:
        return "REJECT"
    return "CONFIRM" if newDimension == primary else "MODIFY"


class FeedbackLoop:
    """反馈事件写入器（机制 1~4 共用）。"""

    async def record(
        self,
        session: AsyncSession,
        *,
        mechanism: str,
        entityType: str,
        entityId: str,
        userAction: str,
        inputSnapshot: dict[str, Any] | None = None,
        systemOutput: dict[str, Any] | None = None,
        userModification: dict[str, Any] | None = None,
        userId: int | None = None,
    ) -> LearningFeedback:
        """落一条反馈事件（**不 commit**，由调用方提交）。

        ``MODIFY`` 与 ``userModification`` 互为充要条件：MODIFY 不带修改内容
        说不清改成了什么，CONFIRM/REJECT 带修改内容则说明调用方把动作判错了。
        两个方向都拦住，避免同一事实有两种记法。
        """
        if mechanism not in LEARNING_MECHANISMS:
            raise ValidationError(
                MSG_WIKI_FEEDBACK_MECHANISM_INVALID.format(mechanism=mechanism)
            )
        if entityType not in LEARNING_ENTITY_TYPES:
            raise ValidationError(
                MSG_WIKI_FEEDBACK_ENTITY_TYPE_INVALID.format(entityType=entityType)
            )
        if userAction not in LEARNING_USER_ACTIONS:
            raise ValidationError(
                MSG_WIKI_FEEDBACK_ACTION_INVALID.format(action=userAction)
            )
        if userAction == "MODIFY" and not userModification:
            raise ValidationError(MSG_WIKI_FEEDBACK_MODIFICATION_REQUIRED)
        if userAction != "MODIFY" and userModification:
            raise ValidationError(
                MSG_WIKI_FEEDBACK_MODIFICATION_UNEXPECTED.format(action=userAction)
            )

        row = LearningFeedback(
            mechanism=mechanism,
            entity_type=entityType,
            entity_id=entityId,
            input_snapshot=inputSnapshot,
            system_output=systemOutput,
            user_action=userAction,
            user_modification=userModification,
            feedback_user_id=userId,
        )
        session.add(row)
        # flush 而非 commit：拿到 id 与默认值（feedback_at），事务仍归调用方
        await session.flush()
        return row

    async def recordClassification(
        self,
        session: AsyncSession,
        *,
        page: WikiPage,
        newDimension: str | None,
        inputSnapshot: dict[str, Any],
        userId: int | None = None,
    ) -> str | None:
        """按 Page 已有的分类建议判定动作并落反馈；无可反馈的建议时返回 None。

        返回动作（而非 None/行的二值）是为了让调用方能把「这次记了什么」
        如实回给用户：UI 上「已记录修正」与「已设定维度」是两句不同的话。

        ``inputSnapshot`` 由调用方**显式**传入，且必须在改写 Page 任何字段之前
        用 :func:`snapshotPageInput` 取好。不在这里读 ``page.title`` 是有原因的：
        ``updatePage`` 会先 ``setattr`` 完所有字段再调本方法，届时标题/正文已是
        新值，而 ``system_output`` 里那条建议是基于**旧**正文算出来的——两者配对
        写进反馈表，就得到一组从来没同时出现过的「输入 → 输出」，
        日后拿它训练分类器等于喂脏数据。
        """
        action = decideClassificationAction(page.auto_classification, newDimension)
        if action is None:
            return None

        await self.record(
            session,
            mechanism=MECHANISM_CLASSIFY,
            entityType="WIKI_PAGE",
            entityId=page.page_id,
            userAction=action,
            inputSnapshot=inputSnapshot,
            # 原始建议原样存下（含 confidence/alternatives），不改写成用户的结论
            systemOutput=page.auto_classification,
            userModification={"dimension": newDimension} if action == "MODIFY" else None,
            userId=userId,
        )
        return action


def snapshotPageInput(page: WikiPage) -> dict[str, Any]:
    """取「建议生成时的输入快照」（标题 + 正文开头）。

    **必须在任何字段改写之前调用** —— 见 ``recordClassification`` 的说明。
    """
    return {
        "title": page.title,
        "contentHead": page.content[:_SNAPSHOT_CONTENT_CHARS],
    }


__all__ = [
    "FeedbackLoop",
    "decideClassificationAction",
    "snapshotPageInput",
    "MECHANISM_CLASSIFY",
]
