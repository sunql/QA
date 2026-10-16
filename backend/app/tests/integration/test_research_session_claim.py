"""P0-X5: ``app.services.session_claim`` 共享守卫单测。

覆盖三件事：

1. **归属守卫**：存在 + 归属 / 不存在 / 归属不符 / admin 越权 四态。
2. **claim 互斥断言 ``"free"``**：用于 ``createTurn`` —— 无活跃轮次放行、有则 409。
3. **claim 互斥断言 ``"held"``**：用于 ``answerCheckpoint`` —— 有活跃轮次放行、无则 409。
   关键 X5 防御：abort 之后 ``active_run_id`` 清空但 pending checkpoint 仍 ``status="pending"``，
   守这个断言就把「abort 后再答 checkpoint 把会话拉回 running」这条路封死。

不变量：「不持有 claim ⇒ 不能续跑」是修复 ``[[qa-system-research-claim-invariant-create-only]]``
的关键：互斥判据（``active_run_id``）与「能不能再跑一轮」的判据（会话 status）本
是两套东西，X5 之前只有 createTurn 把它们绑在一起。
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError
from app.models.rbac import User
from app.services.research_session_service import ResearchSessionService


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def _seedUser(dbSession: AsyncSession, username: str) -> User:
    user = User(
        username=username,
        display_name=username,
        email=None,
        enabled=True,
        password_hash=None,
    )
    dbSession.add(user)
    await dbSession.commit()
    await dbSession.refresh(user)
    return user


async def _currentUser(dbSession: AsyncSession, username: str, **extra: Any) -> CurrentUser:
    """把 DB ``User`` 翻成 ``CurrentUser``（与 stub 头走同路径，避免手工连字段）。"""
    userId = await dbSession.scalar(select(User.id).where(User.username == username))
    assert userId is not None, f"test setup: user {username!r} not seeded"
    return CurrentUser(
        userId=username,
        dbUserId=userId,
        roles=extra.get("roles", ()),
        departments=extra.get("departments", ()),
    )


@pytest.fixture
async def researchUser(dbSession: AsyncSession) -> CurrentUser:
    await _seedUser(dbSession, "x5-research-user")
    return await _currentUser(dbSession, "x5-research-user")


@pytest.fixture
async def otherResearchUser(dbSession: AsyncSession) -> CurrentUser:
    await _seedUser(dbSession, "x5-other-user")
    return await _currentUser(dbSession, "x5-other-user")


@pytest.fixture
async def adminUser(dbSession: AsyncSession) -> CurrentUser:
    await _seedUser(dbSession, "x5-admin-user")
    return await _currentUser(dbSession, "x5-admin-user", roles=("admin",))


# ---------------------------------------------------------------------------
# 目标模块（延迟 import 以让 pytest fixture 先建好 dbSession）
# ---------------------------------------------------------------------------

from app.services.session_claim import (  # noqa: E402
    assert_research_claim_invariant,
    assert_research_session_ownership,
)


# ---------------------------------------------------------------------------
# 归属守卫
# ---------------------------------------------------------------------------


async def test_ownership_returns_row_when_owner(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """归属符合 ⇒ 返回行，不抛错。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    await dbSession.commit()

    out = await assert_research_session_ownership(dbSession, row.id, researchUser)
    assert out.id == row.id
    assert out.created_by == researchUser.dbUserId


