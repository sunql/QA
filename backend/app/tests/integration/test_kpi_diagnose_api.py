"""P0 D2-2：KPI 诊断接口（admin）集成测试。

真实 PG + 完整 HTTP 链路。诊断接口把「为什么没注入」摊开给管理员看：
命中路径（exact_code / keyword）/ 对账 verdict / 是否降级 / Tier /
findings 原因码 / 是否注入 / 注入总闸状态。

RED 覆盖 6 类：
1. pass + 注入
2. 治理列写业务名 ⇒ mismatch（原因码 ``governance_column_not_in_technical``）⇒ 不注入
3. 非 admin ⇒ 403
4. 空 question ⇒ 422
5. 注入总闸关闭 ⇒ ``injectionEnabled=false`` / 块为空
6. 无匹配问句 ⇒ 200 且 ``matches=[]``
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_switches import SWITCH_CONTRACT_INJECTION

pytestmark = pytest.mark.asyncio

_DIAGNOSE_URL = "/api/v1/admin/kpi/diagnose"
_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}
_USER_HEADERS = {"X-User-Id": "test-user", "X-User-Roles": "user"}

# 层 1/3 都能对上的公式：结构化列 RECEIPT_DATE / PROMISED_RECEIPT_DATE 均在。
_FORMULA = (
    "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END) "
    "/ NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) "
    "FROM THBI.DWD_PURCHASE_ORDER_LINE O "
    "JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO"
)


async def _enableInjectionSwitch(session: AsyncSession) -> None:
    session.add(SystemConfig(key=SWITCH_CONTRACT_INJECTION, value="true"))
    await session.commit()


async def _seedMetric(
    session: AsyncSession, *, name: str
) -> OntologyMetric:
    """RATIO + 两侧 measure + formula 列覆盖 ⇒ 层 1/2/3 均可 pass。"""
    metric = OntologyMetric(
        metric_name=name,
        formula=_FORMULA,
        agg_function="SUM",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        execution_source="ESCAPE_HATCH",
        numerator_measure={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        denominator_measure={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        created_by="d2-test",
    )
    session.add(metric)
    await session.commit()
    return metric


async def _seedKpi(
    session: AsyncSession,
    code: str,
    *,
    metricId: int | None,
    numeratorColumns: list[str],
    denominatorColumns: list[str],
    grainKeys: list[str],
    keywords: list[str] | None = None,
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=f"{code} 名称",
        status="PUBLISHED",
        metric_id=metricId,
        numerator_columns=numeratorColumns,
        denominator_columns=denominatorColumns,
        grain_keys=grainKeys,
        semantic_keywords=keywords or [],
        match_threshold=Decimal("0.75"),
    )
    session.add(kpi)
    await session.commit()
    return kpi


async def _warmCache(session: AsyncSession) -> None:
    """显式失效 + 预热：全局缓存不随 TRUNCATE 清空，每个用例必须重建。"""
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(session)


async def _seedPassingKpi(
    session: AsyncSession, code: str
) -> tuple[OntologyMetric, KpiCatalog]:
    metric = await _seedMetric(session, name=f"{code}-metric")
    kpi = await _seedKpi(
        session,
        code,
        metricId=metric.id,
        numeratorColumns=["RECEIPT_DATE"],
        denominatorColumns=["PROMISED_RECEIPT_DATE"],
        grainKeys=["SUPPLIER_CODE"],
        keywords=["诊断"],
    )
    return metric, kpi


async def test_diagnose_pass_reports_injection(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """规范化后的数据 ⇒ pass / Tier A / 注入。"""
    await _enableInjectionSwitch(dbSession)
    metric, _kpi = await _seedPassingKpi(dbSession, "KPI_D2_PASS")
    await _warmCache(dbSession)

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_D2_PASS 这个指标是多少"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["question"] == "KPI_D2_PASS 这个指标是多少"
    assert body["injectionEnabled"] is True
    assert body["injectionBlockEmpty"] is False
    assert len(body["matches"]) == 1

    match = body["matches"][0]
    assert match["kpiCode"] == "KPI_D2_PASS"
    assert match["matchMethod"] == "exact_code"
    assert match["metricId"] == metric.id
    assert match["metricType"] == "RATIO"
    assert match["executionSource"] == "ESCAPE_HATCH"
    assert match["verdict"] == "pass"
    assert match["degraded"] is False
    assert match["tierAEligible"] is True
    assert match["tier"] == "A"
    assert match["reasons"] == []
    assert match["findings"] == []
    assert match["injected"] is True


async def test_diagnose_mismatch_reports_reason_and_no_injection(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """治理列写业务名 ⇒ mismatch，原因码可见，且不注入。"""
    await _enableInjectionSwitch(dbSession)
    metric = await _seedMetric(dbSession, name="KPI_D2_MISMATCH-metric")
    await _seedKpi(
        dbSession,
        "KPI_D2_MISMATCH",
        metricId=metric.id,
        numeratorColumns=["按期到货行数"],
        denominatorColumns=["总到货行数"],
        grainKeys=["SUPPLIER_CODE"],
        keywords=["诊断"],
    )
    await _warmCache(dbSession)

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_D2_MISMATCH 是多少"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["injectionBlockEmpty"] is True
    assert len(body["matches"]) == 1

    match = body["matches"][0]
    assert match["verdict"] == "mismatch"
    assert match["tierAEligible"] is False
    assert match["tier"] == "B"
    assert match["injected"] is False
    assert "governance_column_not_in_technical" in match["reasons"]
    assert any(
        finding["layer"] == "governance"
        and finding["reason"] == "governance_column_not_in_technical"
        and finding["verdict"] == "mismatch"
        for finding in match["findings"]
    )


async def test_diagnose_requires_admin(client: AsyncClient) -> None:
    """非 admin ⇒ 403。"""
    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_ANY 是多少"},
        headers=_USER_HEADERS,
    )

    assert response.status_code == 403


async def test_diagnose_rejects_empty_question(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """空 question ⇒ 422（Query min_length=1）。"""
    await _enableInjectionSwitch(dbSession)

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": ""},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 422


async def test_diagnose_reports_switch_off(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """注入总闸关闭 ⇒ injectionEnabled=false 且块为空（命中仍要能看到）。"""
    await _seedPassingKpi(dbSession, "KPI_D2_SWITCH_OFF")
    await _warmCache(dbSession)
    # 不播种 system_config 行：fail-closed ⇒ 开关关闭。

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_D2_SWITCH_OFF 是多少"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["injectionEnabled"] is False
    assert body["injectionBlockEmpty"] is True
    assert len(body["matches"]) == 1
    assert body["matches"][0]["verdict"] == "pass"
    assert body["matches"][0]["injected"] is False


async def test_diagnose_no_match_returns_empty_list(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """无匹配问句 ⇒ 200 + 空列表（不是 404）。"""
    await _enableInjectionSwitch(dbSession)
    metric = await _seedMetric(dbSession, name="KPI_D2_NOMATCH-metric")
    await _seedKpi(
        dbSession,
        "KPI_D2_NOMATCH",
        metricId=metric.id,
        numeratorColumns=["RECEIPT_DATE"],
        denominatorColumns=["PROMISED_RECEIPT_DATE"],
        grainKeys=["SUPPLIER_CODE"],
        keywords=["完全无关的词"],
    )
    await _warmCache(dbSession)

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "今天天气怎么样"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    assert response.json()["matches"] == []
