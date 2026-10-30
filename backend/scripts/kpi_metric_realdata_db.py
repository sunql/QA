"""KPI 指标真机验证脚本的数据库 helper（只读为主 + 开关行的可还原写）。

⚠️ 所有 SQL **参数化**（``text()`` + 绑定参数），**禁止字符串拼接**
（``information_schema`` / ``pg_catalog`` 没有 ORM 模型，只能用 ``text()``，
但值必须走绑定参数）。

开关行的写是**可还原**的：写前记录原值，退出前原样写回（原本不存在 ⇒ 删除）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from scripts.kpi_metric_db_guard import PROD_DB_NAME
from scripts.kpi_metric_realdata_lib import check, info


async def currentDatabaseName(session: AsyncSession) -> str:
    """``SELECT current_database()`` —— 守卫第 4 闸（落库实测）。

    库名白名单无法区分 dev 与 prod（同名 ``qa_metadata``）⇒ 建连后必须
    复核「真的连到了我以为的那个库」。
    """
    result = await session.execute(text("SELECT current_database()"))
    row = result.first()
    return "" if row is None else str(row[0])


async def readAlembicVersion(session: AsyncSession) -> str | None:
    """读 ``alembic_version.version_num``（没有版本表 ⇒ ``None``）。"""
    result = await session.execute(text("SELECT version_num FROM alembic_version"))
    row = result.first()
    if row is None:
        return None
    return str(row[0])


async def tableColumns(session: AsyncSession, tableName: str) -> frozenset[str]:
    """当前 schema 下某张表的列名集合（大小写按 PG 原样）。"""
    result = await session.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": tableName},
    )
    return frozenset(str(row[0]) for row in result.all())


async def uniqueConstraintColumnSets(
    session: AsyncSession, tableName: str
) -> set[frozenset[str]]:
    """某张表上所有 UNIQUE 约束的**列集合**。

    用于断言 ``ontology_metric_source`` 上存在
    ``UNIQUE(metric_id, class_id, role)``（三元组顺序无关）。
    """
    result = await session.execute(
        text(
            "SELECT c.conname, a.attname "
            "FROM pg_constraint c "
            "JOIN LATERAL unnest(c.conkey) AS k(attnum) ON true "
            "JOIN pg_attribute a "
            "  ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
            "WHERE c.conrelid = to_regclass(CAST(:table AS text)) "
            "  AND c.contype = 'u'"
        ),
        {"table": tableName},
    )
    grouped: dict[str, set[str]] = {}
    for conname, attname in result.all():
        grouped.setdefault(str(conname), set()).add(str(attname))
    return {frozenset(cols) for cols in grouped.values()}


async def hasIndexOn(
    session: AsyncSession, tableName: str, columnName: str
) -> bool:
    """某张表上是否有覆盖 ``columnName`` 的索引（按 ``indexdef`` 文本判定）。"""
    result = await session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = current_schema() AND tablename = :table "
            "  AND indexdef LIKE :pattern"
        ),
        {"table": tableName, "pattern": f"%({columnName})%"},
    )
    return result.first() is not None


async def fetchKpis(session: AsyncSession) -> list[KpiCatalog]:
    """按 ``kpi_code`` 升序取**全部状态**的 KPI（不按 status 过滤）。

    ⚠️ 与 :meth:`KpiMatchCache.warmUp` 的口径**不同**（后者只缓存 PUBLISHED）
    ⇒ 比条数时必须自己筛 ``status == "PUBLISHED"``（T16-16）。
    """
    result = await session.execute(select(KpiCatalog).order_by(KpiCatalog.kpi_code))
    return list(result.scalars().all())


async def fetchLinkedMetricNames(session: AsyncSession) -> dict[str, str]:
    """``kpi_code → 挂靠的 ontology_metric.metric_name``（仅已挂靠的 KPI）。

    用于 S2.2 的**归属**判定：``metric_id`` 非空只说明「挂上了」，不说明
    「挂对了」。``metric_name`` 是回填的归属键（PLAN 的 ``metricName``，库内
    UNIQUE），拿它反查才能抓出「全量挂靠但挂错对象」。
    """
    result = await session.execute(
        select(KpiCatalog.kpi_code, OntologyMetric.metric_name).join(
            OntologyMetric, OntologyMetric.id == KpiCatalog.metric_id
        )
    )
    return {str(code): str(name) for code, name in result.all()}


async def countMetrics(session: AsyncSession) -> int:
    """``ontology_metric`` 的行数。"""
    result = await session.execute(select(func.count()).select_from(OntologyMetric))
    return int(result.scalar_one())


async def countLinkedKpis(session: AsyncSession) -> int:
    """``kpi_catalog.metric_id`` 非空的 KPI 条数（回填是否已落库的判据）。"""
    result = await session.execute(
        select(func.count())
        .select_from(KpiCatalog)
        .where(KpiCatalog.metric_id.is_not(None))
    )
    return int(result.scalar_one())


async def readSwitchValue(session: AsyncSession, key: str) -> str | None:
    """读 ``system_config`` 某开关的原始值（不存在 ⇒ ``None``）。"""
    row = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
    cfg = row.scalar_one_or_none()
    if cfg is None:
        return None
    return cfg.value


async def writeSwitchValue(session: AsyncSession, key: str, value: str) -> None:
    """写 ``system_config`` 开关（不存在 ⇒ 插入，存在 ⇒ 更新）。"""
    row = await session.execute(select(SystemConfig).where(SystemConfig.key == key))
    cfg = row.scalar_one_or_none()
    if cfg is None:
        session.add(
            SystemConfig(key=key, value=value, description="kpi metric realdata")
        )
        await session.flush()
        await session.commit()
        return
    await session.execute(
        update(SystemConfig)
        .where(SystemConfig.key == key)
        .values(value=value)
    )
    await session.commit()


async def deleteSwitchValue(session: AsyncSession, key: str) -> None:
    """删除 ``system_config`` 开关行（不存在 ⇒ no-op）。"""
    await session.execute(delete(SystemConfig).where(SystemConfig.key == key))
    await session.commit()


async def restoreSwitchValue(
    session: AsyncSession, key: str, original: str | None
) -> None:
    """把开关**还原**到运行前的状态（原本不存在 ⇒ 删除该行）。"""
    if original is None:
        await deleteSwitchValue(session, key)
    else:
        await writeSwitchValue(session, key, original)


async def runWithSwitch(
    session: AsyncSession,
    key: str,
    value: str,
    action: Callable[[], Awaitable[None]],
    label: str,
) -> None:
    """临时写开关 → 执行 action → **无条件还原**并核对前后值。

    S4（发布闸门）与 S5（契约注入）共用：两者都需要在「开关开」的状态下
    验证一条链路，而开关的**真实**取值由人决定（fail-closed 默认关）。
    还原是 ``finally`` 里的硬动作——进程被 SIGKILL 时 ``finally`` 不会执行，
    所以该写法**只允许在隔离测试库使用**（正式库一律 SKIP，见 S4/S5 入口）。

    Defense-in-depth（2026-10-30 安全审查 LOW-3）：入口侧的 ``isProd`` 判断
    靠调用方自觉——这里落库实测库名，是 ``qa_metadata``（prod 同名库）直接
    SystemExit，**一次开关读写都不发生**。不信任调用方先判。
    """
    dbName = await currentDatabaseName(session)
    if dbName == PROD_DB_NAME:
        raise SystemExit(
            f"runWithSwitch 拒绝执行：落库实测 {dbName!r} 是正式库名 "
            f"（SIGKILL 残留开关值的风险不可带入）——临时开关写法只允许在 "
            f"qa_metadata_test* 隔离库使用"
        )
    original = await readSwitchValue(session, key)
    await writeSwitchValue(session, key, value)
    try:
        await action()
    finally:
        await restoreSwitchValue(session, key, original)
        after = await readSwitchValue(session, key)
        check(
            f"{label} 开关已还原",
            after == original,
            f"原值 {original!r} → 还原后 {after!r}",
        )
        info(f"开关对照 {key}：运行前={original!r} 临时={value!r} 还原后={after!r}")


__all__ = [
    "countLinkedKpis",
    "countMetrics",
    "currentDatabaseName",
    "deleteSwitchValue",
    "fetchKpis",
    "fetchLinkedMetricNames",
    "hasIndexOn",
    "readAlembicVersion",
    "readSwitchValue",
    "restoreSwitchValue",
    "runWithSwitch",
    "tableColumns",
    "uniqueConstraintColumnSets",
    "writeSwitchValue",
]
