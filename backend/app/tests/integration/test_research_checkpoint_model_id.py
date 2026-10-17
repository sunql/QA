"""spec N4 - Research checkpoint 接受 model_id 字段，续跑用新模型。"""
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DataSource
from app.domain.research_models import ResearchCheckpoint, ResearchSession, ResearchTask, ResearchTurn
from app.domain.research_schemas import CheckpointAnswerRequest
from app.services.research_session_service import ResearchSessionService
from app.api.v1 import research as researchModule

_BASE = "/api/v1/research"


async def _seedUser(dbSession: AsyncSession, username: str) -> None:
    from app.models.rbac import User
    user = User(username=username, display_name=username, email=None, enabled=True, password_hash=None)
    dbSession.add(user)
    await dbSession.commit()


async def _seedDatasource(dbSession: AsyncSession) -> None:
    from app.domain.models import DataSource
    from app.domain.enums import DataSourceType
    from app.infrastructure.security.crypto import encryptApiKey
    ds = DataSource(
        name="api-test-oracle",
        type=DataSourceType.ORACLE,
        host="oracle-test",
        port=1521,
        database_name="THBIDB",
        username="THBI",
        password_encrypted=encryptApiKey("test-password"),
        is_active=True,
        is_default=True,
        oracle_version="19.0.0.0.0",
    )
    dbSession.add(ds)
    await dbSession.commit()


async def _authHeaders(dbSession: AsyncSession) -> dict[str, str]:
    await _seedUser(dbSession, "research-user-a")
    await _seedDatasource(dbSession)
    return {"X-User-Id": "research-user-a"}


async def _createSession(
    dbSession: AsyncSession, userId: int, datasourceId: int
):
    svc = ResearchSessionService()
    return await svc.createSession(
        dbSession, userId=userId, question="q", datasourceId=datasourceId
    )


async def _seedRunState(
    dbSession: AsyncSession,
    *,
    sessionId,
    turnId,
    datasourceId: int | None = None,
):
    from app.services.research_run_state import RunStateSeed, buildRunState
    run_state = buildRunState(
        RunStateSeed(
            session_id=sessionId,
            turn_id=turnId,
            current_question="q",
            mode="research",
            user_id=None,
            model_id=None,
            datasource_id=datasourceId,
        )
    )
    dbSession.add(run_state)
    await dbSession.flush()
    return run_state


async def _claimSession(dbSession: AsyncSession, *, sessionId, runStateId) -> None:
    from sqlalchemy import update
    from app.domain.research_models import ResearchSession
    await dbSession.execute(
        update(ResearchSession)
        .where(ResearchSession.id == sessionId)
        .values(active_run_id=runStateId)
    )
    await dbSession.flush()
    rowInMap = await dbSession.get(ResearchSession, sessionId)
    if rowInMap is not None:
        dbSession.expire(rowInMap, ["active_run_id"])


class TestCheckpointAnswerRequestModelId:
    def test_accepts_model_id_camelcase(self) -> None:
        """payload 含 modelId 字段应被接受（Field alias 生效）。"""
        req = CheckpointAnswerRequest.model_validate({
            "action": "confirm",
            "choice": {},
            "modelId": 2,
        })
        assert req.model_id == 2

    def test_model_id_optional(self) -> None:
        """不传 modelId 也合法（保持向后兼容）。"""
        req = CheckpointAnswerRequest.model_validate({
            "action": "confirm",
            "choice": {},
        })
        assert req.model_id is None

    def test_model_id_must_be_positive_int(self) -> None:
        """modelId <= 0 应被 Pydantic 拒。"""
        with pytest.raises(Exception):
            CheckpointAnswerRequest.model_validate({
                "action": "confirm",
                "choice": {},
                "modelId": 0,
            })

    def test_extra_fields_forbidden(self) -> None:
        """unknown 字段被 extra=forbid 拒。

        注：snake_case ``model_id`` 因 CamelModel ``populate_by_name=True`` 被接受
        （Python 字段名直接走 populate_by_name）。故改用真正未声明字段 ``bogusField``
        验证 ``extra=forbid`` 行为。
        """
        with pytest.raises(Exception):
            CheckpointAnswerRequest.model_validate({
                "action": "confirm",
                "choice": {},
                "bogusField": 1,
            })


class TestAnswerCheckpointPassesModelId:
    """API 层：answerCheckpoint 把 modelId 透传到 task payload。"""

    @pytest.mark.asyncio
    async def test_answer_checkpoint_passes_model_id_to_task_payload(
        self, client: AsyncClient, dbSession: AsyncSession, monkeypatch
    ) -> None:
        """POST /research/checkpoints/{id}/answer 传 modelId=2
        → task.payload 含 modelId=2"""
        import uuid
        from unittest.mock import AsyncMock
        from app.models.rbac import User

        # Minimal fake service for background task
        fake_service = AsyncMock()
        fake_service.resumeTurn = AsyncMock(return_value=None)
        fake_service.markFailed = AsyncMock(return_value=None)
        monkeypatch.setattr(researchModule, "buildResearchAgentService", lambda: fake_service)

        # Setup: create user, session, turn, run_state, checkpoint
        user = User(username="t8-user", display_name="t8", email=None, enabled=True, password_hash=None)
        dbSession.add(user)
        await dbSession.flush()

        datasource = (await dbSession.execute(
            select(DataSource).where(DataSource.is_default == True).limit(1)
        )).scalar_one_or_none()
        if datasource is None:
            from app.domain.enums import DataSourceType
            from app.infrastructure.security.crypto import encryptApiKey
            datasource = DataSource(
                name="api-test-oracle",
                type=DataSourceType.ORACLE,
                host="oracle-test",
                port=1521,
                database_name="THBIDB",
                username="THBI",
                password_encrypted=encryptApiKey("test-password"),
                is_active=True,
                is_default=True,
                oracle_version="19.0.0.0.0",
            )
            dbSession.add(datasource)
            await dbSession.flush()

        row = await _createSession(dbSession, userId=user.id, datasourceId=datasource.id)
        turn = await ResearchSessionService().appendTurn(dbSession, sessionId=row.id, role="user", content={})
        runState = await _seedRunState(dbSession, sessionId=row.id, turnId=turn.id, datasourceId=datasource.id)
        await _claimSession(dbSession, sessionId=row.id, runStateId=runState.id)
        cp = await ResearchSessionService().openCheckpoint(
            dbSession,
            sessionId=row.id,
            turnId=turn.id,
            phase="intent",
            options={"signal": "fixed_scope", "resumePhase": "plan"},
            prompt="test?",
        )
        await dbSession.commit()

        # Sanity: checkpoint exists
        assert cp.id is not None

        resp = await client.post(
            f"{_BASE}/checkpoints/{cp.id}/answer",
            json={"action": "confirm", "choice": {}, "modelId": 2},
            headers={"X-User-Id": "t8-user"},
        )
        assert resp.status_code == 200, resp.text

        # Verify task.payload has modelId
        from app.services.research_task_service import TASK_KIND_RESUME
        task = (await dbSession.execute(
            select(ResearchTask).where(
                ResearchTask.session_id == row.id,
                ResearchTask.turn_id == turn.id,
                ResearchTask.kind == TASK_KIND_RESUME,
            )
        )).scalar_one()
        assert task.payload.get("modelId") == 2