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

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.v1.kpi_diagnose as kpiDiagnoseModule
from app.domain.models import (
    AuditLog,
    KpiCatalog,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
    SessionMessage,
)
from app.models.system_config import SystemConfig
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_audit import RECONCILE_ENTITY_TYPE
from app.services.kpi_metric_switches import (
    SWITCH_CONTRACT_INJECTION,
    SWITCH_GATE,
)
from app.services.kpi_runtime_audit import RUNTIME_ENTITY_TYPE

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
    session: AsyncSession, *, name: str, source: str = "ESCAPE_HATCH"
) -> OntologyMetric:
    """RATIO + 两侧 measure + formula 列覆盖 ⇒ 层 1/2/3 均可 pass。

    P3 D3 起层 2 会解析治理 token 并比较时间粒度：``time_grain`` 按回填后
    等价状态种入（与夹具 KPI 的 ``grain`` 文本推导一致）。
    """
    metric = OntologyMetric(
        metric_name=name,
        formula=_FORMULA,
        agg_function="SUM",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        execution_source=source,
        numerator_measure={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        denominator_measure={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        time_grain="MONTH",
        created_by="d2-test",
    )
    session.add(metric)
    await session.commit()
    return metric


async def _seedSourceProperties(
    session: AsyncSession,
    metric: OntologyMetric,
    mapping: dict[str, str],
) -> None:
    """种一个源类：``property_name`` → ``source_column``，并挂到 metric。

    P3 D3 的层 2 用 ``ontology_metric_source`` 找源类属性做别名解析；无源类
    会按 R2 判 unknown。夹具按回填后等价状态补齐，保留各用例原意图。
    """
    ontologyClass = OntologyClass(
        class_name=f"{metric.metric_name}-source",
        version=1,
        created_by="d2-test",
    )
    session.add(ontologyClass)
    await session.flush()
    for propertyName, sourceColumn in mapping.items():
        session.add(
            OntologyProperty(
                class_id=ontologyClass.id,
                property_name=propertyName,
                source_column=sourceColumn,
                data_type="DATE" if sourceColumn.endswith("_DATE") else "VARCHAR",
            )
        )
    session.add(
        OntologyMetricSource(
            metric_id=metric.id,
            class_id=ontologyClass.id,
            role="NUMERATOR",
        )
    )
    await session.commit()


# 标准 pass 夹具的治理 token 可直接按物理列解析。
_PASS_SOURCE_MAPPING = {
    "RECEIPT_DATE": "RECEIPT_DATE",
    "PROMISED_RECEIPT_DATE": "PROMISED_RECEIPT_DATE",
    "SUPPLIER_CODE": "SUPPLIER_CODE",
}


async def _seedKpi(
    session: AsyncSession,
    code: str,
    *,
    metricId: int | None,
    numeratorColumns: list[str],
    denominatorColumns: list[str],
    grainKeys: list[str],
    keywords: list[str] | None = None,
    grain: str = "供应商+月",
) -> KpiCatalog:
    kpi = KpiCatalog(
        kpi_code=code,
        kpi_name=f"{code} 名称",
        status="PUBLISHED",
        metric_id=metricId,
        numerator_columns=numeratorColumns,
        denominator_columns=denominatorColumns,
        grain_keys=grainKeys,
        grain=grain,
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
    await _seedSourceProperties(session, metric, _PASS_SOURCE_MAPPING)
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
    # 源类把治理业务词解析到技术 measure 里不存在的物理列 ⇒ 保留原意图：
    # 必须是 ``governance_column_not_in_technical`` 的 mismatch（不是无源类 unknown）。
    await _seedSourceProperties(
        dbSession,
        metric,
        {
            "按期到货行数": "BIZ_RECEIPT_DATE",
            "总到货行数": "BIZ_PROMISED_DATE",
            "SUPPLIER_CODE": "SUPPLIER_CODE",
        },
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


async def test_diagnose_none_source_reports_tier_c(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """NONE 源即便层 2 pass 也只能是 Tier C（枚举契约：NONE 不允许 Tier A 直通）。"""
    await _enableInjectionSwitch(dbSession)
    metric = OntologyMetric(
        metric_name="KPI_D2_NONE-metric",
        formula="",
        agg_function="SUM",
        metric_type="MEASURE",
        execution_source="NONE",
        numerator_measure={"agg": "SUM", "expr": "RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        time_grain="MONTH",
        created_by="d2-test",
    )
    dbSession.add(metric)
    await dbSession.commit()
    await _seedKpi(
        dbSession,
        "KPI_D2_NONE",
        metricId=metric.id,
        numeratorColumns=["RECEIPT_DATE"],
        denominatorColumns=[],
        grainKeys=["SUPPLIER_CODE"],
        keywords=["NONE源诊断"],
    )
    await _seedSourceProperties(dbSession, metric, _PASS_SOURCE_MAPPING)
    await _warmCache(dbSession)

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_D2_NONE 是多少"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    match = response.json()["matches"][0]
    # 层 2 判 pass，但 NONE 没有可执行形态 ⇒ 不得报 A / 不得报可直通。
    assert match["verdict"] == "pass"
    assert match["tier"] == "C"
    assert match["tierAEligible"] is False


async def test_diagnose_compiled_source_reports_tier_a_eligible(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """COMPILED 与运行时同口径：对账可直通 ⇒ tier=A、tierAEligible=True。"""
    await _enableInjectionSwitch(dbSession)
    metric = await _seedMetric(
        dbSession, name="KPI_D2_COMPILED-metric", source="COMPILED"
    )
    await _seedKpi(
        dbSession,
        "KPI_D2_COMPILED",
        metricId=metric.id,
        numeratorColumns=["RECEIPT_DATE"],
        denominatorColumns=["PROMISED_RECEIPT_DATE"],
        grainKeys=["SUPPLIER_CODE"],
        keywords=["编译源诊断"],
    )
    await _seedSourceProperties(dbSession, metric, _PASS_SOURCE_MAPPING)
    await _warmCache(dbSession)

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_D2_COMPILED 是多少"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    match = response.json()["matches"][0]
    assert match["executionSource"] == "COMPILED"
    assert match["verdict"] == "pass"
    assert match["tier"] == "A"
    assert match["tierAEligible"] is True


async def test_diagnose_match_failure_is_not_swallowed(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """匹配故障必须向上抛（500），不能伪装成「无命中」的 200。"""

    class _BoomService:
        async def matchAll(self, question: str, limit: int):
            raise RuntimeError("cache/db down")

    monkeypatch.setattr(
        kpiDiagnoseModule, "getKpiSemanticMatchService", lambda: _BoomService()
    )

    with pytest.raises(RuntimeError, match="cache/db down"):
        await client.get(
            _DIAGNOSE_URL,
            params={"question": "任意问句"},
            headers=_ADMIN_HEADERS,
        )


async def test_diagnose_injected_follows_block_membership(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """pass 门禁 ≠ 一定注入：条目被块上限丢掉时 ``injected`` 必须为 false。"""
    await _enableInjectionSwitch(dbSession)
    await _seedPassingKpi(dbSession, "KPI_D2_NOT_IN_BLOCK")
    await _warmCache(dbSession)

    async def _emptyBlock(session, question, **kwargs):
        return "【KPI 口径契约】占位", ()

    monkeypatch.setattr(
        kpiDiagnoseModule, "buildKpiContractBlockWithCodes", _emptyBlock
    )

    response = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_D2_NOT_IN_BLOCK 是多少"},
        headers=_ADMIN_HEADERS,
    )

    assert response.status_code == 200, response.text
    match = response.json()["matches"][0]
    assert match["verdict"] == "pass"
    assert match["injected"] is False


async def test_diagnose_reports_ambiguous_and_builds_no_block(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """v2 开启 + 等长跨 KPI ⇒ ambiguous=true，不建契约块（不替用户选指标）。"""
    from app.services.kpi_metric_switches import SWITCH_MATCH_CONTAINMENT

    await _enableInjectionSwitch(dbSession)
    dbSession.add(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    await dbSession.commit()
    await _seedKpi(dbSession, "KPI_P1_DIAG_A", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["交付率"])
    await _seedKpi(dbSession, "KPI_P1_DIAG_B", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["逾期率"])
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "交付率和逾期率分别是多少"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ambiguous"] is True
    assert sorted(m["kpiCode"] for m in body["matches"]) == [
        "KPI_P1_DIAG_A",
        "KPI_P1_DIAG_B",
    ]
    assert body["injectionBlockEmpty"] is True


async def test_diagnose_containment_hit_reports_method(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """唯一包含式命中 ⇒ matchMethod=containment，与 chat 同一路径。"""
    from app.services.kpi_metric_switches import SWITCH_MATCH_CONTAINMENT

    await _enableInjectionSwitch(dbSession)
    dbSession.add(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    await dbSession.commit()
    await _seedKpi(dbSession, "KPI_P1_DIAG_C", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["采购周期"])
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL, params={"question": "采购周期多长"}, headers=_ADMIN_HEADERS
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ambiguous"] is False
    assert [m["kpiCode"] for m in body["matches"]] == ["KPI_P1_DIAG_C"]
    assert body["matches"][0]["matchMethod"] == "containment"


async def test_diagnose_legacy_when_switch_off(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """灰度关闭 ⇒ 永不澄清（生产当前行为可回退）。"""
    await _seedKpi(dbSession, "KPI_P1_DIAG_D", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["交付率"])
    await _seedKpi(dbSession, "KPI_P1_DIAG_E", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["逾期率"])
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "交付率和逾期率分别是多少"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ambiguous"] is False


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


# ---------------------------------------------------------------------------
# Task 7：同步健康快照（syncHealth）+ distance 字段
#
# ⚠️ 同步健康会读 Milvus / Neo4j ⇒ 本文件**禁止**连真实例（19530 / 7687）。
# 下面的 autouse 夹具把两个读取点换成假替身；用例内可用 monkeypatch 覆盖。
# ---------------------------------------------------------------------------


class _FakeSyncHealthNeo4j:
    """假 Neo4j（图节点计数）。"""

    ids: set[str] = set()

    @staticmethod
    def getKpiCatalogIds() -> set[str]:
        return set(_FakeSyncHealthNeo4j.ids)


@pytest.fixture(autouse=True)
def fakeKpiSyncHealthStores(monkeypatch: pytest.MonkeyPatch) -> None:
    """把诊断接口的 Milvus / Neo4j 读取点换成假替身（禁连真实例）。"""
    _FakeSyncHealthNeo4j.ids = set()
    monkeypatch.setattr(kpiDiagnoseModule, "queryKpiEmbeddings", lambda: [])
    monkeypatch.setattr(kpiDiagnoseModule, "neo4j", _FakeSyncHealthNeo4j)


async def test_diagnose_reports_sync_health(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步健康：三计数齐全 + errors 为空。"""
    await _seedKpi(dbSession, "KPI_D2_HEALTH", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["健康"])
    await _warmCache(dbSession)

    monkeypatch.setattr(
        kpiDiagnoseModule,
        "queryKpiEmbeddings",
        lambda: [{"ontology_id": 1}, {"ontology_id": 2}, {"ontology_id": 3}],
    )
    _FakeSyncHealthNeo4j.ids = {"obj:KPI:1", "obj:KPI:2"}

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "健康"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    health = resp.json()["syncHealth"]
    assert health is not None
    assert health["kpiPublishedPgCount"] == 1
    assert health["kpiVectorCount"] == 3
    assert health["kpiGraphNodeCount"] == 2
    assert health["errors"] == []


async def test_diagnose_sync_health_records_store_errors(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Milvus / Neo4j 取数失败 ⇒ 仍 200，错误进 errors，计数置 None。"""

    def _boomMilvus() -> list[dict]:
        raise RuntimeError("milvus 挂了")

    def _boomNeo4j() -> set[str]:
        raise RuntimeError("neo4j 挂了")

    monkeypatch.setattr(kpiDiagnoseModule, "queryKpiEmbeddings", _boomMilvus)
    monkeypatch.setattr(kpiDiagnoseModule, "neo4j", type("_N", (), {
        "getKpiCatalogIds": staticmethod(_boomNeo4j),
    }))

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "今天天气怎么样"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    health = resp.json()["syncHealth"]
    assert health is not None
    assert health["kpiVectorCount"] is None
    assert health["kpiGraphNodeCount"] is None
    assert len(health["errors"]) == 2
    assert any(e.startswith("milvus: ") for e in health["errors"])
    assert any(e.startswith("neo4j: ") for e in health["errors"])
    assert any("milvus 挂了" in e for e in health["errors"])
    assert any("neo4j 挂了" in e for e in health["errors"])


async def test_diagnose_match_exposes_distance_field(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """matches[].distance 字段存在（L1 不产距离 ⇒ None，不报错）。"""
    await _seedKpi(dbSession, "KPI_D2_DIST", metricId=None,
                   numeratorColumns=[], denominatorColumns=[], grainKeys=[],
                   keywords=["距离"])
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "距离"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["distance"] is None


# ---------------------------------------------------------------------------
# Task 5：治理看板聚合 API（admin-only，best-effort 计数）
#
# 形状契约见设计稿 §2.3。同步健康段复用 `_kpiSyncHealth` ⇒ 本文件顶部的
# fakeKpiSyncHealthStores autouse 夹具同样兜住 Milvus / Neo4j，禁止连真实例。
# ---------------------------------------------------------------------------

_GOVERNANCE_URL = "/api/v1/admin/kpi/governance-summary"


async def test_governance_summary_admin_only(client: AsyncClient) -> None:
    """非 admin ⇒ 403 + 结构化 PERMISSION_DENIED（不是 404 也不是 200）。"""
    resp = await client.get(
        _GOVERNANCE_URL,
        headers={"X-User-Roles": "user", "X-User-Id": "alice"},
    )
    assert resp.status_code == 403
    assert resp.json()["detail"]["code"] == "PERMISSION_DENIED"


async def test_governance_summary_shape(
    client: AsyncClient,
    dbSession: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """种 KPI/metric/审计/开关双面数据 ⇒ 形状、分桶、窗口、闸门、映射都可读。"""
    compiledMetric = await _seedMetric(
        dbSession, name="KPI_GOV_COMPILED-metric", source="COMPILED"
    )
    await _seedKpi(
        dbSession,
        "KPI_GOV_PUBLISHED",
        metricId=compiledMetric.id,
        numeratorColumns=["RECEIPT_DATE"],
        denominatorColumns=["PROMISED_RECEIPT_DATE"],
        grainKeys=["SUPPLIER_CODE"],
    )
    dbSession.add_all(
        [
            KpiCatalog(
                kpi_code="KPI_GOV_DRAFT",
                kpi_name="KPI_GOV_DRAFT 名称",
                status="DRAFT",
                metric_id=None,
                numerator_columns=[],
                denominator_columns=[],
                grain_keys=[],
                semantic_keywords=[],
                match_threshold=Decimal("0.75"),
            ),
            # execution_source 缺失的两种形态：NULL 与空串，都必须落 NONE 桶。
            OntologyMetric(
                metric_name="KPI_GOV_NULL_SOURCE-metric",
                formula="",
                agg_function="SUM",
                metric_type="MEASURE",
                created_by="gov-test",
            ),
            OntologyMetric(
                metric_name="KPI_GOV_EMPTY_SOURCE-metric",
                formula="",
                agg_function="SUM",
                metric_type="MEASURE",
                execution_source="",
                created_by="gov-test",
            ),
            # 发布闸门打开；Tier A / sync 不种行 ⇒ 保持默认（False / True）。
            SystemConfig(key=SWITCH_GATE, value="true"),
        ]
    )
    # 对账审计只写 findings 非空的行：干净 pass 不落库，但 degraded pass
    # （层 1/3 mismatch + 层 2 pass）会以 verdict=pass 落库。这里直接种行
    # 只为验证 JSONB `after_json->>'verdict'` 的计数口径。
    # ⇒ `passCount` 只是「实际落库的 pass 行数」——只有 degraded pass 会落库，**不是通过率**。
    now = datetime.now(UTC)
    dbSession.add_all(
        [
            AuditLog(
                entity_type=RECONCILE_ENTITY_TYPE,
                entity_id=compiledMetric.id,
                action="CREATE",
                actor="system:kpi-contract",
                after_json={
                    "kpi_code": "KPI_GOV_PUBLISHED",
                    "verdict": "mismatch",
                    "degraded": False,
                    "findings": [],
                },
                created_at=now,
            ),
            AuditLog(
                entity_type=RECONCILE_ENTITY_TYPE,
                entity_id=compiledMetric.id,
                action="CREATE",
                actor="system:kpi-contract",
                after_json={
                    "kpi_code": "KPI_GOV_PUBLISHED",
                    "verdict": "pass",
                    "degraded": False,
                    "findings": [],
                },
                created_at=now - timedelta(days=1),
            ),
            # 窗口外（8 天前）：若丢掉时间谓词，mismatchCount 会 1→2。
            AuditLog(
                entity_type=RECONCILE_ENTITY_TYPE,
                entity_id=compiledMetric.id,
                action="CREATE",
                actor="system:kpi-contract",
                after_json={
                    "kpi_code": "KPI_GOV_OLD",
                    "verdict": "mismatch",
                    "degraded": False,
                    "findings": [],
                },
                created_at=now - timedelta(days=8),
            ),
            # 非对账 entity_type：若丢掉 entity_type 谓词，mismatchCount 1→2。
            AuditLog(
                entity_type="kpi_catalog",
                entity_id=compiledMetric.id,
                action="CREATE",
                actor="test",
                after_json={"verdict": "mismatch"},
                created_at=now,
            ),
        ]
    )
    await dbSession.commit()

    # 两个假存储返回不同计数：交换 milvus/neo4j 映射会立刻对不上。
    monkeypatch.setattr(
        kpiDiagnoseModule,
        "queryKpiEmbeddings",
        lambda: [{"ontology_id": 1}, {"ontology_id": 2}, {"ontology_id": 3}],
    )
    _FakeSyncHealthNeo4j.ids = {"obj:KPI:1", "obj:KPI:2"}

    resp = await client.get(_GOVERNANCE_URL, headers=_ADMIN_HEADERS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kpis"]["total"] >= 2
    assert body["kpis"]["byStatus"]["PUBLISHED"] >= 1
    assert body["kpis"]["byStatus"]["DRAFT"] >= 1
    assert body["metrics"]["byExecutionSource"]["COMPILED"] >= 1
    # NULL / 空串 metric 都落 NONE，且响应里不得出现空串键。
    assert body["metrics"]["byExecutionSource"]["NONE"] >= 2
    assert "" not in body["metrics"]["byExecutionSource"]
    assert "metrics" in body and "reconcile" in body and "gates" in body
    assert body["reconcile"]["windowDays"] == 7
    # 仅窗口内 + 对账 entity_type 的 1 mismatch + 1 pass 被计数；
    # 窗口外行与非对账行都被排除（见上方两条变异说明）。
    assert body["reconcile"]["mismatchCount"] == 1
    assert body["reconcile"]["passCount"] == 1
    assert body["reconcile"]["unknownCount"] == 0
    assert body["reconcile"]["lastMismatchAt"] is not None
    assert body["reconcile"]["reconcileError"] is None
    # 只开了发布闸门：Tier A 缺行 ⇒ False；sync 在测试环境被 conftest 的
    # `KPI_SYNC_ENABLED=false` 一键止血置 False（生产缺行默认 True）。
    assert body["gates"]["publishGateEnabled"] is True
    assert body["gates"]["tierAEnabled"] is False
    assert body["gates"]["syncEnabled"] is False
    # 同步健康复用 `_kpiSyncHealth`：PG 计数取真值，Milvus=3 / Neo4j=2。
    assert body["syncHealth"]["publishedKpiCount"] == 1
    assert body["syncHealth"]["milvusKpiCount"] == 3
    assert body["syncHealth"]["neo4jKpiCount"] == 2
    assert body["syncHealth"]["kpiTotal"] == body["kpis"]["total"]
    assert body["syncHealth"]["syncHealthError"] is None
    assert body["generatedAt"]


async def test_governance_summary_sync_health_degrades(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同步健康整段抛错 ⇒ 仍 200，三计数 null + syncHealthError（不 500）。

    reconcile 的 best-effort 分支不另立用例：两段用的是同一 savepoint 隔离
    模式（各自 `begin_nested()` + 降级返回），这里只钉住用户可见的降级契约。
    """

    async def _boom(session: AsyncSession) -> object:
        raise RuntimeError("sync store down")

    monkeypatch.setattr(kpiDiagnoseModule, "_kpiSyncHealth", _boom)

    resp = await client.get(_GOVERNANCE_URL, headers=_ADMIN_HEADERS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    health = body["syncHealth"]
    assert health["publishedKpiCount"] is None
    assert health["milvusKpiCount"] is None
    assert health["neo4jKpiCount"] is None
    assert health["kpiTotal"] == body["kpis"]["total"]
    assert health["syncHealthError"] is not None
    assert "sync store down" in health["syncHealthError"]
    # 同步健康失败不应影响其余聚合段。
    assert "reconcile" in body and "gates" in body


# ---------------------------------------------------------------------------
# M4：运行时 Tier 分布 / 降级原因 / 近似命中率
# ---------------------------------------------------------------------------

def _runtimeEvent(
    tier: str,
    reason: str | None,
    createdAt: datetime,
    *,
    entityType: str = RUNTIME_ENTITY_TYPE,
) -> AuditLog:
    return AuditLog(
        entity_type=entityType,
        entity_id=1,
        action="CREATE",
        actor="system:kpi-runtime",
        after_json={
            "kpi_code": "KPI_RT",
            "tier": tier,
            "reason": reason,
            "match_method": "exact_code",
        },
        created_at=createdAt,
    )


async def test_governance_summary_runtime_tiers(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """运行时事件 ⇒ byTier / 降级原因 Top-N / 近似命中率；窗口与非本类事件均被排除。"""
    now = datetime.now(UTC)
    dbSession.add_all(
        [
            # Tier A 2 条、Tier B 3 条（同一降级原因）、C 1 条、LEGACY 4 条，
            # 外加 1 条 tier 为空串的脏行 ⇒ 归 UNKNOWN 桶（不得静默丢弃）。
            _runtimeEvent("A", None, now),
            _runtimeEvent("A", None, now - timedelta(days=1)),
            _runtimeEvent("", None, now),
            *[
                _runtimeEvent("B", "tier_a_execution_error", now - timedelta(hours=idx))
                for idx in range(3)
            ],
            _runtimeEvent("C", "tier_c_direct", now),
            _runtimeEvent("LEGACY", "reason_1", now),
            _runtimeEvent("LEGACY", "reason_2", now),
            _runtimeEvent("LEGACY", "reason_3", now),
            _runtimeEvent("LEGACY", "reason_4", now),
            # 窗口外（8 天前）：若丢掉时间谓词，A 计数 2→3、total 11→12。
            _runtimeEvent("A", None, now - timedelta(days=8)),
            # 其他 entity_type：若丢掉过滤，total 11→12。
            _runtimeEvent("A", None, now, entityType="kpi_metric_reconcile"),
        ]
    )
    # 近 7 天 chat 助手回答 2 条：非 assistant / 非 chat / 窗口外各 1 条须排除。
    dbSession.add_all(
        [
            SessionMessage(
                session_id="sess_rt", role="assistant", content="a",
                channel="chat", created_time=now,
            ),
            SessionMessage(
                session_id="sess_rt", role="assistant", content="a",
                channel="chat", created_time=now - timedelta(days=1),
            ),
            SessionMessage(
                session_id="sess_rt", role="assistant", content="a",
                channel="doc_qa", created_time=now,
            ),
            SessionMessage(
                session_id="sess_rt", role="user", content="q",
                channel="chat", created_time=now,
            ),
            SessionMessage(
                session_id="sess_rt", role="assistant", content="a",
                channel="chat", created_time=now - timedelta(days=8),
            ),
        ]
    )
    await dbSession.commit()

    resp = await client.get(_GOVERNANCE_URL, headers=_ADMIN_HEADERS)

    assert resp.status_code == 200, resp.text
    runtime = resp.json()["runtime"]
    assert runtime["windowDays"] == 7
    assert runtime["total"] == 11
    assert runtime["byTier"] == {"A": 2, "B": 3, "C": 1, "LEGACY": 4, "UNKNOWN": 1}
    # Top-N=5：最高频原因在前，同频按字母序；6 个不同原因 ⇒ tier_c_direct 被截断。
    assert runtime["degradedReasons"][0] == {
        "reason": "tier_a_execution_error",
        "count": 3,
    }
    assert [item["reason"] for item in runtime["degradedReasons"]] == [
        "tier_a_execution_error",
        "reason_1",
        "reason_2",
        "reason_3",
        "reason_4",
    ]
    assert runtime["chatAnswers"] == 2
    assert runtime["hitRate"] == 5.5
    assert runtime["runtimeError"] is None


async def test_governance_summary_runtime_hit_rate_null_without_answers(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """窗口内无 chat 回答 ⇒ hitRate 为 null（不是 0、不除零）。"""
    now = datetime.now(UTC)
    dbSession.add(_runtimeEvent("A", None, now))
    await dbSession.commit()

    resp = await client.get(_GOVERNANCE_URL, headers=_ADMIN_HEADERS)

    assert resp.status_code == 200, resp.text
    runtime = resp.json()["runtime"]
    assert runtime["total"] == 1
    assert runtime["chatAnswers"] == 0
    assert runtime["hitRate"] is None


async def test_governance_summary_runtime_degrades(
    dbSession: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行时统计查询抛错 ⇒ 全 0 + runtimeError（best-effort，事务不被污染）。"""

    class _Exploding:
        def __getattr__(self, name: str) -> object:
            raise RuntimeError("runtime store down")

    monkeypatch.setattr(kpiDiagnoseModule, "AuditLog", _Exploding())

    runtime = await kpiDiagnoseModule._governanceRuntime(dbSession)

    assert runtime.total == 0
    assert runtime.by_tier == {}
    assert runtime.degraded_reasons == []
    assert runtime.chat_answers == 0
    assert runtime.hit_rate is None
    assert runtime.runtime_error is not None
    assert "runtime store down" in runtime.runtime_error
    # savepoint 已回滚：调用方事务仍可继续使用。
    row = await dbSession.execute(text("SELECT 1"))
    assert row.scalar_one() == 1
