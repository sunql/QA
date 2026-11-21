"""A6：``property_alias`` 落库写保护（真 PostgreSQL + 真 ORM 事件）。

**为什么必须真库**：本批最危险的一条保证是「绝不覆盖已有人工 alias」
（ASSET 域 8 条是别人的资产）。它物理上只存在于 UPDATE 的
``property_alias IS NULL OR btrim(...) = ''`` 谓词里 —— 单测覆盖不到：
把那半行删掉，所有纯函数单测仍然全绿，而 ``--apply`` 会开始改写人工 alias。
本仓位规则要求写路径用真 PostgreSQL（``CLAUDE.md`` §7）。

T15-Q 同类例外登记：回填脚本没有 API 端点（运维脚本），这里直接调
``applyPlans``；用「真 PG + 真 ORM + 每例独立库」替代 HTTP 链路。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyProperty
from app.services.kpi_property_alias_backfill import (
    ACTION_FILL,
    AliasBackfillError,
    AliasPlan,
)
from scripts.backfill_property_alias import applyPlans


async def _seedProperty(
    session: AsyncSession,
    *,
    className: str,
    column: str,
    alias: str | None,
    validTo: datetime | None = None,
    version: int = 1,
) -> int:
    ontologyClass = OntologyClass(
        class_name=className,
        class_alias=className,
        source_table=className,
        created_by="test",
        version=version,
        valid_to=validTo,
    )
    session.add(ontologyClass)
    await session.flush()
    session.add(
        OntologyProperty(
            class_id=ontologyClass.id,
            property_name=column,
            source_column=column,
            property_alias=alias,
            data_type="VARCHAR",
        )
    )
    await session.commit()
    return int(ontologyClass.id)


def _plan(classId: int, className: str, column: str, alias: str) -> AliasPlan:
    return AliasPlan(
        class_name=className,
        source_column=column,
        alias=alias,
        current_alias=None,
        action=ACTION_FILL,
        class_id=classId,
    )


async def _aliasOf(session: AsyncSession, classId: int, column: str) -> str | None:
    return (
        await session.execute(
            select(OntologyProperty.property_alias)
            .where(OntologyProperty.class_id == classId)
            .where(OntologyProperty.source_column == column)
        )
    ).scalar_one_or_none()


async def test_人工alias不被覆盖且整体回滚(dbSession: AsyncSession) -> None:
    classId = await _seedProperty(
        dbSession, className="T_MANUAL_ALIAS", column="COL", alias="人工名"
    )

    # 计划来自写库前的旧快照（当时 alias 为空）—— 落库时必须被 SQL 谓词挡住。
    with pytest.raises(AliasBackfillError):
        await applyPlans(
            dbSession, [_plan(classId, "T_MANUAL_ALIAS", "COL", "自动名")]
        )

    assert await _aliasOf(dbSession, classId, "COL") == "人工名"


async def test_空alias被填充并读回复核(dbSession: AsyncSession) -> None:
    classId = await _seedProperty(
        dbSession, className="T_EMPTY_ALIAS", column="COL", alias="   "
    )

    count = await applyPlans(
        dbSession, [_plan(classId, "T_EMPTY_ALIAS", "COL", "收货日期")]
    )

    assert count == 1
    assert await _aliasOf(dbSession, classId, "COL") == "收货日期"


async def test_墓碑类同名属性不会被写(dbSession: AsyncSession) -> None:
    """同名类可有多行（墓碑 + 有效）；定位键是 class_id，不是 class_name。"""
    tombstoneId = await _seedProperty(
        dbSession,
        className="T_VERSIONED",
        column="COL",
        alias=None,
        validTo=datetime.now(UTC),
    )
    liveId = await _seedProperty(
        dbSession, className="T_VERSIONED", column="COL", alias=None, version=2
    )

    count = await applyPlans(dbSession, [_plan(liveId, "T_VERSIONED", "COL", "别名")])

    assert count == 1
    assert await _aliasOf(dbSession, liveId, "COL") == "别名"
    assert await _aliasOf(dbSession, tombstoneId, "COL") is None
