"""KPI 对账审计与灰度开关（真实 PostgreSQL）。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure import database as databaseModule
from app.models.system_config import SystemConfig
from app.services import kpi_runtime_audit as runtimeAudit
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
from app.services.kpi_runtime_audit import (
    RUNTIME_ACTOR,
    RUNTIME_ENTITY_TYPE,
    buildRuntimePayload,
    recordKpiRuntimeEvent,
)


@pytest.fixture
def session(dbSession: AsyncSession) -> AsyncSession:
    return dbSession


@pytest.fixture(autouse=True)
def _runtimeEventsUseTestDb(seedEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时事件走独立会话 ⇒ 工厂必须指向测试库。

    生产由 lifespan 建 ``_sessionFactory``；unit 夹具只给 ``seedEngine`` 的
    工厂，不替换全局的话 writer 会连到配置里的业务库（5432）。
    """
    factory, _ = seedEngine
    monkeypatch.setattr(databaseModule, "getSessionFactory", lambda: factory)


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


# ---------------------------------------------------------------------------
# 运行时 Tier 事件（M4 看板：Tier 分布 / 降级原因 / 命中率）
# ---------------------------------------------------------------------------

async def _runtimeCount(session: AsyncSession) -> int:
    row = await session.execute(
        text("SELECT COUNT(*) FROM audit_log WHERE entity_type = :t"),
        {"t": RUNTIME_ENTITY_TYPE},
    )
    return int(row.scalar_one())


def test_runtime_payload_shape() -> None:
    """纯函数钉形状：看板按 `tier` / `reason` 两个 JSONB 键聚合。"""
    payload = buildRuntimePayload(
        kpiCode="KPI_RT",
        tier="B",
        reason="tier_a_execution_error",
        matchMethod="exact_code",
    )
    assert payload == {
        "kpi_code": "KPI_RT",
        "tier": "B",
        "reason": "tier_a_execution_error",
        "match_method": "exact_code",
    }


async def test_runtime_event_writes_audit_row(session: AsyncSession) -> None:
    """每次路由写一行 `kpi_runtime_event`，entity_id 对齐 kpi_catalog.id。"""
    before = await _runtimeCount(session)
    written = await recordKpiRuntimeEvent(
        kpiId=4243,
        kpiCode="KPI_RT_WRITE",
        tier="B",
        reason="tier_a_execution_error",
        matchMethod="exact_code",
    )
    assert written is True
    assert await _runtimeCount(session) == before + 1
    row = await session.execute(
        text(
            "SELECT entity_type, entity_id, action, actor, after_json FROM audit_log "
            "WHERE entity_type = :t AND entity_id = 4243"
        ),
        {"t": RUNTIME_ENTITY_TYPE},
    )
    rec = row.mappings().one()
    assert rec["entity_type"] == "kpi_runtime_event"
    assert rec["entity_id"] == 4243
    assert rec["action"] == "CREATE"
    assert rec["actor"] == RUNTIME_ACTOR
    after = rec["after_json"]
    if isinstance(after, str):
        after = json.loads(after)
    assert after["kpi_code"] == "KPI_RT_WRITE"
    assert after["tier"] == "B"
    assert after["reason"] == "tier_a_execution_error"
    assert after["match_method"] == "exact_code"


async def test_runtime_event_survives_without_caller_commit() -> None:
    """事件必须自提交：流式终结分支从不 commit 请求会话。

    用**另一条连接**读同一行 —— 若事件仍挂在某个未提交事务里，这里读不到。
    """
    written = await recordKpiRuntimeEvent(
        kpiId=4246, kpiCode="KPI_RT_DURABLE", tier="A"
    )
    assert written is True
    async with databaseModule.getSessionFactory()() as reader:
        row = await reader.execute(
            text(
                "SELECT after_json FROM audit_log "
                "WHERE entity_type = :t AND entity_id = 4246"
            ),
            {"t": RUNTIME_ENTITY_TYPE},
        )
        after = row.scalar_one()
    if isinstance(after, str):
        after = json.loads(after)
    assert after["tier"] == "A"


async def test_runtime_event_null_reason_preserved(session: AsyncSession) -> None:
    """Tier A 正常执行无降级原因 ⇒ reason 落 NULL（看板不得计入降级）。"""
    written = await recordKpiRuntimeEvent(
        kpiId=4244, kpiCode="KPI_RT_A", tier="A", matchMethod="keyword"
    )
    assert written is True
    row = await session.execute(
        text(
            "SELECT after_json FROM audit_log "
            "WHERE entity_type = :t AND entity_id = 4244"
        ),
        {"t": RUNTIME_ENTITY_TYPE},
    )
    after = row.scalar_one()
    if isinstance(after, str):
        after = json.loads(after)
    assert after["tier"] == "A"
    assert after["reason"] is None


async def test_runtime_event_store_failure_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """审计服务抛错 ⇒ 返回 False、零残留，且调用方事务不受影响。"""

    async def _boom(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(runtimeAudit._audit, "record", _boom)

    before = await _runtimeCount(session)
    written = await recordKpiRuntimeEvent(
        kpiId=4245, kpiCode="KPI_RT_FAIL", tier="C"
    )
    # 调用方事务仍可用：后续写入能提交，且没有残留的运行时事件行。
    session.add(SystemConfig(key="KPI_TEST_AFTER_RUNTIME_FAIL", value="ok"))
    await session.commit()

    assert written is False
    assert await _runtimeCount(session) == before
    row = await session.execute(
        text("SELECT value FROM system_config WHERE key = :k"),
        {"k": "KPI_TEST_AFTER_RUNTIME_FAIL"},
    )
    assert row.scalar_one() == "ok"


async def test_runtime_event_db_failure_is_swallowed(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 DB 失败路径：不可序列化的负载 ⇒ 提交报错被吞、独立会话回滚。

    不替身：让 ``buildRuntimePayload`` 产出 ``object()``，由 asyncpg 在绑定
    JSONB 时抛错 —— 走的是真实 flush/commit，而不是 monkeypatch 掉的服务方法。
    """
    monkeypatch.setattr(
        runtimeAudit, "buildRuntimePayload", lambda **_: {"tier": object()}
    )

    before = await _runtimeCount(session)
    written = await recordKpiRuntimeEvent(
        kpiId=4247, kpiCode="KPI_RT_DB_FAIL", tier="C"
    )

    assert written is False
    assert await _runtimeCount(session) == before
