"""P1-5 turn_result_repository 单元测试（RED）。

基于 ResearchReport.payload.turn_results 子结构操作，不加新表。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import ResearchReport, ResearchSession
from app.services.turn_result_repository import TurnResult, TurnResultRepository


@pytest_asyncio.fixture
async def research_report(dbSession: AsyncSession) -> ResearchReport:
    """创建一个空的 research_report（payload={}）。"""
    session_id = uuid4()
    # 先创建 parent research_session（ResearchReport 有 FK）
    parent = ResearchSession(
        id=session_id,
        title="test",
        mode="auto",
        status="active",
        created_by=None,
    )
    dbSession.add(parent)
    await dbSession.flush()
    report = ResearchReport(
        id=uuid4(),
        session_id=session_id,
        version=1,
        payload={},
        status="draft",
    )
    dbSession.add(report)
    await dbSession.flush()
    return report


@pytest_asyncio.fixture
async def research_report_with_turns(dbSession: AsyncSession) -> ResearchReport:
    """创建一个带 3 条 turn_results 的 research_report。"""
    session_id = uuid4()
    parent = ResearchSession(
        id=session_id,
        title="test with turns",
        mode="auto",
        status="active",
        created_by=None,
    )
    dbSession.add(parent)
    await dbSession.flush()
    report = ResearchReport(
        id=uuid4(),
        session_id=session_id,
        version=1,
        payload={
            "turn_results": [
                {"turn_id": "t1", "result_ref": "r1", "step_results": [{"i": 1}]},
                {"turn_id": "t2", "result_ref": "r2", "step_results": [{"i": 2}]},
                {"turn_id": "t3", "result_ref": "r3", "step_results": [{"i": 3}]},
            ]
        },
        status="draft",
    )
    dbSession.add(report)
    await dbSession.flush()
    return report


@pytest.mark.asyncio
async def test_upsert_creates_turn_results_list_in_payload(
    dbSession: AsyncSession, research_report: ResearchReport
) -> None:
    """首次 upsert 创建 turn_results 列表。"""
    turn = TurnResult(
        turn_id="t1",
        result_ref="r1",
        step_results=[{"i": 1}],
    )
    await TurnResultRepository.upsert(dbSession, research_report.id, turn)
    await dbSession.refresh(research_report)
    assert "turn_results" in research_report.payload
    assert research_report.payload["turn_results"][0]["turn_id"] == "t1"


@pytest.mark.asyncio
async def test_get_by_turn_id_returns_specific_turn(
    dbSession: AsyncSession, research_report_with_turns: ResearchReport
) -> None:
    """get_by_turn_id 按 turn_id 精确返回单条。"""
    turn = await TurnResultRepository.get_by_turn_id(
        dbSession, research_report_with_turns.id, "t2"
    )
    assert turn is not None
    assert turn.turn_id == "t2"
    assert turn.result_ref == "r2"


@pytest.mark.asyncio
async def test_get_all_returns_sorted_by_turn_id(
    dbSession: AsyncSession, research_report_with_turns: ResearchReport
) -> None:
    """get_all 返回所有 turn_result，按 turn_id 升序。"""
    turns = await TurnResultRepository.get_all(
        dbSession, research_report_with_turns.id
    )
    assert [t.turn_id for t in turns] == ["t1", "t2", "t3"]
