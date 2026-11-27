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
from app.services.branding_service import (
    BRAND_NAME_EN_KEY,
    BRAND_NAME_KEY,
    BRAND_SHORT_NAME_KEY,
    BRAND_TITLE_KEY,
    DEFAULT_BRAND_NAME,
    DEFAULT_BRAND_NAME_EN,
    DEFAULT_BRAND_SHORT_NAME,
)
from app.services.chart_thresholds import (
    _CHART_HBAR_MIN_ROWS_DEFAULT,
    _CHART_HEATMAP_MIN_COVERAGE_DEFAULT,
    _CHART_PIE_MAX_ROWS_DEFAULT,
    _CHART_TOP_N_MAX_DEFAULT,
)
from app.services.data_summary import FULL_DATA_THRESHOLD

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
    (
        "FULL_DATA_THRESHOLD",
        str(FULL_DATA_THRESHOLD),
        "数据清单全量/摘要分界行数：查询结果不超过此行数时全量注入回答 prompt，"
        "超过则只给首尾样本；调小省 token 但中间行可能丢失（如 27 行的 B125）",
    ),
)

#: 品牌配置种子（feat-configurable-brand）。
#:
#: ⚠️ **与上面的阈值种子语义相反**：阈值是「默认值变更下发」通道（DO UPDATE，
#: 重跑即重置为源码默认）；品牌是**内容**，admin 在「系统配置」页改过之后
#: **不能**被发版重置 —— 否则每次部署品牌名都被打回原形，运维会认为功能坏了。
#:
#: 所以品牌种子走 `seed_brand_config`（**value 不覆盖**）：
#: 行不存在 ⇒ 插一行（让 admin 能在配置页看到并改）；行已存在 ⇒ **value 原样保留**
#: （只刷 description 元数据）。
#:
#: 值一律从源码常量派生（不写字面量），这样改源码默认值后种子自动跟随，
#: 漂移只可能来自「忘了把新键加进来」——由
#: `app/tests/unit/test_branding_service.py::test_brand_seed_covers_all_brand_keys` 拦住。
#:
#: ⚠️ 唯一例外：`BRAND_TITLE` 播种为 **NULL（未配置）** —— 它的语义是
#: 「未配置时跟随品牌主名称」。若播种成写死的主名，admin 改了主名却发现标题没跟着
#: 变，会以为功能没生效。留空 + description 说明，是这里唯一诚实的表达。
BRAND_CONFIG_SEEDS: tuple[tuple[str, str | None, str], ...] = (
    (
        BRAND_NAME_KEY,
        DEFAULT_BRAND_NAME,
        "品牌主名称（中文）：侧边栏展开态、登录页标题、页面标题回落源（超 64 字符会被截断）",
    ),
    (
        BRAND_NAME_EN_KEY,
        DEFAULT_BRAND_NAME_EN,
        "品牌英文名：英文界面下的侧边栏展开态（超 64 字符会被截断）",
    ),
    (
        BRAND_SHORT_NAME_KEY,
        DEFAULT_BRAND_SHORT_NAME,
        "品牌缩写：侧边栏折叠态显示（宽度有限，只放得下 2-4 个字符；超 16 字符会被截断）",
    ),
    (
        BRAND_TITLE_KEY,
        None,
        "浏览器标签页标题；留空则跟随「品牌主名称」（超 128 字符会被截断）",
    ),
)


async def seed_brand_config(factory: async_sessionmaker[AsyncSession]) -> int:
    """播种 4 个品牌配置键（**value 绝不覆盖**，description 可刷新）。

    与 `seed_system_config` 的区别是本函数**不碰 value**：
    阈值种子负责「默认值变更下发」，品牌种子只负责「保证键存在、让 admin 可改」。

    冲突路径用 `on_conflict_do_update(set_={"description": ...})` ——
    只刷 description（元数据：admin 在配置页靠它理解这个键），
    **value 一个字都不动**（admin 改过的品牌名不能被发版重置）。

    返回处理行数（与 `seed_system_config` 口径一致：冲突行同样计入）。
    """
    async with factory() as session:
        for key, value, description in BRAND_CONFIG_SEEDS:
            stmt = (
                pg_insert(SystemConfig)
                .values(key=key, value=value, description=description)
                .on_conflict_do_update(
                    index_elements=["key"],
                    set_={"description": description},
                )
            )
            await session.execute(stmt)
        await session.commit()
    return len(BRAND_CONFIG_SEEDS)


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
        b = await seed_brand_config(factory)
        print(f"seed_brand_config: {b} rows ensured (existing values kept)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
