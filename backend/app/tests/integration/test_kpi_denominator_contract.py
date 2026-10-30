"""B4：分母契约弱校验装配器集成测试（真实 PG）。

`buildKpiDenominatorWarning(session, question, sql)`：
matchAll（Top-3，同 B3 匹配口径）→ 第一个 denominator 非空的命中 KPI →
`denominatorContractWarning` 弱校验（声明列 ⊆ SQL 分母列，示警不硬拒）。

零回归契约：无匹配 / denominator 空 / 中文声明判不了 / 任何异常 → None。

匹配路径：单 KPI 用例走 exact-alias 快车道（问句含 KPI code，confidence=1.0，
同 test_chat_l1_routing / test_kpi_contract_injection 约定）；seed 后必须
`onKpiChanged()+warmUp` 整库预热（refreshOne 冷缓存是 no-op——B3 已钉死的坑）。
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_contract_reconcile import buildKpiDenominatorWarning

pytestmark = pytest.mark.asyncio

_SQL_BAD_DENOM = "SELECT SUM(received_qty)/SUM(late_qty) AS rate FROM t"
_SQL_GOOD_DENOM = "SELECT SUM(received_qty)/SUM(order_qty) AS rate FROM t"


async def _seedKpi(
    session: AsyncSession,
    code: str,
    *,
    denominator: str | None = None,
    status: str = "PUBLISHED",
) -> KpiCatalog:
    from decimal import Decimal

    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=code,
        status=status,
        denominator=denominator,
        match_threshold=Decimal("0.75"),
    )
    session.add(kpi)
    await session.commit()
    return kpi


async def _warmCache(session: AsyncSession) -> None:
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(session)


async def test_mismatched_denominator_warns(dbSession: AsyncSession) -> None:
    """声明列不在 SQL 分母 → 示警文本含声明列。"""
    await _seedKpi(dbSession, "KPI_B4_01", denominator="order_qty")
    await _warmCache(dbSession)

    warning = await buildKpiDenominatorWarning(
        dbSession, "KPI_B4_01 是多少", _SQL_BAD_DENOM
    )

    assert warning is not None
    assert "order_qty" in warning


async def test_compatible_denominator_no_warning(dbSession: AsyncSession) -> None:
    """SQL 分母包含声明列 → None。"""
    await _seedKpi(dbSession, "KPI_B4_02", denominator="order_qty")
    await _warmCache(dbSession)
    assert (
        await buildKpiDenominatorWarning(dbSession, "KPI_B4_02 是多少", _SQL_GOOD_DENOM)
        is None
    )


async def test_chinese_denominator_cannot_judge(dbSession: AsyncSession) -> None:
    """中文声明抽不出列名 → 判不了 → None（不谎报不一致）。"""
    await _seedKpi(dbSession, "KPI_B4_03", denominator="总到货行数")
    await _warmCache(dbSession)
    assert (
        await buildKpiDenominatorWarning(dbSession, "KPI_B4_03 是多少", _SQL_BAD_DENOM)
        is None
    )


async def test_kpi_without_denominator_skips(dbSession: AsyncSession) -> None:
    """denominator 空（B5 DRAFT 户头形态）→ None。"""
    await _seedKpi(dbSession, "KPI_B4_04", denominator=None)
    await _warmCache(dbSession)
    assert (
        await buildKpiDenominatorWarning(dbSession, "KPI_B4_04 是多少", _SQL_BAD_DENOM)
        is None
    )


async def test_no_match_returns_none(dbSession: AsyncSession) -> None:
    """问句无 KPI 命中 → None。"""
    await _seedKpi(dbSession, "KPI_B4_05", denominator="order_qty")
    await _warmCache(dbSession)
    assert (
        await buildKpiDenominatorWarning(dbSession, "今天天气如何", _SQL_BAD_DENOM)
        is None
    )


async def test_empty_question_or_sql_returns_none(dbSession: AsyncSession) -> None:
    """空问句 / 空 SQL 防御 → None。"""
    await _seedKpi(dbSession, "KPI_B4_06", denominator="order_qty")
    await _warmCache(dbSession)
    assert await buildKpiDenominatorWarning(dbSession, "", _SQL_BAD_DENOM) is None
    assert await buildKpiDenominatorWarning(dbSession, "  ", _SQL_BAD_DENOM) is None
    assert await buildKpiDenominatorWarning(dbSession, "KPI_B4_06", None) is None
