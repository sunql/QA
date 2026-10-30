import re

from sqlalchemy import inspect, text

from app.domain.models import Base

ORCHESTRATION_TABLES = {
    "agent_orchestration_profile",
    "agent_orchestration_binding",
    "agent_orchestration_run",
    "agent_orchestration_step",
    "agent_orchestration_usage",
}


def test_orchestration_models_register_on_existing_metadata():
    assert Base.metadata.tables.keys() >= ORCHESTRATION_TABLES


async def test_orchestration_schema_is_additive(dbSession):
    tables = set(await dbSession.run_sync(lambda s: inspect(s.connection()).get_table_names()))
    assert tables >= ORCHESTRATION_TABLES
    columns = await dbSession.run_sync(
        lambda s: {c["name"] for c in inspect(s.connection()).get_columns("agent_orchestration_run")}
    )
    assert {"run_id", "agent_code", "cost_source", "cost_complete", "lease_until"} <= columns
    fk_rows = (await dbSession.execute(text(
        "SELECT confdeltype::text FROM pg_constraint "
        "WHERE conrelid='agent_orchestration_run'::regclass AND contype='f'"
    ))).scalars().all()
    assert "n" in fk_rows  # agent_id ON DELETE SET NULL


async def test_legacy_agent_tables_have_no_orchestration_columns(dbSession):
    for table in ("agent_definition", "agent_access_policy", "agent_tool_config"):
        columns = await dbSession.run_sync(
            lambda s, table=table: {
                c["name"] for c in inspect(s.connection()).get_columns(table)
            }
        )
        assert not any("orchestration" in c or c == "execution_mode" for c in columns)


async def test_pause_code_is_constrained_to_the_protocol_values(dbSession):
    rows = (await dbSession.execute(text(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='agent_orchestration_run'::regclass "
        "AND conname='ck_agent_orchestration_run_pause_code'"
    ))).scalars().all()
    assert len(rows) == 1
    for code in (
        "NEEDS_INPUT",
        "AMBIGUOUS",
        "PERMISSION_DENIED",
        "BUDGET_EXHAUSTED",
        "TOOL_FAILED",
        "PLANNER_OUTPUT_INVALID",
        "AGENT_UNAVAILABLE",
        "INFRASTRUCTURE_ERROR",
    ):
        assert code in rows[0]


async def _costSourceCheckValues(dbSession, table: str, constraint: str) -> set[str]:
    definition = (await dbSession.execute(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid=to_regclass(:table) AND conname=:constraint"
        ),
        {"table": table, "constraint": constraint},
    )).scalar_one()
    return set(re.findall(r"'([A-Z_]+)'", definition))


async def test_run_cost_source_allows_mixed_while_usage_stays_three(dbSession):
    runValues = await _costSourceCheckValues(
        dbSession, "agent_orchestration_run", "ck_agent_orchestration_run_cost_source"
    )
    usageValues = await _costSourceCheckValues(
        dbSession, "agent_orchestration_usage", "ck_agent_orchestration_usage_cost_source"
    )
    assert runValues == {"MEASURED", "ESTIMATED", "UNKNOWN", "MIXED"}
    assert usageValues == {"MEASURED", "ESTIMATED", "UNKNOWN"}
