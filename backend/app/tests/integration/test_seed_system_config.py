"""seed_system_config 集成测试（真实 PostgreSQL）。

`Harness/rules/魔数治理.md` §3 要求种子脚本幂等、§4 要求「默认值改了必须走种子
脚本，否则 dev/prod 漂移」。本文件把这两条都变成可执行的断言：

1. **漂移**：源码里每个 `_CHART_*_DEFAULT` 都必须有一条同值的种子条目 —— 只改源码
   不改种子，这里先红，而不是等线上表现不一致。
2. **幂等**：跑两次行数不变（DO UPDATE 语义下冲突行也被计入，故行数恒定）。
3. **下发**：已存在的不同值会被重置为源码默认 —— 这是刻意行为（默认值变更的
   下发通道），用测试把它钉住，以免有人「顺手」改成 DO NOTHING。

强制规则（Harness/rules/测试规范.md）：真实 PostgreSQL，每测试 TRUNCATE 隔离。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.system_config import SystemConfig
from app.services.chart_thresholds import (
    _CHART_HBAR_MIN_ROWS_DEFAULT,
    _CHART_HEATMAP_MIN_COVERAGE_DEFAULT,
    _CHART_PIE_MAX_ROWS_DEFAULT,
    _CHART_TOP_N_MAX_DEFAULT,
)
from app.services.data_summary import FULL_DATA_THRESHOLD
from app.tests import _pg_support
from scripts.seed_system_config import CHART_CONFIG_SEEDS, seed_system_config


@pytest.fixture()
async def pgFactory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """真实 PG 的空库会话工厂（每测试 TRUNCATE 隔离）。"""
    engine = await _pg_support._newEngine()
    try:
        await _pg_support._truncateAll(engine)
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
    finally:
        await engine.dispose()


class TestSeedEntriesMatchSourceDefaults:
    """纯模块级检查：不需要数据库。"""

    def test_every_governed_default_is_seeded(self) -> None:
        seeded = {key: value for key, value, _ in CHART_CONFIG_SEEDS}
        expected = {
            "CHART_PIE_MAX_ROWS": str(_CHART_PIE_MAX_ROWS_DEFAULT),
            "CHART_HBAR_MIN_ROWS": str(_CHART_HBAR_MIN_ROWS_DEFAULT),
            "CHART_HEATMAP_MIN_COVERAGE": str(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT),
            "CHART_TOP_N_MAX": str(_CHART_TOP_N_MAX_DEFAULT),
            "FULL_DATA_THRESHOLD": str(FULL_DATA_THRESHOLD),
        }
        assert seeded == expected

    def test_keys_are_unique(self) -> None:
        keys = [key for key, _, _ in CHART_CONFIG_SEEDS]
        assert len(keys) == len(set(keys))

    def test_every_entry_has_a_description(self) -> None:
        """admin 在配置页靠 description 理解这个数字的后果。"""
        for key, _, description in CHART_CONFIG_SEEDS:
            assert description.strip(), f"{key} 缺少 description"


@pytest.mark.asyncio
async def test_seed_is_idempotent(pgFactory: async_sessionmaker[AsyncSession]) -> None:
    """跑两次行数不变（DO UPDATE：冲突行同样计入）。"""
    first = await seed_system_config(pgFactory)
    second = await seed_system_config(pgFactory)

    assert first == second == len(CHART_CONFIG_SEEDS)
    async with pgFactory() as session:
        rows = (await session.execute(select(SystemConfig.key))).scalars().all()
    assert sorted(rows) == sorted(key for key, _, _ in CHART_CONFIG_SEEDS)


@pytest.mark.asyncio
async def test_seed_resets_an_existing_value(
    pgFactory: async_sessionmaker[AsyncSession],
) -> None:
    """已存在且被 admin 改过的值 → 重置为源码默认（刻意的下发语义）。

    若把 conflict 路径改成 DO NOTHING，本用例会红 —— 那正是要防的回归：
    「默认值改了但下发不到 prod」。
    """
    async with pgFactory() as session:
        session.add(SystemConfig(key="CHART_PIE_MAX_ROWS", value="99"))
        await session.commit()

    await seed_system_config(pgFactory)

    async with pgFactory() as session:
        value = (
            await session.execute(
                select(SystemConfig.value).where(SystemConfig.key == "CHART_PIE_MAX_ROWS")
            )
        ).scalar_one()
    assert value == str(_CHART_PIE_MAX_ROWS_DEFAULT)


@pytest.mark.asyncio
async def test_seeded_values_are_read_back_by_the_readers(
    pgFactory: async_sessionmaker[AsyncSession],
) -> None:
    """种子写下的值，必须能被读取函数按同一口径读出来（端到端闭环）。"""
    from app.services.chart_thresholds import loadChartThresholds

    async with pgFactory() as session:
        session.add(SystemConfig(key="CHART_TOP_N_MAX", value="42"))
        await session.commit()

    async with pgFactory() as session:
        thresholds = await loadChartThresholds(session)

    assert thresholds.topNMax == 42
