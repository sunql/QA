"""召回候选集的数据源过滤（2026-11-19 跨源幽灵表根治）。

设计：Harness/changes/2026-11-19-recall-datasource-filter-anchor/design.md。
ontology_class 无「类→数据源」绑定列 ⇒ 召回链全局取类，SQL 却在会话选定
数据源执行 ⇒ 登记在 A 源的物理表会在 B 源会话被召回（生产事故
main_data_supplier → ORA-00942）。本模块用 schema_cache（每数据源物理表
清单，多 owner 行）做运行时校验。三态（用户裁决）：
- 有缓存 + 有 datasourceId：硬过滤；
- datasourceId=None：不过滤（披露 no_datasource）；
- 缓存缺失/空/读失败：不过滤 fail-open（披露 no_cache）——目录缺失不把
  真实表的查询打死。
表名归一与 schema_introspection_service._validateOntologyAgainstSchema 同口径：
裸表名 + owner 限定名并集、.upper() 大小写不敏感、空 source_table 保留。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import SchemaCache

# NOTE: brief 原文写 `from loguru import logger`，但本仓依赖清单无 loguru，
# 全仓日志口径为标准库 logging（对齐 schema_introspection_service），故改用
# stdlib logger；调用点（warning/ exc_info / %s 惰性格式化）语义不变。
logger = logging.getLogger(__name__)


def _tableKeys(schemaData: list[dict[str, Any]]) -> set[str]:
    """schema_data → 可匹配表名集合（裸名 + owner 限定名，全大写）。"""
    keys: set[str] = set()
    for table in schemaData:
        name = table.get("table_name")
        if not name:
            continue
        keys.add(name.upper())
        owner = table.get("owner")
        if owner:
            keys.add(f"{owner}.{name}".upper())
    return keys


async def loadDatasourceTableSet(
    session: AsyncSession, datasourceId: int
) -> set[str] | None:
    """当前数据源的物理表名集合（多 owner 行取并集）。

    无缓存行 / 清单为空 / 读异常 ⇒ None（调用方按 fail-open 处理）；
    读失败只 warning 不阻断主链路（对齐 _getClassFilterMaxClasses 口径）。
    """
    try:
        rows = (
            await session.execute(
                select(SchemaCache).where(SchemaCache.datasource_id == datasourceId)
            )
        ).scalars().all()
    except Exception:
        logger.warning(
            "schema_cache 读取失败，跨源过滤跳过 datasourceId=%s",
            datasourceId, exc_info=True,
        )
        return None
    keys: set[str] = set()
    for row in rows:
        keys |= _tableKeys(row.schema_data or [])
    if not keys:
        # 空/缺两条分支都 fail-open（不过滤），但**必须留服务端痕迹**：
        # 否则「数据源目录缺失」只体现在用户可见的 no_cache 披露里，服务端零日志
        # ⇒ 运维看不到 fail-open 正在生效（无声降级）。
        # ⚠️ 日志只带 datasourceId，**不得**带口令/DSN。
        logger.warning(
            "schema_cache 无可用表清单，跨源过滤 fail-open "
            "rows=%d datasourceId=%s",
            len(rows), datasourceId,
        )
        return None
    return keys


def filterClassesByDatasource(
    classes: list[Any], tableSet: set[str]
) -> tuple[list[Any], list[str]]:
    """按表名集合过滤候选类，返回 (保留类, 被滤 source_table 原文列表)。

    匹配键 = source_table 全文 .upper()，对 tableSet（裸名 ∪ owner 限定名并集）
    直接比对 —— 与 schema_introspection_service._validateOntologyAgainstSchema
    同口径（裸表名与 owner 限定名写法都命中，大小写不敏感）；
    source_table 为空的类保留（无法判定，不误杀）。
    返回新列表，不改动入参。
    """
    kept: list[Any] = []
    dropped: list[str] = []
    for cls in classes:
        table = cls.source_table
        if not table:
            kept.append(cls)
            continue
        if table.upper() in tableSet:
            kept.append(cls)
        else:
            dropped.append(table)
    return kept, dropped


async def applyDatasourceRecallFilter(
    session: AsyncSession, classes: list[Any], *, datasourceId: int | None
) -> tuple[list[Any], dict[str, Any]]:
    """三态入口：返回 (过滤后的候选类, ClassRecallInfo 披露字段 dict)。

    dict 恒含 datasourceFiltered / datasourceFilterSkipped 两键，调用方用
    recallInfo.model_copy(update=...) 合并进召回诊断。
    """
    disclosure: dict[str, Any] = {
        "datasourceFiltered": 0,
        "datasourceFilterSkipped": None,
    }
    if datasourceId is None:
        disclosure["datasourceFilterSkipped"] = "no_datasource"
        return list(classes), disclosure
    tableSet = await loadDatasourceTableSet(session, datasourceId)
    if tableSet is None:
        disclosure["datasourceFilterSkipped"] = "no_cache"
        return list(classes), disclosure
    kept, dropped = filterClassesByDatasource(classes, tableSet)
    if dropped:
        logger.warning(
            "召回候选跨源过滤 dropped=%d total=%d droppedTables=%s datasourceId=%s",
            len(dropped), len(classes), dropped[:10], datasourceId,
        )
    disclosure["datasourceFiltered"] = len(dropped)
    return kept, disclosure


def isRecallDatasourceScoped(disclosure: dict[str, Any]) -> bool:
    """披露里「跨源过滤已生效」⇒ 候选集已按当前数据源收窄。

    False = 未选数据源（no_datasource）或数据源目录缺失（no_cache）⇒ 候选集
    仍含别源类。**收窄窗口的路径（锚定救援）必须先过这一关**：候选集没按
    数据源收窄时，锚定命中可能全是别源的类，窄窗一旦只含别源表就是必然的
    ORA-00942（比宽窗更糟）。调用方直接传 ``applyDatasourceRecallFilter``
    返回的披露 dict，避免各调用点各写一遍判定。
    """
    return disclosure.get("datasourceFilterSkipped") is None
