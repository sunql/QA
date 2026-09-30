"""seed_system_config - 幂等 upsert 受治理的运行时可调阈值。

`Harness/rules/魔数治理.md` §3 引用本脚本作为受治理阈值的种子入口，但仓库里
一直没有它（此前 4 个键靠 0052 migration 的默认 INSERT 落地）。本脚本补上这条腿，
并把「源码默认值」与「DB 初值」钉在一起：`app/tests/unit/test_seed_system_config.py`
的漂移测试会断言每个 `_CHART_*_DEFAULT` 都有同值的种子条目 —— 改了源码默认值却
忘了改种子，测试会红，而不是等到线上表现不一致才发现。

**conflict 路径用 DO UPDATE 而非 DO NOTHING**（memory `qa-system-seed-upsert-pattern`：
DO NOTHING 会让重跑永远回填不了）。这里的语义是**刻意覆盖**：本脚本是「默认值变更
下发」的通道，对应治理规范反模式表最后一行「默认值改了但未走种子脚本 → dev/prod
配置漂移」。

⚠️ 因此**不要在 admin 已经在线调过值之后重跑**本脚本 —— 会把 value 重置回默认。
脚本会打印哪些键被重置，便于发现。

运行：`./scripts/run_local.sh python -m scripts.seed_system_config`
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import getSettings
from app.models.system_config import SystemConfig
from app.services.chart_thresholds import (
    _CHART_HBAR_MIN_ROWS_DEFAULT,
    _CHART_HEATMAP_MIN_COVERAGE_DEFAULT,
    _CHART_PIE_MAX_ROWS_DEFAULT,
    _CHART_TOP_N_MAX_DEFAULT,
)

# (key, value, description)。value 一律用 str() 从源码常量派生 —— 不写字面量，
# 这样源码默认值一改，种子自动跟随，漂移只可能来自「忘了把新键加进来」。
CHART_CONFIG_SEEDS: tuple[tuple[str, str, str], ...] = (
    (
        "CHART_PIE_MAX_ROWS",
        str(_CHART_PIE_MAX_ROWS_DEFAULT),
        "占比类图表（饼图/环形图）超过此行数则改画横向柱状",
    ),
    (
        "CHART_HBAR_MIN_ROWS",
        str(_CHART_HBAR_MIN_ROWS_DEFAULT),
        "单维度类目行数达到此值时，竖向柱状改横向（类目名挤不下）",
    ),
    (
        "CHART_HEATMAP_MIN_COVERAGE",
        str(_CHART_HEATMAP_MIN_COVERAGE_DEFAULT),
        "热力图门槛：交叉矩阵完备度 = 行数 /(维度1基数×维度2基数)，低于此值退化为柱状",
    ),
    (
        "CHART_TOP_N_MAX",
        str(_CHART_TOP_N_MAX_DEFAULT),
        "TOP N 上限：结果行数超过此值就不按 Top N 处理，改当普通分类比较",
    ),
)


async def seed_system_config(factory: async_sessionmaker[AsyncSession]) -> int:
    """upsert 全部受治理键，返回处理行数。"""
    async with factory() as session:
        # 先读现值：让「哪些键真的被改动」可观测，而不是静默覆盖 admin 的调优。
        existing = dict(
            (
                await session.execute(
                    select(SystemConfig.key, SystemConfig.value).where(
                        SystemConfig.key.in_([k for k, _, _ in CHART_CONFIG_SEEDS])
                    )
                )
            ).all()
        )
        overwritten: list[str] = []
        for key, value, description in CHART_CONFIG_SEEDS:
            if key in existing and existing[key] != value:
                overwritten.append(f"{key}: {existing[key]!r} -> {value!r}")
            stmt = (
                pg_insert(SystemConfig)
                .values(key=key, value=value, description=description)
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"value": value, "description": description},
                )
            )
            await session.execute(stmt)
        await session.commit()

    if overwritten:
        print("⚠️  以下已存在的值被重置为源码默认（admin 调优丢失）：")
        for line in overwritten:
            print(f"   - {line}")
    return len(CHART_CONFIG_SEEDS)


async def main() -> None:
    """CLI 入口：独立引擎跑种子，跑完释放连接池。"""
    settings = getSettings()
    engine = create_async_engine(settings.databaseUrl, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        n = await seed_system_config(factory)
        print(f"seed_system_config: {n} rows upserted")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
