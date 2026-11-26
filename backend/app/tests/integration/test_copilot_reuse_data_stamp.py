"""P4 收尾集成测试：同日复用键含数据新鲜度戳（0171，真实 PostgreSQL）。

场景分支的复用 = 同用户/Agent/模型/问题文本/**数据戳**。feature_value
的 computed_at 变化（指标重算）⇒ 相同问题文本不再命中旧 run。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.agent_orchestration_models import AgentOrchestrationRun
from app.domain.copilot_scenario_models import CopilotScenario
from app.domain.models import FeatureDefinition, FeatureValue
from app.domain.procurement_copilot_models import (
    ProcurementCopilotConversation,
    ProcurementCopilotTurn,
)
from app.domain.procurement_copilot_schemas import CopilotMessageRequest
from app.services import procurement_copilot as copilot

RENDERED_QUESTION = (
    "分析供应商风险情况（供应商编码=A013）。请调用场景绑定工具完成取数与分析；"
    "supplierKey=A013。若上方括号内的参数与用户问题描述冲突，以用户问题描述为准。"
)
AGENT = "SUPPLIER_RISK_AGENT"

_SCENARIO_ROW = CopilotScenario(
    scenario_code="supplier_risk",
    name="供应商风险速览",
    description="d",
    icon_code="search",
    agent_code=AGENT,
    slot_schema=[
        {
            "key": "supplierKey",
            "type": "text",
            "label": "供应商编码",
            "conflictKeywords": ["供应商", "厂家"],
        }
    ],
    question_template="分析供应商风险情况",
    chips=[],
    result_renderer="none",
    required_role=None,
    sort_order=20,
    enabled=True,
)


class _CapturingRunner:
    def __init__(self) -> None:
        self.created: list[str] = []

    async def createRun(self, _session: Any, runCreate: Any, _actor: Any) -> Any:
        runId = f"run-{len(self.created) + 1}"
        self.created.append(runId)
        return SimpleNamespace(run_id=runId, status="QUEUED")

    async def getRun(self, _session: Any, runId: str, _actor: Any) -> Any:
        return SimpleNamespace(status="SUCCEEDED")


def _installFakes(monkeypatch: pytest.MonkeyPatch, runner: _CapturingRunner) -> None:
    async def _fakeLoad(_session: Any, **_kw: Any) -> Any:
        return ProcurementCopilotConversation(
            conversation_id=uuid4(),
            created_by="u1",
            agent_code=AGENT,
            model_config_id=1,
        )

    async def _fakeTurns(_session: Any, _cid: Any) -> list[Any]:
        return []

    async def _fakeAppend(_session: Any, _conv: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(copilot.conversationStore, "loadOrCreateConversation", _fakeLoad)
    monkeypatch.setattr(copilot.conversationStore, "listTurns", _fakeTurns)
    monkeypatch.setattr(copilot.conversationStore, "appendTurn", _fakeAppend)

    class _FakeScenarioService:
        def __init__(self, _session: Any) -> None:
            return None

        async def getByCode(self, _code: str) -> Any:
            return _SCENARIO_ROW

    monkeypatch.setattr(
        "app.services.copilot_scenario_service.CopilotScenarioService",
        _FakeScenarioService,
    )


async def _seedRunAndTurn(session: AsyncSession, *, runId: str, dataStamp: str | None) -> None:
    run = AgentOrchestrationRun(
        run_id=runId,
        profile_revision=1,
        instructions_snapshot="",
        tool_bindings_snapshot=[],
        max_observation_chars=50000,
        max_iterations=20,
        max_tool_calls=30,
        max_cost_usd=5,
        timeout_seconds=900,
        agent_code=AGENT,
        model_config_id=1,
        question=RENDERED_QUESTION,
        created_by="u1",
        status="SUCCEEDED",
        created_time=datetime.now(UTC),
        queued_at=datetime.now(UTC),
    )
    session.add(run)
    conversation = ProcurementCopilotConversation(
        conversation_id=uuid4(),
        created_by="u1",
        agent_code=AGENT,
        model_config_id=1,
    )
    session.add(conversation)
    await session.flush()
    turn = ProcurementCopilotTurn(
        conversation_id=conversation.conversation_id,
        seq=1,
        question=RENDERED_QUESTION,
        run_id=runId,
        data_stamp=dataStamp,
        created_time=datetime.now(UTC),
    )
    session.add(turn)
    await session.commit()


def _dto() -> Any:
    return CopilotMessageRequest(
        message="",
        agent_code=AGENT,
        model_config_id=1,
        scenario_code="supplier_risk",
        slots={"supplierKey": "A013"},
        mode="task",
    )


@pytest.mark.asyncio
async def test_reuse_hits_when_stamp_matches(
    client: AsyncClient,
    dbSession: AsyncSession,
    seed_data_source: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbSession.add(_SCENARIO_ROW)
    feature = FeatureDefinition(
        feature_name="SUPPLIER_OTD_3M",
        entity_type="SUPPLIER",
        calculation_logic="SELECT 1",
        datasource_id=seed_data_source.id,
    )
    dbSession.add(feature)
    await dbSession.flush()
    stampTime = datetime(2026, 11, 25, 8, 0, 0, tzinfo=UTC)
    dbSession.add(
        FeatureValue(
            feature_id=feature.id,
            entity_key="A013",
            value=100,
            valid_at=stampTime.date(),
            computed_at=stampTime,
        )
    )
    await _seedRunAndTurn(dbSession, runId="run-stamp-hit", dataStamp=stampTime.isoformat())

    runner = _CapturingRunner()
    _installFakes(monkeypatch, runner)
    await copilot.prepareCopilotTurn(dbSession, _dto(), SimpleNamespace(userId="u1"), service=runner)
    assert runner.created == []  # 戳一致 ⇒ 复用，不开新 run


@pytest.mark.asyncio
async def test_reuse_misses_when_feature_recomputed(
    client: AsyncClient,
    dbSession: AsyncSession,
    seed_data_source: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbSession.add(_SCENARIO_ROW)
    feature = FeatureDefinition(
        feature_name="SUPPLIER_OTD_3M",
        entity_type="SUPPLIER",
        calculation_logic="SELECT 1",
        datasource_id=seed_data_source.id,
    )
    dbSession.add(feature)
    await dbSession.flush()
    stampTime = datetime(2026, 11, 25, 8, 0, 0, tzinfo=UTC)
    dbSession.add(
        FeatureValue(
            feature_id=feature.id,
            entity_key="A013",
            value=100,
            valid_at=stampTime.date(),
            computed_at=stampTime,
        )
    )
    await _seedRunAndTurn(dbSession, runId="run-stale", dataStamp=stampTime.isoformat())

    # 指标重算 ⇒ computed_at 前移 ⇒ 戳变化 ⇒ 旧 run 不再命中
    recomputed = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
    fv = (await dbSession.execute(select(FeatureValue))).scalars().first()
    fv.computed_at = recomputed
    await dbSession.commit()

    runner = _CapturingRunner()
    _installFakes(monkeypatch, runner)
    await copilot.prepareCopilotTurn(dbSession, _dto(), SimpleNamespace(userId="u1"), service=runner)
    assert len(runner.created) == 1  # 戳不一致 ⇒ 新跑
