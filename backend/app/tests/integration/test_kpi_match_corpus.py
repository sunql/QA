"""P1 验收：50 条真实问句回归集（设计 §5.3 / §13 M1 前两条）。

真实 PostgreSQL + 完整 HTTP 链路：seed 12 条 PUBLISHED KPI 与语义关键词，
打开 KPI_MATCH_CONTAINMENT_ENABLED 灰度行，重建缓存，再逐条调 admin 诊断接口
（运行时同一个 matchRoute 决策），断言唯一命中 / 歧义集合 / 零命中。

验收口径：
- 正向 36 条：命中率 ≥ 90%（≥ 33 条）；
- 误命中（负向被命中 + 正向命中错 KPI + 歧义集合不符或未判歧义）合计 ≤ 2 条；
- 4 条真实歧义必须 ambiguous 且候选集合精确等于期望（§5.2「不得直接执行」）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import KpiCatalog, OntologyMetric
from app.models.system_config import SystemConfig
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_switches import (
    SWITCH_CONTRACT_INJECTION,
    SWITCH_MATCH_CONTAINMENT,
)
from scripts.seed_kpi_catalog import seedKpiCatalog
from scripts.seed_kpi_semantic_index import seedKpiSemanticIndex

pytestmark = pytest.mark.asyncio

_DIAGNOSE_URL = "/api/v1/admin/kpi/diagnose"
_ADMIN_HEADERS = {"X-User-Id": "test-admin", "X-User-Roles": "admin"}

_MIN_POSITIVE_HITS = 33   # 36 × 90% 向下取整
_MAX_FALSE_HITS = 2


@dataclass(frozen=True)
class _Case:
    question: str
    expected: tuple[str, ...]  # 唯一命中=1 个 code；歧义=候选全集；负向=()
    kind: str                  # positive | ambiguous | negative


# 12 个 KPI code 常量（与 scripts/seed_kpi_catalog.py 一致）
_OTD = "KPI_SUPPLIER_OTD"
_OVD = "KPI_SUPPLIER_OVERDUE_RATIO"
_CYC = "KPI_PURCHASE_CYCLE_TIME"
_DLY = "KPI_SUPPLIER_DELAY_DAYS"
_DFC = "KPI_SUPPLIER_DEFECT_RATE"
_FPY = "KPI_SUPPLIER_FPY"
_NCR = "KPI_SUPPLIER_NCR_RATE"
_PRV = "KPI_PURCHASE_PRICE_VARIANCE"
_CST = "KPI_COST_SAVING"
_INV = "KPI_INVOICE_MATCH_RATE"
_PAY = "KPI_PAYMENT_ON_TIME"
_MNT = "KPI_SUPPLIER_MAINTENANCE_RATIO"

_CORPUS: tuple[_Case, ...] = (
    # ── 正向 36：唯一命中 ──────────────────────────────────────────────
    _Case("上个月供应商的准时交付率是多少", (_OTD,), "positive"),
    _Case("KPI_SUPPLIER_OTD 这个指标最近三个月怎么样", (_OTD,), "positive"),
    _Case("帮我算一下 OTD", (_OTD,), "positive"),
    _Case("哪些供应商的按时交付做得不好", (_OTD,), "positive"),
    _Case("on time delivery 排名", (_OTD,), "positive"),
    _Case("逾期率最高的五个供应商", (_OVD,), "positive"),
    _Case("本月逾期订单有多少", (_OVD,), "positive"),
    _Case("overdue 订单清单", (_OVD,), "positive"),
    _Case("采购周期平均是多少天", (_CYC,), "positive"),
    _Case("供应商的 lead time 分布", (_CYC,), "positive"),
    _Case("平均延误天数", (_DLY,), "positive"),
    _Case("delay days 是多少", (_DLY,), "positive"),
    _Case("供应商来料不良率趋势", (_DFC,), "positive"),
    _Case("iqc 合格情况", (_DFC,), "positive"),
    _Case("一次合格率", (_FPY,), "positive"),
    _Case("first pass yield 怎么算", (_FPY,), "positive"),
    _Case("NCR 数量", (_NCR,), "positive"),
    _Case("不合格报告的比例", (_NCR,), "positive"),
    _Case("采购价格偏差率", (_PRV,), "positive"),
    _Case("price variance 是多少", (_PRV,), "positive"),
    _Case("今年采购成本节约了多少", (_CST,), "positive"),
    _Case("cost saving 趋势", (_CST,), "positive"),
    _Case("三单匹配率", (_INV,), "positive"),
    _Case("invoice match 失败的原因", (_INV,), "positive"),
    _Case("付款及时率怎么样", (_PAY,), "positive"),
    _Case("账期内的按时付款比例", (_PAY,), "positive"),
    _Case("供应商资质合规率", (_MNT,), "positive"),
    _Case("有多少供应商 certificate 过期", (_MNT,), "positive"),
    _Case("哪些供应商的交付率低", (_OTD,), "positive"),
    _Case("采购交期太长", (_CYC,), "positive"),
    _Case("来料不良的批次数", (_DFC,), "positive"),
    _Case("供应商的缺陷率高不高", (_DFC,), "positive"),
    _Case("逾期天数排行", (_DLY,), "positive"),
    _Case("价格波动的幅度", (_PRV,), "positive"),
    _Case("节支金额", (_CST,), "positive"),
    _Case("供应商合规率", (_MNT,), "positive"),
    # ── 歧义 4：等长跨 KPI，必须反问 ──────────────────────────────────
    _Case("供应商交付率和逾期率分别是多少", (_OTD, _OVD), "ambiguous"),
    _Case("价格偏差和成本节约哪个高", (_PRV, _CST), "ambiguous"),
    _Case("来料不良和来料异常的数量", (_DFC, _NCR), "ambiguous"),
    _Case("交付率、不良率和合规率分别是多少", (_OTD, _DFC, _MNT), "ambiguous"),
    # ── 负向 10：一个都不能命中 ───────────────────────────────────────
    _Case("供应商一共有多少家", (), "negative"),
    _Case("查询本月采购订单数量", (), "negative"),
    _Case("上个月的总采购金额是多少", (), "negative"),
    _Case("物料主数据有多少条", (), "negative"),
    _Case("工厂的产能利用率", (), "negative"),
    _Case("本月新增供应商名单", (), "negative"),
    _Case("订单金额排名前十的供应商", (), "negative"),
    _Case("各工厂的库存周转率", (), "negative"),
    _Case("采购员张三负责哪些订单", (), "negative"),
    _Case("系统里有哪些数据源", (), "negative"),
)


async def _enableSwitch(session: AsyncSession, key: str) -> None:
    session.add(SystemConfig(key=key, value="true"))
    await session.commit()


async def _warmCache(session: AsyncSession) -> None:
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(session)


async def test_corpus_has_fifty_cases() -> None:
    """语料规模守卫：设计 §5.3 要求 50 条，改动语料时不许悄悄缩水。"""
    assert len(_CORPUS) == 50
    assert sum(1 for c in _CORPUS if c.kind == "positive") == 36
    assert sum(1 for c in _CORPUS if c.kind == "ambiguous") == 4
    assert sum(1 for c in _CORPUS if c.kind == "negative") == 10


async def test_fifty_question_corpus_meets_acceptance(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    await seedKpiCatalog(dbSession)
    await seedKpiSemanticIndex(dbSession)
    await _enableSwitch(dbSession, SWITCH_MATCH_CONTAINMENT)
    await _warmCache(dbSession)

    positiveHits = 0
    falseHits = 0
    failures: list[str] = []

    for case in _CORPUS:
        resp = await client.get(
            _DIAGNOSE_URL, params={"question": case.question}, headers=_ADMIN_HEADERS
        )
        assert resp.status_code == 200, f"{case.question} → HTTP {resp.status_code}"
        body = resp.json()
        codes = tuple(sorted(m["kpiCode"] for m in body["matches"]))
        expected = tuple(sorted(case.expected))
        isAmbiguous = body["ambiguous"]
        ok = codes == expected and isAmbiguous == (case.kind == "ambiguous")

        if ok and case.kind == "positive":
            positiveHits += 1
        if not ok:
            failures.append(
                f"[{case.kind}] {case.question} → 实际 {codes} ambiguous={isAmbiguous}，"
                f"期望 {expected} ambiguous={case.kind == 'ambiguous'}"
            )
            if case.kind != "positive" or codes:
                # 负向被命中 / 歧义判错 / 正向命中错 KPI 都算误命中；
                # 正向完全没命中只拉低命中率，不算误命中。
                falseHits += 1

    report = (
        f"正向命中 {positiveHits}/36（要求 ≥{_MIN_POSITIVE_HITS}），"
        f"误命中 {falseHits}（要求 ≤{_MAX_FALSE_HITS}）\n" + "\n".join(failures)
    )
    assert positiveHits >= _MIN_POSITIVE_HITS, report
    assert falseHits <= _MAX_FALSE_HITS, report
    assert not failures, report


async def test_kpi_code_question_yields_non_empty_contract_block(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """M1 验收第三条：问句里直接给 KPI code ⇒ 契约块非空。

    这条需要真实挂靠的 metric（seed 的 12 条 metric_id 均为 NULL，契约块必空），
    所以单独构造一条 pass 的 KPI。
    """
    metric = OntologyMetric(
        metric_name="M1-OTD-metric",
        formula=(
            "SELECT COUNT(CASE WHEN R.RECEIPT_DATE <= O.PROMISED_RECEIPT_DATE THEN 1 END) "
            "/ NULLIF(COUNT(O.PROMISED_RECEIPT_DATE), 0) "
            "FROM THBI.DWD_PURCHASE_ORDER_LINE O "
            "JOIN THBI.DWD_GOODS_RECEIPT_LINE R ON O.PO_NO = R.PO_NO"
        ),
        agg_function="SUM",
        metric_type="RATIO",
        aggregation_order="WEIGHTED_RATIO",
        execution_source="ESCAPE_HATCH",
        numerator_measure={"agg": "COUNT", "expr": "RECEIPT_DATE"},
        denominator_measure={"agg": "COUNT", "expr": "PROMISED_RECEIPT_DATE"},
        grain_keys=["SUPPLIER_CODE"],
        created_by="p1-m1",
    )
    dbSession.add(metric)
    await dbSession.commit()
    dbSession.add(
        KpiCatalog(
            kpi_code="KPI_M1_OTD",
            kpi_name="M1 准时交付率",
            status="PUBLISHED",
            metric_id=metric.id,
            numerator_columns=["RECEIPT_DATE"],
            denominator_columns=["PROMISED_RECEIPT_DATE"],
            grain_keys=["SUPPLIER_CODE"],
            semantic_keywords=["准时交付"],
            match_threshold=Decimal("0.75"),
        )
    )
    await dbSession.commit()
    await _enableSwitch(dbSession, SWITCH_CONTRACT_INJECTION)
    await _enableSwitch(dbSession, SWITCH_MATCH_CONTAINMENT)
    await _warmCache(dbSession)

    resp = await client.get(
        _DIAGNOSE_URL,
        params={"question": "KPI_M1_OTD 这个指标怎么样"},
        headers=_ADMIN_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matches"][0]["kpiCode"] == "KPI_M1_OTD"
    assert body["matches"][0]["matchMethod"] == "exact_code"
    assert body["injectionBlockEmpty"] is False
