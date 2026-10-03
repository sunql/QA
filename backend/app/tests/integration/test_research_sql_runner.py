"""ResearchSqlRunner：只读执行 + 验证失败不抛（真实 PG）。"""

import pytest

from app.services.research_sql_runner import ResearchSqlRunner


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
    from app.services.hypothesis_service import Hypothesis

    runner = ResearchSqlRunner()
    bad = Hypothesis(statement="s", driver=None, verificationSql="SELECT * FROM 不存在的表")
    outcome = await runner.runVerification(dbSession, bad)
    assert outcome["error"] is not None and outcome["rows"] == []
