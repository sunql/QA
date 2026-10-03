"""ResearchSqlRunner：只读执行 + 行上限/超时 + 验证失败不抛（真实 PG）。"""

import pytest

from app.services import research_sql_runner
from app.services.hypothesis_service import Hypothesis
from app.services.research_sql_runner import VERIFICATION_ROW_CAP, ResearchSqlRunner


@pytest.mark.asyncio
async def test_execute_readonly_select(dbSession) -> None:
    runner = ResearchSqlRunner()
    rows = await runner.executeReadonlySql(dbSession, "SELECT 1 AS ONE FROM (SELECT 1) T")
    assert rows and list(rows[0].values())[0] == 1


@pytest.mark.asyncio
async def test_rejects_dml(dbSession) -> None:
    runner = ResearchSqlRunner()
    with pytest.raises(ValueError):
        await runner.executeReadonlySql(dbSession, "DELETE FROM research_session")


@pytest.mark.asyncio
async def test_run_verification_returns_error_dict_not_raise(dbSession) -> None:
    runner = ResearchSqlRunner()
    bad = Hypothesis(statement="s", driver=None, verificationSql="SELECT * FROM 不存在的表")
    outcome = await runner.runVerification(dbSession, bad)
    assert outcome["error"] is not None and outcome["rows"] == []


@pytest.mark.asyncio
async def test_run_verification_failure_leaves_session_usable(dbSession) -> None:
    """失败事务必须回滚：否则同一 session 后续语句抛 PendingRollbackError。"""
    runner = ResearchSqlRunner()
    bad = Hypothesis(statement="s", driver=None, verificationSql="SELECT * FROM 不存在的表")
    assert (await runner.runVerification(dbSession, bad))["error"] is not None
    rows = await runner.executeReadonlySql(dbSession, "SELECT 1 AS ONE FROM (SELECT 1) T")
    assert rows and list(rows[0].values())[0] == 1


@pytest.mark.asyncio
async def test_execute_readonly_truncates_over_row_cap(dbSession) -> None:
    """超过行上限 → 截断到 VERIFICATION_ROW_CAP（与 business_db_pool 同语义）。"""
    runner = ResearchSqlRunner()
    sql = f"SELECT n FROM generate_series(1, {VERIFICATION_ROW_CAP + 5}) AS n"
    rows = await runner.executeReadonlySql(dbSession, sql)
    assert len(rows) == VERIFICATION_ROW_CAP


@pytest.mark.asyncio
async def test_execute_readonly_times_out(dbSession, monkeypatch) -> None:
    """超时 → TimeoutError。

    pg_sleep 被 SQL Guard 的 _FORBIDDEN_CALLS 拒绝（时序侧信道，见
    business_db_pool.py），故用 generate_series 造一条必超时的重查询；
    超时常量 monkeypatch 到 50ms，避免测试真的等满默认超时。
    """
    monkeypatch.setattr(research_sql_runner, "VERIFICATION_TIMEOUT_SECONDS", 0.05)
    runner = ResearchSqlRunner()
    with pytest.raises(TimeoutError):
        await runner.executeReadonlySql(
            dbSession, "SELECT COUNT(*) FROM generate_series(1, 50000000) AS n"
        )
