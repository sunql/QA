"""研究会话归属 + 状态互斥（claim invariant）共享守卫。

P0-X5（1012 cross-refs spec §P0-X5）：研究会话写路径的「归属 + 互斥」两件
事原本散落在 ``api/v1/research.py`` 的若干端点里，靠人工校对维持一致性。

本模块把它们归到一处：

- :func:`assert_research_session_ownership` —— 归属校验，**取代** ``_ownedSession``。
- :func:`assert_research_claim_invariant` —— 互斥不变量「``active_run_id`` 与
  调用方意图匹配」的单点事实。

设计要点：

1. **不变量是「调用方意图」与「库中真值」的契约** —— 调用方声明期望
   （``expected="free"`` / ``expected="held"``），函数读库断言；不达 ⇒ 409。
2. **归属守卫不绕过 admin**（与 ``session_guard.assertSessionOwnership``
   不同；research 端点要求硬归属，避免 admin 误写他人会话）—— admin
   自己的 ``dbUserId`` 行走正常分支，越权 404 仍生效。
3. **claim 不变量通过所有写路径强制**：
   - ``createTurn`` 期望 ``"free"``（无活跃轮次才放行）
   - ``answerCheckpoint`` 期望 ``"held"``（必须存在活跃轮次才允许续跑）
   - ``abortSession`` 不直接走 claim（自带的 ``abortSessionIfActive``
     已用状态白名单做释放，不需要 claim 校验）

为什么 ``answerCheckpoint`` 必须断言 ``"held"``：在 abort 之后，会话落
``aborted`` 终态 + ``active_run_id`` 清空（见 ``abortSessionIfActive`` 的
"释放执行权" 分支），但 pending checkpoint 仍 ``status="pending"``。如果不
断言 ``"held"``，abort 之后再答 checkpoint 会把 ``resumeTurn`` 拉回
``running`` —— ``resumeTurn`` **不 claim**，就出现 ``status=running`` +
``active_run_id=NULL`` 的会话，下一次 ``POST /turns`` 直接放行、起第二条
状态机。详见记忆 ``[[qa-system-research-claim-invariant-create-only]]``。

不要在 ``resumeTurn`` 内部加 claim（要重做幂等键那一整套）—— 关掉入口
更稳：本守卫 + service 层 ``releaseActiveRun`` 闸门双保险。
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError
from app.domain.research_models import ResearchSession

ClaimExpectation = Literal["free", "held"]

# 与既有 ``_ownedSession`` 的 detail 文案逐字节一致：search 用
# "研究会话不存在" 做断言，改了要同步改测试。
_MSG_RESEARCH_SESSION_NOT_FOUND = "研究会话不存在"


async def assert_research_session_ownership(
    db: AsyncSession,
    sessionId: uuid.UUID,
    user: CurrentUser,
) -> ResearchSession:
    """按 id 取研究会话并校验归属；不存在 / 非本人一律 404（不泄露存在性）。

    从 ``api/v1/research.py`` 的私有 ``_ownedSession`` 提升而来 —— 共享给
    所有 research 写路径（createTurn / answerCheckpoint / abortSession /
    deleteSession / getSession / listReports）。

    **admin 也不放行**（与 ``session_guard.assertSessionOwnership`` 不同）：
    research 端点要求硬归属，避免 admin 误写他人会话。admin 自己的
    ``dbUserId`` 行走正常分支，越权 404 仍生效。
    """
    row = await db.get(ResearchSession, sessionId)
    if row is None or row.created_by != user.dbUserId:
        raise NotFoundError(_MSG_RESEARCH_SESSION_NOT_FOUND)
    return row


async def assert_research_claim_invariant(
    db: AsyncSession,
    sessionId: uuid.UUID,
    *,
    expected: ClaimExpectation,
) -> ResearchSession:
    """断言 ``active_run_id`` 与 ``expected`` 一致；不达 ⇒ 409。

    :param expected:

      - ``"free"``：用于 ``createTurn`` —— **无** 活跃轮次才放行。
        反向断言：``active_run_id is not None`` ⇒ 409（同会话已有进行中
        的轮次 / 暂停等决策的轮次）。
      - ``"held"``：用于 ``answerCheckpoint`` —— **有** 活跃轮次才放行。
        反向断言：``active_run_id is None`` ⇒ 409（会话已终态 / 已
        abort / 从未 claim；任何一种都不该被续跑，否则会绕过互斥）。

    不存在 ⇒ 404（与归属守卫同口径，避免泄露存在性）。
    """
    row = await db.get(ResearchSession, sessionId)
    if row is None:
        raise NotFoundError(_MSG_RESEARCH_SESSION_NOT_FOUND)
    isHeld = row.active_run_id is not None
    if expected == "free" and isHeld:
        # 中文文案与既有 createTurn 409 保持一致
        raise ConflictError(
            f"研究会话已有进行中的轮次（active_run_id={row.active_run_id}），"
            f"请等待完成或取消后再提交"
        )
    if expected == "held" and not isHeld:
        raise ConflictError(
            "研究会话未持有执行权（active_run_id 为空），"
            "无法续跑：会话已终态 / 已取消 / 从未启动"
        )
    return row
