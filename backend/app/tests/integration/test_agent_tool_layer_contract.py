"""AgentTool.data_layers 集成契约测试（Phase 7 G6，真实 PG）。

核心防漂移验证：执行内置工具 handler，用 SQLAlchemy before_cursor_execute
追踪实际触达的表，断言「工具声明的 data_layers」==「实际读取表的层」。
防止：声明加了一层但 handler 没读、或 handler 开始读新表但声明没跟上。

- supplier_360 / supplier_risk → EntityMapping(DIM) + Feature(DEF/VALUE)(FEATURE)
- graph_traverse → Neo4j 业务实体子图（DIM+DWD），图实体分类在单测覆盖
  （本文件不执行 graph handler：需要真实 Neo4j，且其 SQL 走图库不落 PG）
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    EntityType,
    FeatureRefreshFrequency,
    FeatureStatus,
    MatchRule,
    SourceSystem,
)
from app.domain.models import DataSource, EntityMapping, FeatureDefinition, FeatureValue
from app.services.agent_tools import AgentToolContext
from app.services.agent_tool_config_registry import agent_tool_config_registry as agent_tool_registry
from app.services.agent_tool_config_service import AgentToolConfigService

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _warmToolRegistry(dbSession: AsyncSession) -> None:
    """DB-backed registry 需 warmUp；test fixture 用 TOOL_SEEDS seed + warm。"""
    service = AgentToolConfigService()
    for seed in (
        {
            "name": "supplier_360",
            "data_object": "SUPPLIER",
            "data_layers": ["DIM", "FEATURE"],
            "handler_kind": "BUILTIN",
            "handler_ref": "supplier_360",
            "arg_extractor_kind": "supplier_key",
        },
        {
            "name": "supplier_risk",
            "data_object": "SUPPLIER",
            "data_layers": ["DIM", "FEATURE"],
            "handler_kind": "BUILTIN",
            "handler_ref": "supplier_risk",
            "arg_extractor_kind": "supplier_risk_key",
        },
    ):
        await service.upsertSeed(dbSession, seed["name"], seed)
    await dbSession.commit()
    agent_tool_registry.invalidate()
    await agent_tool_registry.warmUp(dbSession)
    yield
    agent_tool_registry.invalidate()

# 表 → 层 词汇表（SSOT，与 Harness/wiki 层词汇表对齐）
_TABLE_LAYER = {
    "entity_mapping": "DIM",
    "feature_definition": "FEATURE",
    "feature_value": "FEATURE",
}

_DS_ID = 9701
_SUPPLIER_KEY = 100001
_SUPPLIER_CODE = "SUP000001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _seed(dbSession: AsyncSession) -> None:
    dbSession.add(
        DataSource(
            id=_DS_ID,
            name="contract-ds",
            type="postgresql",
            host="localhost",
            port=5432,
            database_name="test_db",
            username="u",
            password_encrypted="x",
        )
    )
    dbSession.add(
        EntityMapping(
            entity_type=EntityType.SUPPLIER,
            enterprise_key=_SUPPLIER_KEY,
            enterprise_code=_SUPPLIER_CODE,
            source_system=SourceSystem.ERP,
            source_key=f"V{_SUPPLIER_KEY}",
            source_code=f"V{_SUPPLIER_KEY}",
            match_rule=MatchRule.MDM_MASTER,
            owner="procurement",
        )
    )
    dbSession.add(
        FeatureDefinition(
            id=1,
            feature_name="SUPPLIER_RISK_SCORE",
            feature_alias="综合风险评分",
            feature_definition="auto",
            entity_type=EntityType.SUPPLIER,
            calculation_logic="SELECT 1",
            window_size="12M",
            refresh_frequency=FeatureRefreshFrequency.DAILY,
            unit="score",
            status=FeatureStatus.ACTIVE,
            is_enabled=True,
            datasource_id=_DS_ID,
            version="v1.0",
        )
    )
    dbSession.add(
        FeatureValue(
            feature_id=1,
            entity_key=_SUPPLIER_CODE,
            value=0.50,
            valid_at=date(2026, 8, 31),
            computed_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )
    await dbSession.commit()


def _installTracer(dbSession: AsyncSession) -> tuple[set[str], object]:
    """在会话绑定引擎上挂 before_cursor_execute，收集触达的表名。"""
    tables: set[str] = set()

    def _collect(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        compiled = getattr(context, "compiled", None)
        stmt = getattr(compiled, "statement", None)
        if stmt is None:
            return
        froms = (
            stmt.get_final_froms()
            if hasattr(stmt, "get_final_froms")
            else getattr(stmt, "froms", ())
        )
        for from_ in froms:
            name = getattr(from_, "name", None)
            if name:
                tables.add(name)

    engine = dbSession.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _collect)
    return tables, _collect


async def _traceHandler(dbSession: AsyncSession, tool_name: str) -> set[str]:
    tool = agent_tool_registry.get(tool_name)
    assert tool is not None
    tables, listener = _installTracer(dbSession)
    try:
        await tool.handler(
            dbSession,
            {"key": str(_SUPPLIER_KEY)},
            AgentToolContext(llm_factory=None, actor="contract-test"),
        )
    finally:
        event.remove(dbSession.bind.sync_engine, "before_cursor_execute", listener)
    return tables


# ---------------------------------------------------------------------------
# 契约用例
# ---------------------------------------------------------------------------


class TestSupplierToolLayerContract:
    async def test_supplier_360_touches_only_declared_tables(
        self, client, dbSession
    ) -> None:
        await _seed(dbSession)
        tables = await _traceHandler(dbSession, "supplier_360")
        # handler 必须触达实体映射 + 特征两族表（缺任一都算契约破裂）
        assert {"entity_mapping", "feature_definition"} <= tables
        assert "feature_value" in tables
        # 无未声明表：任一触达表都必须在 SSOT 映射里（防「开始读新表但声明没跟上」）
        assert set(tables) <= set(_TABLE_LAYER), (
            f"未声明表被触达: {sorted(set(tables) - set(_TABLE_LAYER))}"
        )
        # 触达表 → 层 必须与声明一致
        touched_layers = {_TABLE_LAYER[t] for t in tables}
        assert touched_layers == {"DIM", "FEATURE"}
        declared = set(agent_tool_registry.get("supplier_360").data_layers)
        assert declared == touched_layers

    async def test_supplier_risk_touches_only_declared_tables(
        self, client, dbSession
    ) -> None:
        await _seed(dbSession)
        tables = await _traceHandler(dbSession, "supplier_risk")
        assert {"entity_mapping", "feature_definition", "feature_value"} <= tables
        assert set(tables) <= set(_TABLE_LAYER), (
            f"未声明表被触达: {sorted(set(tables) - set(_TABLE_LAYER))}"
        )
        touched_layers = {_TABLE_LAYER[t] for t in tables}
        assert touched_layers == {"DIM", "FEATURE"}
        declared = set(agent_tool_registry.get("supplier_risk").data_layers)
        assert declared == touched_layers

    async def test_supplier_tools_declare_identical_layer_sets(
        self, client, dbSession
    ) -> None:
        # supplier_risk 复用 Supplier360Service.get360，两层契约必须完全一致
        assert (
            agent_tool_registry.get("supplier_360").data_layers
            == agent_tool_registry.get("supplier_risk").data_layers
        )
