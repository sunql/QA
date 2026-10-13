"""SessionQueryState 绑定 (user_id, datasource_id, session_id) 三元集成测试。

RED: 现有 _loadQueryState / _saveQueryState 不接 user/datasource，
     load 无 scope 过滤 → 跨用户/跨数据源窜 state。
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource, SessionQueryState
from app.domain.enums import DataSourceType

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_state(dbSession: AsyncSession) -> dict:
    """播种两个不同用户 × 两个不同数据源的身份数据。"""
    ds_a = DataSource(
        id=90001, name="DS-A", type=DataSourceType.ORACLE,
        host="h", port=1521, database_name="svc",
        username="u", password_encrypted="cipher",
    )
    ds_b = DataSource(
        id=90002, name="DS-B", type=DataSourceType.ORACLE,
        host="h", port=1521, database_name="svc",
        username="u", password_encrypted="cipher",
    )
    dbSession.add(ds_a)
    dbSession.add(ds_b)
    await dbSession.flush()

    user_a_id = 91001
    user_b_id = 91002

    await dbSession.commit()
    return {
        "datasource_a_id": ds_a.id,
        "datasource_b_id": ds_b.id,
        "user_a_id": user_a_id,
        "user_b_id": user_b_id,
    }


async def test_load_query_state_filters_by_user_and_datasource(
    dbSession: AsyncSession, seeded_state: dict,
):
    """用户 B 拿 A 的 sessionId + 不同 datasourceId → _loadQueryState 返 None。"""
    from app.services.chat_context import ContextMixin

    class _Svc(ContextMixin):
        pass

    service = _Svc()

    # A 在 datasource_a 上有 state
    state = SessionQueryState(
        session_id="shared-sid",
        datasource_id=seeded_state["datasource_a_id"],
        user_id=seeded_state["user_a_id"],
        last_question="A 的问题",
        turn_count=1,
    )
    dbSession.add(state)
    await dbSession.commit()

    # B 在 datasource_b 上调 load（同 sessionId）→ 应返 None
    result = await service._loadQueryState(
        dbSession,
        "shared-sid",
        userId=seeded_state["user_b_id"],
        datasourceId=seeded_state["datasource_b_id"],
    )
    assert result is None  # 不应返回 A 的 state


async def test_load_query_state_returns_when_scope_matches(
    dbSession: AsyncSession, seeded_state: dict,
):
    """同 user + 同 datasource → 正常返回 state。"""
    from app.services.chat_context import ContextMixin

    class _Svc(ContextMixin):
        pass

    service = _Svc()

    state = SessionQueryState(
        session_id="own-sid",
        datasource_id=seeded_state["datasource_a_id"],
        user_id=seeded_state["user_a_id"],
        last_question="A 的问题",
        turn_count=1,
    )
    dbSession.add(state)
    await dbSession.commit()

    result = await service._loadQueryState(
        dbSession,
        "own-sid",
        userId=seeded_state["user_a_id"],
        datasourceId=seeded_state["datasource_a_id"],
    )
    assert result is not None
    assert result.last_question == "A 的问题"


async def test_save_query_state_persists_user_and_datasource(
    dbSession: AsyncSession, seeded_state: dict,
):
    """_saveQueryState 写入带 userId/datasourceId；后续 load 用同 scope 能读回。"""
    from app.services.chat_context import ContextMixin
    from app.domain.query_plan import QueryPlan

    class _Svc(ContextMixin):
        pass

    service = _Svc()

    await service._saveQueryState(
        dbSession,
        "new-sid",
        question="test",
        plan=None,
        sql=None,
        resultColumns=[],
        userId=seeded_state["user_a_id"],
        datasourceId=seeded_state["datasource_a_id"],
        schemaName="public",
    )

    loaded = await service._loadQueryState(
        dbSession,
        "new-sid",
        userId=seeded_state["user_a_id"],
        datasourceId=seeded_state["datasource_a_id"],
    )
    assert loaded is not None
    assert loaded.user_id == seeded_state["user_a_id"]
    assert loaded.datasource_id == seeded_state["datasource_a_id"]
