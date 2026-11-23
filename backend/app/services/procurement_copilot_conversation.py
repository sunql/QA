"""I3 多轮 Copilot 的会话服务：会话壳、轮次、上下文注入。

三条规则（都是「不做会出真问题」的）：

1. **追问 = 续跑，不是新开**：上一轮 run 停在 `PAUSED/NEEDS_INPUT` 时，用户下一句话
   是对追问的回答 ⇒ 走既有 `resumeRun`（同一个 run 继续 PLAN），而不是新建 run
   把已经做过的检索再花一遍钱。轮次表允许两条轮次指向同一 `run_id`。
2. **上下文有界**：新问题带最近 N 轮（默认 3）且总字符 ≤ `MAX_CONTEXT_CHARS`
   （默认 1500）的摘要；超长从**最旧**开始丢。没有上限的上下文会撑爆 planner 的
   observation 预算。
3. **会话绑定 Agent + 模型**：中途换场景/换模型 ⇒ 422 要求新开会话；
   否则上下文会把上一个场景的结论喂给另一个 Agent。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.procurement_copilot_models import (
    MAX_TURNS_PER_CONVERSATION,
    ProcurementCopilotConversation,
    ProcurementCopilotTurn,
)
from app.services.messages_zh import (
    MSG_COPILOT_CONVERSATION_AGENT_MISMATCH,
    MSG_COPILOT_CONVERSATION_NOT_FOUND,
    MSG_COPILOT_CONVERSATION_TOO_MANY_TURNS,
)

#: 注入上下文的上限（轮数 / 字符数 / 单条摘要长度）。
MAX_CONTEXT_TURNS = 3
MAX_CONTEXT_CHARS = 1500
MAX_SUMMARY_CHARS = 300

CONTEXT_HEADER = "[对话上下文]"
CURRENT_QUESTION_HEADER = "[当前问题]"


@dataclass(frozen=True)
class TurnHistory:
    """构建上下文所需的一轮（只读视图）。"""

    question: str
    answer: str | None
    status: str


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def buildContextBlock(
    history: Sequence[TurnHistory],
    *,
    maxTurns: int = MAX_CONTEXT_TURNS,
    maxChars: int = MAX_CONTEXT_CHARS,
) -> str:
    """最近 N 轮 → 上下文块（无历史 ⇒ 空串）。

    从**最新**往前取，拼到超长就停 ⇒ 保新弃旧（旧的更可能已被后续结论覆盖）。
    """
    if not history or maxTurns <= 0 or maxChars <= 0:
        return ""
    lines: list[str] = []
    used = 0
    for index, turn in enumerate(reversed(list(history)[-maxTurns:]), start=1):
        answer = turn.answer or ("（未完成）" if turn.status else "（无回答）")
        block = (
            f"{index}. 用户：{_clip(turn.question, MAX_SUMMARY_CHARS)}\n"
            f"   助手：{_clip(answer, MAX_SUMMARY_CHARS)}"
        )
        if used + len(block) > maxChars:
            break
        lines.append(block)
        used += len(block) + 1
    if not lines:
        return ""
    # 按时间正序展示（上面是倒序取的）
    ordered = list(reversed(lines))
    return f"{CONTEXT_HEADER}\n" + "\n".join(ordered)


def buildRunInput(message: str, history: Sequence[TurnHistory]) -> str:
    """新问题 +（有历史时）上下文块。无历史 ⇒ 原样返回（run.question 保持干净）。"""
    context = buildContextBlock(history)
    if not context:
        return message
    return f"{context}\n{CURRENT_QUESTION_HEADER}\n{message}"


async def getOwnedConversation(
    session: AsyncSession,
    conversationId: UUID,
    actor: object,
) -> ProcurementCopilotConversation:
    """按 id 取会话；非创建者**一律 404**（不泄露会话是否存在）。"""
    row = (
        await session.execute(
            select(ProcurementCopilotConversation).where(
                ProcurementCopilotConversation.conversation_id == conversationId
            )
        )
    ).scalars().first()
    if row is None or row.created_by != getattr(actor, "userId", None):
        raise NotFoundError(MSG_COPILOT_CONVERSATION_NOT_FOUND)
    return row


async def loadOrCreateConversation(
    session: AsyncSession,
    *,
    conversationId: UUID | None,
    actor: object,
    agentCode: str,
    modelConfigId: int,
) -> ProcurementCopilotConversation:
    """取会话或新建；已存在时校验归属与 Agent 绑定。"""
    if conversationId is None:
        conversation = ProcurementCopilotConversation(
            conversation_id=uuid4(),
            created_by=str(getattr(actor, "userId", "")),
            agent_code=agentCode,
            model_config_id=modelConfigId,
        )
        session.add(conversation)
        await session.flush()
        return conversation

    conversation = await getOwnedConversation(session, conversationId, actor)
    if conversation.agent_code != agentCode:
        raise ValidationError(
            MSG_COPILOT_CONVERSATION_AGENT_MISMATCH.format(
                current=conversation.agent_code, requested=agentCode
            )
        )
    return conversation


async def listTurns(
    session: AsyncSession, conversationId: UUID
) -> list[ProcurementCopilotTurn]:
    rows = (
        await session.execute(
            select(ProcurementCopilotTurn)
            .where(ProcurementCopilotTurn.conversation_id == conversationId)
            .order_by(ProcurementCopilotTurn.seq)
        )
    ).scalars().all()
    return list(rows)


async def appendTurn(
    session: AsyncSession,
    conversation: ProcurementCopilotConversation,
    *,
    question: str,
    runId: str,
) -> ProcurementCopilotTurn:
    """追加一轮（seq 由已有轮数推导；上限后拒绝，要求新开会话）。"""
    turns = await listTurns(session, conversation.conversation_id)
    if len(turns) >= MAX_TURNS_PER_CONVERSATION:
        raise ValidationError(
            MSG_COPILOT_CONVERSATION_TOO_MANY_TURNS.format(
                limit=MAX_TURNS_PER_CONVERSATION
            )
        )
    turn = ProcurementCopilotTurn(
        conversation_id=conversation.conversation_id,
        seq=len(turns) + 1,
        question=question,
        run_id=runId,
        created_time=datetime.now(UTC),
    )
    session.add(turn)
    await session.flush()
    return turn


def historyFromTurns(
    turns: Sequence[ProcurementCopilotTurn], runViews: dict[str, tuple[str | None, str]]
) -> list[TurnHistory]:
    """轮次 + run 摘要 → 上下文历史（`runViews`: runId -> (answer, status)）。"""
    history: list[TurnHistory] = []
    for turn in turns:
        answer, status = runViews.get(turn.run_id, (None, ""))
        history.append(TurnHistory(question=turn.question, answer=answer, status=status))
    return history


def lastRunId(turns: Sequence[ProcurementCopilotTurn]) -> str | None:
    return turns[-1].run_id if turns else None


@dataclass(frozen=True)
class ConversationSummaryRow:
    """会话列表项（service 层只读视图，DTO 转换在 API 层）。"""

    conversation: ProcurementCopilotConversation
    turnCount: int
    lastQuestion: str | None


async def listOwnedConversations(
    session: AsyncSession,
    actor: object,
    *,
    limit: int = 20,
) -> list[ConversationSummaryRow]:
    """列出**当前用户自己**的会话（最近更新在前）。

    与 `getOwnedConversation` 同口径：只看自己的，别人的既不可见也不计数
    （admin 也只看自己的 —— 历史会话属于个人会话空间）。
    """
    userId = str(getattr(actor, "userId", ""))
    conversations = (
        await session.execute(
            select(ProcurementCopilotConversation)
            .where(ProcurementCopilotConversation.created_by == userId)
            .order_by(
                ProcurementCopilotConversation.updated_time.desc(),
                ProcurementCopilotConversation.id.desc(),
            )
            .limit(limit)
        )
    ).scalars().all()
    if not conversations:
        return []

    ids = [c.conversation_id for c in conversations]
    counts = dict(
        (
            await session.execute(
                select(
                    ProcurementCopilotTurn.conversation_id,
                    func.count(ProcurementCopilotTurn.id),
                )
                .where(ProcurementCopilotTurn.conversation_id.in_(ids))
                .group_by(ProcurementCopilotTurn.conversation_id)
            )
        ).all()
    )
    # PG DISTINCT ON：每个会话取 seq 最大的一条（最后一问）
    lastRows = (
        await session.execute(
            select(
                ProcurementCopilotTurn.conversation_id,
                ProcurementCopilotTurn.question,
            )
            .where(ProcurementCopilotTurn.conversation_id.in_(ids))
            .order_by(
                ProcurementCopilotTurn.conversation_id,
                ProcurementCopilotTurn.seq.desc(),
            )
            .distinct(ProcurementCopilotTurn.conversation_id)
        )
    ).all()
    lastQuestions = {row[0]: row[1] for row in lastRows}
    return [
        ConversationSummaryRow(
            conversation=conversation,
            turnCount=int(counts.get(conversation.conversation_id, 0)),
            lastQuestion=lastQuestions.get(conversation.conversation_id),
        )
        for conversation in conversations
    ]


async def deleteOwnedConversation(
    session: AsyncSession,
    conversationId: UUID,
    actor: object,
) -> None:
    """删除**自己的**会话及其轮次（别人的会话一律 404，与详情同口径）。

    `procurement_copilot_turn` 没有库级 FK（见 0162），故先显式删轮次再删会话。
    **不删 run**：编排运行是独立审计对象，删会话只断开「线程」这层组织。
    """
    conversation = await getOwnedConversation(session, conversationId, actor)
    await session.execute(
        delete(ProcurementCopilotTurn).where(
            ProcurementCopilotTurn.conversation_id == conversationId
        )
    )
    await session.delete(conversation)
    await session.commit()