async def test_ownership_404_when_session_missing(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """不存在 ⇒ 404（不泄露存在性）。"""
    with pytest.raises(NotFoundError) as excInfo:
        await assert_research_session_ownership(
            dbSession, uuid.uuid4(), researchUser
        )
    assert "研究会话不存在" in str(excInfo.value)


async def test_ownership_404_when_other_user(
    dbSession: AsyncSession,
    researchUser: CurrentUser,
    otherResearchUser: CurrentUser,
) -> None:
    """非本人 ⇒ 404（与既有 ``_ownedSession`` 同口径，不泄露存在性）。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    await dbSession.commit()

    with pytest.raises(NotFoundError):
        await assert_research_session_ownership(
            dbSession, row.id, otherResearchUser
        )


async def test_ownership_404_when_admin_impostor(
    dbSession: AsyncSession,
    researchUser: CurrentUser,
    adminUser: CurrentUser,
) -> None:
    """P0-X5：归属守卫**不**放行 admin —— admin 越权 404 仍生效。

    与 ``session_guard.assertSessionOwnership``（chat 域）不同：research
    写路径要求硬归属，避免 admin 误写他人会话。admin 自己的 ``dbUserId``
    行走正常分支。
    """
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    await dbSession.commit()

    with pytest.raises(NotFoundError):
        await assert_research_session_ownership(
            dbSession, row.id, adminUser
        )


# ---------------------------------------------------------------------------
# claim 互斥断言 —— "free"（用于 createTurn）
# ---------------------------------------------------------------------------


async def test_claim_invariant_free_passes_when_no_active_run(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """``expected="free"``：``active_run_id is None`` 放行。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    await dbSession.commit()

    out = await assert_research_claim_invariant(
        dbSession, row.id, expected="free"
    )
    assert out.id == row.id
    assert out.active_run_id is None


async def test_claim_invariant_free_409_when_active_run(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """``expected="free"``：``active_run_id is not None`` ⇒ 409。"""
    from app.services.research_run_state import RunStateSeed, buildRunState

    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    # FK 约束：``active_run_id`` 指向 ``research_run_state.id``，必须先有 run state 行
    runState = buildRunState(
        RunStateSeed(
            session_id=row.id,
            turn_id=turn.id,
            current_question="q",
            mode="research",
            user_id=researchUser.dbUserId,
            model_id=None,
            datasource_id=None,
        )
    )
    dbSession.add(runState)
    await dbSession.flush()
    row.active_run_id = runState.id
    await dbSession.commit()

    with pytest.raises(ConflictError) as excInfo:
        await assert_research_claim_invariant(
            dbSession, row.id, expected="free"
        )
    assert "进行中" in str(excInfo.value)


# ---------------------------------------------------------------------------
# claim 互斥断言 —— "held"（用于 answerCheckpoint）
# ---------------------------------------------------------------------------


async def test_claim_invariant_held_passes_when_active_run(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """``expected="held"``：``active_run_id is not None`` 放行。"""
    from app.services.research_run_state import RunStateSeed, buildRunState

    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    turn = await svc.appendTurn(dbSession, sessionId=row.id, role="user", content={})
    runState = buildRunState(
        RunStateSeed(
            session_id=row.id,
            turn_id=turn.id,
            current_question="q",
            mode="research",
            user_id=researchUser.dbUserId,
            model_id=None,
            datasource_id=None,
        )
    )
    dbSession.add(runState)
    await dbSession.flush()
    row.active_run_id = runState.id
    await dbSession.commit()

    out = await assert_research_claim_invariant(
        dbSession, row.id, expected="held"
    )
    assert out.id == row.id
    assert out.active_run_id == runState.id


async def test_claim_invariant_held_409_when_no_active_run(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """P0-X5 核心防御：``expected="held"``：``active_run_id is None`` ⇒ 409。

    对应 abort 之后 / 终态之后 / 从未 claim 三种场景 —— 任何一种都不该
    被续跑（resumeTurn 不 claim，会造出 ``running + active_run_id=NULL``
    的会话，下一次 POST /turns 放行，起第二条状态机）。
    """
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=researchUser.dbUserId, question="q")
    await dbSession.commit()

    with pytest.raises(ConflictError) as excInfo:
        await assert_research_claim_invariant(
            dbSession, row.id, expected="held"
        )
    assert "未持有执行权" in str(excInfo.value)
    assert "active_run_id 为空" in str(excInfo.value)


async def test_claim_invariant_404_when_session_missing(
    dbSession: AsyncSession, researchUser: CurrentUser
) -> None:
    """claim 互斥断言：会话不存在 ⇒ 404（与归属守卫同口径）。"""
    with pytest.raises(NotFoundError):
        await assert_research_claim_invariant(
            dbSession, uuid.uuid4(), expected="free"
        )
    with pytest.raises(NotFoundError):
        await assert_research_claim_invariant(
            dbSession, uuid.uuid4(), expected="held"
        )
