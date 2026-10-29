"""KPI 对账审计与灰度开关（真实 PostgreSQL）。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig
from app.services.kpi_metric_audit import (
    RECONCILE_ENTITY_TYPE,
    recordReconcileResult,
)
from app.services.kpi_metric_reconcile import (
    VERDICT_MISMATCH,
    VERDICT_PASS,
    VERDICT_UNKNOWN,
    ReconcileFinding,
    ReconcileResult,
)
from app.services.kpi_metric_switches import readKpiMetricSwitch


@pytest.fixture
def session(dbSession: AsyncSession) -> AsyncSession:
    return dbSession


def _finding(reason: str, verdict: str) -> ReconcileFinding:
    return ReconcileFinding(
        layer="governance", reason=reason, detail="测试明细", verdict=verdict
    )


async def test_pass_result_does_not_write_audit(session: AsyncSession) -> None:
    """pass 不落 audit_log（避免噪声）。"""
    before = await _auditCount(session)
    written = await recordReconcileResult(
        session,
        kpiId=1,
        kpiCode="KPI_X",
        result=ReconcileResult(verdict=VERDICT_PASS, findings=()),
    )
    await session.commit()
    assert written is False
    assert await _auditCount(session) == before


async def test_mismatch_result_writes_audit(session: AsyncSession) -> None:
    before = await _auditCount(session)
    written = await recordReconcileResult(
        session,
        kpiId=1,
        kpiCode="KPI_X",
        result=ReconcileResult(
            verdict=VERDICT_MISMATCH,
            findings=(_finding("grain_mismatch", VERDICT_MISMATCH),),
        ),
    )
    await session.commit()
    assert written is True
    assert await _auditCount(session) == before + 1


async def test_unknown_result_writes_audit(session: AsyncSession) -> None:
    written = await recordReconcileResult(
        session,
        kpiId=2,
        kpiCode="KPI_Y",
        result=ReconcileResult(
            verdict=VERDICT_UNKNOWN,
            findings=(_finding("governance_columns_absent", VERDICT_UNKNOWN),),
        ),
    )
    await session.commit()
    assert written is True


async def test_audit_row_shape(session: AsyncSession) -> None:
    await recordReconcileResult(
        session,
        kpiId=4242,
        kpiCode="KPI_SHAPE",
        result=ReconcileResult(
            verdict=VERDICT_MISMATCH,
            findings=(_finding("grain_mismatch", VERDICT_MISMATCH),),
        ),
    )
    await session.commit()
    row = await session.execute(
        text(
            "SELECT entity_type, entity_id, action, after_json FROM audit_log "
            "WHERE entity_type = :t AND entity_id = 4242"
        ),
        {"t": RECONCILE_ENTITY_TYPE},
    )
    rec = row.mappings().one()
    assert rec["entity_type"] == "kpi_metric_reconcile"
    assert rec["entity_id"] == 4242
    assert rec["action"] == "CREATE"
    after = rec["after_json"]
    if isinstance(after, str):
        after = json.loads(after)
    assert after["verdict"] == "mismatch"
    assert after["kpi_code"] == "KPI_SHAPE"
    assert after["findings"][0]["reason"] == "grain_mismatch"


async def test_switch_recognizes_falsy_value(session: AsyncSession) -> None:
    session.add(SystemConfig(key="KPI_TEST_FALSY", value=" Off "))
    await session.commit()
    assert await readKpiMetricSwitch(session, "KPI_TEST_FALSY", True) is False


async def test_switch_recognizes_truthy_value(session: AsyncSession) -> None:
    session.add(SystemConfig(key="KPI_TEST_TRUTHY", value="YES"))
    await session.commit()
    assert await readKpiMetricSwitch(session, "KPI_TEST_TRUTHY", False) is True


async def test_switch_missing_uses_default(session: AsyncSession) -> None:
    assert await readKpiMetricSwitch(session, "KPI_TEST_MISSING", True) is True
    assert await readKpiMetricSwitch(session, "KPI_TEST_MISSING", False) is False


async def test_switch_invalid_uses_default(session: AsyncSession) -> None:
    session.add(SystemConfig(key="KPI_TEST_INVALID", value="not-a-number"))
    await session.commit()
    assert await readKpiMetricSwitch(session, "KPI_TEST_INVALID", True) is True


async def _auditCount(session: AsyncSession) -> int:
    row = await session.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE entity_type = :t"),
        {"t": RECONCILE_ENTITY_TYPE},
    )
    return int(row.scalar_one())
