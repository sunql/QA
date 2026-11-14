"""P2 Tier A 全链路集成测试（真实 PostgreSQL + 完整 HTTP 链路）。

覆盖设计 §6 的四条执行分层路径 —— 测试只从 HTTP 入口发起，生产代码零改动：

1. Tier A：普通问句直通 ``metric.formula``，真实 asyncpg 连测试库自身，0 LLM。
2. Tier C：``execution_source=NONE`` ⇒ 只给口径说明，不执行、不调 LLM。
3. LEGACY：Tier A 开关行缺失 ⇒ 回落旧 FD 路径（不执行、不报错）。
4. Tier B：显式时间限定 / 执行异常 / 非只读公式 ⇒ 哨兵拆包后走主 NL2SQL 流水线。
5. 非流式 Tier B 必须 200：钉住「哨兵拆包在 ``if preamble is not None`` 之前」，
   否则 ``requireFullResponse`` 会抛 ``RuntimeError`` → 500。

约定：
- 业务库 = 测试元数据库自身（同 ``test_kpi_tier_a_execution_pg.py``），不建任何表。
- 每个用例前后 ``business_db_pool.reset_pool()``：``_adapters`` 按 datasourceId
  缓存，TRUNCATE 清不掉进程内缓存。
- KPI 匹配缓存是全局单例，不随 TRUNCATE 清空；每个用例显式失效 + 预热。
- Tier B 用例给主链路注入最小 fake（路由 / LLM / adapter / embedding），
  避免无 key 的 LLM 与真实业务库；Tier A 用例**不**替换 adapter —— 要测真连接。
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import (
    DataSource,
    KpiCatalog,
    LlmConfig,
    OntologyClass,
    OntologyMetric,
    OntologyMetricSource,
    OntologyProperty,
)
from app.infrastructure import business_db_pool
from app.infrastructure.llm.base_client import StreamChunk
from app.infrastructure.security.crypto import encryptApiKey
from app.models.system_config import SystemConfig
from app.services.kpi_match_cache import get_kpi_match_cache
from app.services.kpi_metric_reconcile import reconcileMetric
from app.services.kpi_metric_spec import governanceSpecOf, techniqueOf
from app.services.kpi_metric_switches import SWITCH_MATCH_CONTAINMENT, SWITCH_TIER_A
from app.services.kpi_runtime_audit import RUNTIME_ENTITY_TYPE
from app.tests._pg_support import resolveTestDatabaseUrl

pytestmark = pytest.mark.asyncio

_CHAT_URL = "/api/v1/chat"
_CHAT_STREAM_URL = "/api/v1/chat/stream"
_HEADERS = {"X-User-Id": "test-user-001"}

# 单值 COUNT 公式：结构化列 {"cnt"} ⊆ formula 标识符 {"cnt", "kpi_catalog"}。
_TIER_A_FORMULA = "SELECT COUNT(*) AS cnt FROM kpi_catalog"
# cnt 是真实表里不存在的列 ⇒ 对账 pass（标识符集合含 cnt），执行时才报错。
_BROKEN_FORMULA = "SELECT cnt FROM kpi_catalog"
# 非只读公式：对账同样 pass，执行前被 _assert_read_only 拦下。
_WRITE_FORMULA = "DELETE FROM kpi_catalog WHERE cnt > 0"


# ---------------------------------------------------------------------------
# 进程内缓存隔离（模块级 autouse）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def _resetBusinessDbPools():
    """每用例前后释放 adapter 缓存：TRUNCATE 不会清进程内 ``_adapters``。"""
    await business_db_pool.reset_pool()
    yield
    await business_db_pool.reset_pool()


# ---------------------------------------------------------------------------
# Arrange helpers
# ---------------------------------------------------------------------------


async def _seedSelfDatasource(dbSession: AsyncSession) -> DataSource:
    """把测试元数据库自身当作业务库：真实 asyncpg + 真实只读事务。"""
    url = make_url(resolveTestDatabaseUrl())
    ds = DataSource(
        name="tier-a-chat-self-pg",
        type="postgresql",
        host=url.host,
        port=url.port or 5432,
        database_name=url.database,
        username=url.username,
        password_encrypted=encryptApiKey(url.password or ""),
        is_active=True,
    )
    dbSession.add(ds)
    await dbSession.commit()
    await dbSession.refresh(ds)
    return ds


async def _seedSwitches(
    dbSession: AsyncSession,
    *,
    containment: bool = True,
    tierA: bool = True,
) -> None:
    """v2 包含式匹配 + Tier A 执行器均为 system_config 灰度行（默认关）。"""
    rows: list[SystemConfig] = []
    if containment:
        rows.append(SystemConfig(key=SWITCH_MATCH_CONTAINMENT, value="true"))
    if tierA:
        rows.append(SystemConfig(key=SWITCH_TIER_A, value="true"))
    if rows:
        dbSession.add_all(rows)
        await dbSession.commit()


async def _seedKpiWithMetric(
    dbSession: AsyncSession,
    *,
    datasourceId: int,
    kpiCode: str,
    kpiName: str,
    keywords: list[str],
    formula: str,
    executionSource: str = "ESCAPE_HATCH",
    businessDefinition: str = "按测试元数据表计数",
    timeColumn: str | None = None,
    metricOverrides: dict | None = None,
    sourceTable: str | None = None,
    sourceRole: str = "NUMERATOR",
) -> tuple[OntologyMetric, KpiCatalog]:
    """对账 pass 的最小形状：MEASURE + 分子 {cnt} + 粒度 {kpi_code}。

    P3 D3 起层 2 走别名解析：夹具按**回填后等价状态**种源类属性
    （``cnt`` / ``kpi_code`` 可解析）与时间粒度（``MONTH``），否则无源类会
    判 unknown、time_grain 缺失也会判 unknown，Tier A 判定被误伤。

    ``metricOverrides`` 在 flush 前按 setattr 覆盖技术形态字段（COMPILED 用例
    需要把分子换成物理列）；``sourceTable`` / ``sourceRole`` 供编译器用例给源类
    落 ``source_table`` 与 BASE 角色。
    """
    metric = OntologyMetric(
        metric_name=f"{kpiCode}-metric",
        formula=formula,
        metric_type="MEASURE",
        numerator_measure={"agg": "COUNT", "expr": "cnt"},
        grain_keys=["kpi_code"],
        execution_source=executionSource,
        time_grain="MONTH",
        time_column=timeColumn,
        created_by="tier-a-chat-test",
    )
    for key, value in (metricOverrides or {}).items():
        setattr(metric, key, value)
    dbSession.add(metric)
    await dbSession.flush()

    kpi = KpiCatalog(
        kpi_code=kpiCode,
        kpi_name=kpiName,
        business_definition=businessDefinition,
        unit="个",
        status="PUBLISHED",
        metric_id=metric.id,
        datasource_id=datasourceId,
        semantic_keywords=keywords,
        match_threshold=Decimal("0.75"),
        numerator_columns=["cnt"],
        grain_keys=["kpi_code"],
        grain="按kpi_code+月",
    )
    dbSession.add(kpi)

    ontologyClass = OntologyClass(
        class_name=f"{kpiCode}-source",
        version=1,
        source_table=sourceTable,
        created_by="tier-a-chat-test",
    )
    dbSession.add(ontologyClass)
    await dbSession.flush()
    for column in ("cnt", "kpi_code"):
        dbSession.add(
            OntologyProperty(
                class_id=ontologyClass.id,
                property_name=column,
                source_column=column,
                data_type="VARCHAR",
            )
        )
    if timeColumn:
        # P4a：time_column 必须能在源类日期候选中解析，否则层 2 对账 unknown
        # ⇒ degraded ⇒ 路由根本进不了 Tier A（渲染分支无从被测）。
        dbSession.add(
            OntologyProperty(
                class_id=ontologyClass.id,
                property_name=timeColumn,
                source_column=timeColumn,
                data_type="DATE",
            )
        )
    dbSession.add(
        OntologyMetricSource(
            metric_id=metric.id,
            class_id=ontologyClass.id,
            role=sourceRole,
        )
    )
    await dbSession.commit()
    await dbSession.refresh(kpi)
    return metric, kpi


async def _warmKpiCache(dbSession: AsyncSession) -> None:
    """全局缓存不随 TRUNCATE 清空，每个用例必须显式失效 + 预热。"""
    cache = get_kpi_match_cache()
    cache.onKpiChanged()
    await cache.warmUp(dbSession)


# ---------------------------------------------------------------------------
# Tier B 主链路最小 fake（仅这些用例注入；Tier A 用真实 adapter）
# ---------------------------------------------------------------------------

_ROWS = [{"NAME": "A", "QTY": Decimal(10)}, {"NAME": "B", "QTY": Decimal(20)}]


class _StubLlm:
    """按 prompt 内容路由：计划 / SQL / 图表 JSON / 回答；流式回答两段增量。"""

    async def complete(self, messages: list, **kwargs) -> object:
        system = messages[0].content
        user = messages[1].content

        class _Resp:
            content = ""
            modelName = "test-model"
            promptTokens = 10
            completionTokens = 5

        if "图表类型" in user:
            _Resp.content = '{"title":{"text":"t"},"series":[{"type":"bar","data":[1,2]}]}'
        elif "解析为查询计划" in system:
            _Resp.content = (
                '{"target":"各供应商的收货数量汇总","selectedClasses":["PRECEIPT"],'
                '"selectedProperties":["NAME","QTY"],"groupBy":["NAME"]}'
            )
        elif "生成 SQL 时必须" in system:
            _Resp.content = (
                "```sql\nSELECT NAME, SUM(QTY) AS TOTAL_QTY FROM ZJTH.PRECEIPT "
                "GROUP BY NAME FETCH FIRST 10 ROWS ONLY\n```"
            )
        else:
            _Resp.content = "查询完成，共 2 条记录。"
        return _Resp()

    async def completeStream(self, messages: list, **kwargs):
        for piece in ("查询完成，", "共 2 条记录。"):
            yield StreamChunk(
                content=piece,
                isDone=False,
                promptTokens=0,
                completionTokens=0,
                modelName="test-model",
            )
        yield StreamChunk(
            content="",
            isDone=True,
            promptTokens=10,
            completionTokens=5,
            modelName="test-model",
        )


class _RouterFor:
    def __init__(self, config: LlmConfig) -> None:
        self._config = config

    def selectModel(self, configs, prompt, ctx) -> LlmConfig:
        return self._config

    def selectFallbackModel(self, configs, excludeId) -> LlmConfig | None:
        return None


class _FakeAdapter:
    """Tier B 主链路：不触达真实业务库。"""

    async def execute_read_only(self, sql: str) -> list[dict]:
        return _ROWS


class _StubEmbedding:
    async def storeQueryEmbedding(self, **kwargs) -> None:
        return None


async def _seedTierBPipelineSupport(
    dbSession: AsyncSession,
) -> LlmConfig:
    """主链路所需的最小元数据：LlmConfig + 一个可被计划引用的本体类。"""
    config = LlmConfig(
        model_name="test-model",
        provider="openai",
        cost_per_1k_input=Decimal("0.001"),
        cost_per_1k_output=Decimal("0.002"),
    )
    cls = OntologyClass(
        class_name="PRECEIPT",
        class_alias="收货单",
        source_table="ZJTH.PRECEIPT",
        properties=[
            OntologyProperty(
                property_name="NAME", data_type="STRING", source_column="NAME"
            ),
            OntologyProperty(
                property_name="QTY", data_type="DECIMAL", source_column="QTY"
            ),
        ],
    )
    dbSession.add_all([config, cls])
    await dbSession.commit()
    await dbSession.refresh(config)
    return config


def _installTierBFakes(monkeypatch, config: LlmConfig) -> None:
    """替换 ChatService 单例的路由 / LLM / adapter / embedding。

    ``_kpiExecutionService`` 在构造时已捕获 ``get_adapter``，不受本替换影响 ——
    执行失败的用例仍走真实 adapter，这正是要验证的路径。
    """
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(chat_module._service, "_modelRouter", _RouterFor(config))
    monkeypatch.setattr(
        chat_module._service, "_llmFactory", lambda _config: _StubLlm()
    )
    monkeypatch.setattr(
        chat_module._service,
        "_adapterProvider",
        lambda _dsId, _ds: _FakeAdapter(),
    )
    monkeypatch.setattr(chat_module._service, "_embedding", _StubEmbedding())


# ---------------------------------------------------------------------------
# HTTP / SSE helpers
# ---------------------------------------------------------------------------


def _payload(question: str, datasourceId: int) -> dict:
    return {
        "sessionId": f"s-{uuid.uuid4().hex[:12]}",
        "question": question,
        "datasourceId": datasourceId,
    }


def _metaFrames(text: str) -> list[dict]:
    """按空行切 SSE 帧，只保留 ``event: meta``，返回解析后的 data。"""
    frames: list[dict] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event: str | None = None
        data: dict | None = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if event == "meta" and data is not None:
            frames.append(data)
    return frames


async def _countKpiCatalog(dbSession: AsyncSession) -> int:
    result = await dbSession.execute(text("SELECT COUNT(*) FROM kpi_catalog"))
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 1. Tier A：零 LLM 确定性执行
# ---------------------------------------------------------------------------


async def test_plain_question_executes_tier_a_zero_llm(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_OK",
        kpiName="TierA测试指标",
        keywords=["TierA测试指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    # 同一连接先取基线：Tier A 的 COUNT(*) 必须与它一致。
    baseline = await _countKpiCatalog(dbSession)
    assert baseline >= 1  # 至少包含刚播种的 KPI 行

    resp = await client.post(
        _CHAT_URL,
        json=_payload("TierA测试指标是多少", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["intent"] == "l1_match"
    assert body["tokensUsed"] == 0
    assert body["cost"] == 0.0
    assert body["executionTier"] == "A"
    assert body["degradedReason"] is None

    data = body["data"]
    assert data and len(data) == 1
    value = next(v for v in data[0].values() if v is not None)
    assert abs(float(value) - float(baseline)) <= 0.01, (
        f"Tier A 结果 {value!r} 与基线 COUNT(*)={baseline} 不一致"
    )
    assert body["kpiCode"] == kpi.kpi_code


# ---------------------------------------------------------------------------
# 2. Tier C：execution_source=NONE ⇒ 只给口径说明
# ---------------------------------------------------------------------------


async def test_none_source_returns_tier_c_text(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_C_NONE",
        kpiName="TierC测试指标",
        keywords=["TierC测试指标"],
        formula=_TIER_A_FORMULA,
        executionSource="NONE",
        businessDefinition="该指标暂无技术形态",
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("TierC测试指标是多少", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["executionTier"] == "C"
    assert "execution_source_none" in (body["degradedReason"] or "")
    assert "暂不可确定性计算" in body["answer"]
    assert body["tokensUsed"] == 0
    assert body["data"] is None


# ---------------------------------------------------------------------------
# 3. LEGACY：Tier A 开关行缺失 ⇒ 旧 FD 路径
# ---------------------------------------------------------------------------


async def test_switch_row_missing_keeps_legacy_behavior(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    ds = await _seedSelfDatasource(dbSession)
    # 只开 v2 匹配（保证能命中），**不**播种 KPI_METRIC_TIER_A_ENABLED。
    await _seedSwitches(dbSession, tierA=False)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_LEGACY_OK",
        kpiName="TierLegacy测试指标",
        keywords=["TierLegacy测试指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("TierLegacy测试指标是多少", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["executionTier"] == "LEGACY"
    assert body["degradedReason"] == "tier_a_disabled"
    # 旧 FD 路径找不到锚点 ⇒ 不执行、不报错。
    assert body["data"] is None


# ---------------------------------------------------------------------------
# 4. Tier B（流式）：显式时间限定
# ---------------------------------------------------------------------------


async def test_time_scoped_question_degrades_to_tier_b(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_OK",
        kpiName="TierA测试指标",
        keywords=["TierA测试指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_STREAM_URL,
        json=_payload("近6个月TierA测试指标", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    metas = _metaFrames(resp.text)
    assert metas, f"未收到 meta 帧，SSE={resp.text[:400]}"

    first = metas[0]
    assert first["executionTier"] == "B"
    assert first["degradedReason"] == "explicit_time_scope"
    # 哨兵已拆包：preamble 未终结，因此首个 meta 不带 routingLayer。
    assert "routingLayer" not in first


# ---------------------------------------------------------------------------
# 5. Tier B（流式）：执行异常降级
# ---------------------------------------------------------------------------


async def test_formula_execution_error_degrades_to_tier_b(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    metric, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_ERR",
        kpiName="TierA报错指标",
        keywords=["TierA报错指标"],
        formula=_BROKEN_FORMULA,
    )
    await _warmKpiCache(dbSession)

    # 先钉住对账结论：该 formula 确实能过对账，才会走到执行并在此报错。
    reconciled = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
    assert reconciled.isTierAEligible is True

    resp = await client.post(
        _CHAT_STREAM_URL,
        json=_payload("TierA报错指标统计", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    metas = _metaFrames(resp.text)
    assert metas, f"未收到 meta 帧，SSE={resp.text[:400]}"
    assert metas[0]["executionTier"] == "B"
    assert metas[0]["degradedReason"] == "execution_error"


# ---------------------------------------------------------------------------
# 6. Tier B（非流式）：非只读公式绝不触达 adapter
# ---------------------------------------------------------------------------


async def test_non_readonly_formula_never_reaches_adapter(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    metric, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_WRITE",
        kpiName="TierA写指标",
        keywords=["TierA写指标"],
        formula=_WRITE_FORMULA,
    )
    await _warmKpiCache(dbSession)

    reconciled = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
    assert reconciled.isTierAEligible is True

    before = await _countKpiCatalog(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("TierA写指标统计", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["executionTier"] == "B"
    assert body["degradedReason"] == "formula_not_read_only"

    # 种子 KPI 行仍在且行数未变 ⇒ 写语句从未执行。
    assert await _countKpiCatalog(dbSession) == before
    remaining = await dbSession.execute(
        text("SELECT COUNT(*) FROM kpi_catalog WHERE kpi_code = :code"),
        {"code": kpi.kpi_code},
    )
    assert int(remaining.scalar_one()) == 1


# ---------------------------------------------------------------------------
# 7. Tier B（非流式）必须 200：哨兵拆包位置守卫
# ---------------------------------------------------------------------------


async def test_time_scoped_question_non_stream_returns_200_and_tier_b(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """若 Tier B 拆包被移到 ``if preamble is not None`` 之下，
    ``requireFullResponse`` 会对哨兵抛 ``RuntimeError`` → 500；本用例钉住 200。"""
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_OK",
        kpiName="TierA测试指标",
        keywords=["TierA测试指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("近6个月TierA测试指标", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionTier"] == "B"
    assert body["degradedReason"] == "explicit_time_scope"


# ---------------------------------------------------------------------------
# 8. 两入口观测一致性：METRIC 意图不得暴露 Tier B 字段
# ---------------------------------------------------------------------------


async def test_metric_intent_gets_no_tier_b_fields_on_either_entry(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """两入口观测一致性守卫：METRIC 意图在流式/非流式都**不得**带 Tier B 字段。

    问句「TierA报错指标是多少」的意图分类由 ``IntentService.classifyResult``
    实测得出为 ``METRIC``（不是猜测）——该意图在两个入口都会早退到领域命令，
    不会落到通用查询主链路。流式入口的 META 帧在意图分支之前产出，故必须按
    本帧解析出的 intent 门控；非流式入口的 ``_attachKpiHint`` 本就在早退分支之后。
    两者都应表现为 ``executionTier is None`` / ``degradedReason is None``。

    真空通过风险由第 5 例 ``test_formula_execution_error_degrades_to_tier_b`` 排除：
    该用例使用**同一夹具**（``KPI_TIER_A_ERR`` + 同一坏公式）与同一 KPI 名的
    QUERY 问句「TierA报错指标统计」，断言流式首帧 ``executionTier == "B"``，
    证明本夹具的 Tier B hint 确实存在 —— 故本用例的「无字段」是意图门控所致，
    而非 hint 为空。此处不再重复发请求。
    """
    from app.services.intent_service import IntentService

    question = "TierA报错指标是多少"
    result = IntentService().classifyResult(question)
    assert result.intent.value == "metric", (
        f"问句意图实测为 {result.intent.value!r}，本守卫依赖 METRIC 早退路径"
    )

    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_ERR",
        kpiName="TierA报错指标",
        keywords=["TierA报错指标"],
        formula=_BROKEN_FORMULA,
    )
    await _warmKpiCache(dbSession)

    # 流式：首个 META 帧 intent=metric，且不含 Tier B 观测键。
    stream_resp = await client.post(
        _CHAT_STREAM_URL,
        json=_payload(question, ds.id),
        headers=_HEADERS,
    )
    assert stream_resp.status_code == 200, stream_resp.text
    metas = _metaFrames(stream_resp.text)
    assert metas, f"未收到 meta 帧，SSE={stream_resp.text[:400]}"
    first = metas[0]
    assert first["intent"] == "metric"
    assert "executionTier" not in first, (
        f"METRIC 意图不得暴露 executionTier，实际 meta={first!r}"
    )
    assert "degradedReason" not in first, (
        f"METRIC 意图不得暴露 degradedReason，实际 meta={first!r}"
    )

    # 非流式：领域命令早退，Tier 字段保持 None（与流式一致）。
    chat_resp = await client.post(
        _CHAT_URL,
        json=_payload(question, ds.id),
        headers=_HEADERS,
    )
    assert chat_resp.status_code == 200, chat_resp.text
    body = chat_resp.json()
    assert body["intent"] == "metric"
    assert body["executionTier"] is None
    assert body["degradedReason"] is None


# ---------------------------------------------------------------------------
# 9. 两入口观测一致性（正向）：CLARIFY 意图必须带 Tier B 字段
# ---------------------------------------------------------------------------


async def test_clarify_intent_keeps_tier_b_fields_on_either_entry(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """两入口观测一致性正向守卫：CLARIFY 落到通用查询主链路，必须带 Tier B 字段。

    问句「近6个月TierA报错指标是什么」的意图由 ``IntentService.classifyResult``
    实测得出为 ``CLARIFY``（不是猜测）。该意图**不在** ``processMessage`` 的早退
    分支里：``_handleGenericQuery`` → ``_handleClarify`` 返回后仍会经过
    ``_attachKpiHint``，因此非流式会带 ``executionTier=B`` /
    ``degradedReason=explicit_time_scope``；流式首帧 META 在分支之前产出，门控
    集合必须包含 CLARIFY 才能与之一致。本用例是字段**存在**且取值相同的正向守卫
    （与第 8 例 METRIC 的「无字段」互为对偶）。
    """
    from app.services.intent_service import IntentService

    question = "近6个月TierA报错指标是什么"
    result = IntentService().classifyResult(question)
    assert result.intent.value == "clarify", (
        f"问句意图实测为 {result.intent.value!r}，本守卫依赖 CLARIFY 走通用查询链路"
    )

    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_ERR",
        kpiName="TierA报错指标",
        keywords=["TierA报错指标"],
        formula=_BROKEN_FORMULA,
    )
    await _warmKpiCache(dbSession)

    # 流式：首帧 intent=clarify，且带 Tier B 观测键。
    stream_resp = await client.post(
        _CHAT_STREAM_URL,
        json=_payload(question, ds.id),
        headers=_HEADERS,
    )
    assert stream_resp.status_code == 200, stream_resp.text
    metas = _metaFrames(stream_resp.text)
    assert metas, f"未收到 meta 帧，SSE={stream_resp.text[:400]}"
    first = metas[0]
    assert first["intent"] == "clarify", first
    assert first["executionTier"] == "B", first
    assert first["degradedReason"] == "explicit_time_scope", first

    # 非流式：CLARIFY 走 _handleClarify，返回后 _attachKpiHint 挂上同一组字段。
    chat_resp = await client.post(
        _CHAT_URL,
        json=_payload(question, ds.id),
        headers=_HEADERS,
    )
    assert chat_resp.status_code == 200, chat_resp.text
    body = chat_resp.json()
    assert body["intent"] == "clarify"
    assert body["executionTier"] == "B"
    assert body["degradedReason"] == "explicit_time_scope"


# ---------------------------------------------------------------------------
# 8. P4a：{time_scope} 占位符 —— 非 Oracle fail-closed / Oracle 渲染放行
# ---------------------------------------------------------------------------

_PLACEHOLDER_FORMULA = "SELECT COUNT(*) AS cnt FROM kpi_catalog WHERE {time_scope}"


async def test_placeholder_metric_pg_fails_closed_to_tier_b(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """占位符 + time_column 就绪 + postgresql ⇒ 执行层渲染 fail-closed。

    路由判 Tier A（time_param_ready=True），渲染层抛 unsupported ⇒ degraded
    reason=time_scope_render_unsupported_dialect；绝不让 LLM 猜时间、绝不 500。
    """
    ds = await _seedSelfDatasource(dbSession)  # type == "postgresql"
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TScopePG",
        kpiName="时间占位PG指标",
        keywords=["时间占位PG指标"],
        formula=_PLACEHOLDER_FORMULA,
        timeColumn="created_time",
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("近6个月时间占位PG指标", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionTier"] == "B"
    assert body["degradedReason"] == "time_scope_render_unsupported_dialect"


async def test_placeholder_metric_oracle_renders_predicate_into_formula(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """占位符 + oracle ⇒ 渲染成功，发给 adapter 的 formula 含 DATE 字面量谓词。

    本地无真实 Oracle：替换执行器 live adapter provider 捕获执行入参（渲染层
    证据），fake adapter 返回合法行 ⇒ 全链路判 Tier A。
    """
    captured: dict[str, str] = {}

    class _FakeAdapter:
        async def execute_read_only(self, sql: str, maxRows: int):  # noqa: ANN001
            captured["sql"] = sql
            return [{"cnt": 7}]

    def _fakeGetAdapter(datasourceId, datasource):  # noqa: ANN001
        return _FakeAdapter()

    # ``KpiExecutionService`` 在构造时已捕获 adapterProvider（ChatService 注入的
    # ``get_adapter``），事后 patch 模块级名字无效；必须 patch 实例上的 live provider。
    import app.api.v1.chat as chat_module

    monkeypatch.setattr(
        chat_module._service._kpiExecutionService,
        "_adapterProvider",
        _fakeGetAdapter,
    )

    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    metric, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TScopeOra",
        kpiName="时间占位Oracle指标",
        keywords=["时间占位Oracle指标"],
        formula=_PLACEHOLDER_FORMULA,
        timeColumn="created_time",
    )
    # 自连 datasource 是 postgresql ⇒ 改 type 后**提交**（本用例专测 oracle 渲染分支）。
    # 仅 flush 不够：请求走另一个 session/连接，未提交的 UPDATE 对它不可见。
    ds.type = "oracle"
    await dbSession.commit()
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("近6个月时间占位Oracle指标", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionTier"] == "A", body
    sql = captured["sql"]
    assert "{time_scope}" not in sql
    assert "created_time >= DATE '" in sql
    assert "created_time < DATE '" in sql


# ---------------------------------------------------------------------------
# 9. COMPILED：结构化列实时编译，0 LLM
# ---------------------------------------------------------------------------


async def test_tier_a_compiled_metric_executes_without_llm(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """COMPILED：结构化列编译成单值只读 SELECT，0 LLM。

    ``kpi_code`` 是 NOT NULL 列 ⇒ ``COUNT(kpi_code)`` 与 ``COUNT(*)`` 等价，
    对账层 2 也能把治理分子列与技术 measure 对齐（``COUNT(*)`` 的 expr=None
    会判 technical_measure_unjudgeable，永远进不了 Tier A）。
    """
    import app.api.v1.chat as chat_module

    def _failLlm(_config):  # noqa: ANN001
        raise AssertionError("Tier A COMPILED 不得调用 LLM")

    monkeypatch.setattr(chat_module._service, "_llmFactory", _failLlm)

    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_COMPILED",
        kpiName="编译指标",
        keywords=["编译指标"],
        formula="",
        executionSource="COMPILED",
        metricOverrides={
            "numerator_measure": {"agg": "COUNT", "expr": "kpi_code"},
        },
        sourceTable="kpi_catalog",
        sourceRole="BASE",
    )
    # 治理侧分子列必须与技术 measure 的物理列一致，否则层 2 判 unknown。
    kpi.numerator_columns = ["kpi_code"]
    await dbSession.commit()
    await _warmKpiCache(dbSession)

    baseline = await _countKpiCatalog(dbSession)
    assert baseline >= 1

    resp = await client.post(
        _CHAT_URL,
        json=_payload("编译指标统计", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["intent"] == "l1_match"
    assert body["executionTier"] == "A"
    assert body["degradedReason"] is None
    assert body["tokensUsed"] == 0
    assert body["cost"] == 0.0
    assert body["kpiCode"] == kpi.kpi_code

    data = body["data"]
    assert data and len(data) == 1
    value = next(v for v in data[0].values() if v is not None)
    assert abs(float(value) - float(baseline)) <= 0.01, (
        f"COMPILED 结果 {value!r} 与基线 COUNT(*)={baseline} 不一致"
    )


# ---------------------------------------------------------------------------
# 10. COMPILED：编译 fail-closed ⇒ Tier B（不是 500）
# ---------------------------------------------------------------------------


async def test_tier_a_compiled_missing_join_degrades_to_tier_b(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """跨两类但 join 目录无边 ⇒ 编译 fail-closed，走 Tier B 而不是 500。"""
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    metric, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_TIER_A_NOJOIN",
        kpiName="无连接指标",
        keywords=["无连接指标"],
        formula="",
        executionSource="COMPILED",
        sourceTable="kpi_catalog",
        metricOverrides={
            "metric_type": "DIFFERENCE",
            "numerator_measure": {"agg": "COUNT", "expr": "cnt"},
            "denominator_measure": {"agg": "COUNT", "expr": "den_cnt"},
        },
    )
    numSource = (
        await dbSession.execute(
            select(OntologyMetricSource).where(
                OntologyMetricSource.metric_id == metric.id
            )
        )
    ).scalar_one()
    numClass = await dbSession.get(OntologyClass, numSource.class_id)
    denClass = OntologyClass(
        class_name="KPI_TIER_A_NOJOIN-den",
        version=1,
        source_table="other_den_table",
        created_by="tier-a-chat-test",
    )
    dbSession.add(denClass)
    await dbSession.flush()
    dbSession.add(
        OntologyProperty(
            class_id=denClass.id,
            property_name="den_cnt",
            source_column="den_cnt",
            data_type="VARCHAR",
        )
    )
    dbSession.add(
        OntologyMetricSource(
            metric_id=metric.id,
            class_id=denClass.id,
            role="DENOMINATOR",
        )
    )
    # 两源类 ⇒ 编译器必须靠显式 class_id 才能各自定位源表。
    metric.numerator_measure = {
        "agg": "COUNT", "expr": "cnt", "class_id": numClass.id,
    }
    metric.denominator_measure = {
        "agg": "COUNT", "expr": "den_cnt", "class_id": denClass.id,
    }
    kpi.denominator_columns = ["den_cnt"]
    await dbSession.commit()
    await _warmKpiCache(dbSession)

    # 先钉住对账：本夹具确实有资格进 Tier A，失败只能来自编译。
    reconciled = reconcileMetric(governanceSpecOf(kpi), techniqueOf(metric))
    assert reconciled.isTierAEligible is True, reconciled.reasons()

    resp = await client.post(
        _CHAT_URL,
        json=_payload("无连接指标统计", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executionTier"] == "B", body
    assert body["degradedReason"] == "compiled_join_path_missing", body


# ---------------------------------------------------------------------------
# 8. 运行时 Tier 事件：M4 看板的数据来源（audit_log kpi_runtime_event）
# ---------------------------------------------------------------------------


async def _runtimeEvents(dbSession: AsyncSession, kpiId: int) -> list[dict]:
    """读该 KPI 的运行时事件（按写入顺序），用于钉住「实际服务」的 Tier。"""
    result = await dbSession.execute(
        text(
            "SELECT after_json FROM audit_log "
            "WHERE entity_type = :t AND entity_id = :id ORDER BY id"
        ),
        {"t": RUNTIME_ENTITY_TYPE, "id": kpiId},
    )
    events: list[dict] = []
    for raw in result.scalars().all():
        events.append(json.loads(raw) if isinstance(raw, str) else raw)
    return events


async def test_tier_a_chat_records_tier_a_event(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """Tier A 正常执行 ⇒ 落一条 tier=A、reason=NULL 的事件。"""
    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_RT_A",
        kpiName="运行时A指标",
        keywords=["运行时A指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL, json=_payload("运行时A指标是多少", ds.id), headers=_HEADERS
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["executionTier"] == "A"

    events = await _runtimeEvents(dbSession, kpi.id)
    assert len(events) == 1, events
    assert events[0]["kpi_code"] == "KPI_RT_A"
    assert events[0]["tier"] == "A"
    assert events[0]["reason"] is None
    # v2 包含匹配优先于关键词匹配（_seedSwitches 已开 SWITCH_MATCH_CONTAINMENT）。
    assert events[0]["match_method"] == "containment"


async def test_streamed_tier_a_event_persists(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """流式终结分支只 yield SSE 帧就 return，从不 commit 请求会话。

    事件必须走独立会话自提交，否则流式问句一条都记不上（实测：非流式 1 行、
    流式 0 行），看板会把 Tier 分布系统性偏向 Tier B。
    """
    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_RT_STREAM_A",
        kpiName="流式运行时指标",
        keywords=["流式运行时指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_STREAM_URL,
        json=_payload("流式运行时指标统计", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    metas = _metaFrames(resp.text)
    assert metas and metas[0]["executionTier"] == "A"

    events = await _runtimeEvents(dbSession, kpi.id)
    assert len(events) == 1, events
    assert events[0]["tier"] == "A"
    assert events[0]["reason"] is None


async def test_tier_c_chat_records_tier_c_event_with_reason(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """Tier C ⇒ tier=C，降级原因与响应里的一致（execution_source_none）。"""
    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_RT_C",
        kpiName="运行时C指标",
        keywords=["运行时C指标"],
        formula=_TIER_A_FORMULA,
        executionSource="NONE",
        businessDefinition="该指标暂无技术形态",
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL, json=_payload("运行时C指标是多少", ds.id), headers=_HEADERS
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["executionTier"] == "C"

    events = await _runtimeEvents(dbSession, kpi.id)
    assert len(events) == 1, events
    assert events[0]["tier"] == "C"
    assert events[0]["reason"] == "execution_source_none"


async def test_tier_a_execution_failure_records_tier_b_event(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """Tier A 执行失败降级到 B ⇒ 事件按**实际服务**记 tier=B + execution_error。"""
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_RT_A_ERR",
        kpiName="运行时A报错指标",
        keywords=["运行时A报错指标"],
        formula=_BROKEN_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_STREAM_URL,
        json=_payload("运行时A报错指标统计", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    metas = _metaFrames(resp.text)
    assert metas and metas[0]["executionTier"] == "B"

    events = await _runtimeEvents(dbSession, kpi.id)
    assert len(events) == 1, events
    assert events[0]["tier"] == "B"
    assert events[0]["reason"] == "execution_error"


async def test_legacy_route_records_legacy_event(
    client: AsyncClient, dbSession: AsyncSession
) -> None:
    """Tier A 开关行缺失 ⇒ 实际走旧路径，事件记 LEGACY + tier_a_disabled。"""
    ds = await _seedSelfDatasource(dbSession)
    await _seedSwitches(dbSession, tierA=False)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_RT_LEGACY",
        kpiName="运行时Legacy指标",
        keywords=["运行时Legacy指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    resp = await client.post(
        _CHAT_URL, json=_payload("运行时Legacy指标是多少", ds.id), headers=_HEADERS
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["executionTier"] == "LEGACY"

    events = await _runtimeEvents(dbSession, kpi.id)
    assert len(events) == 1, events
    assert events[0]["tier"] == "LEGACY"
    assert events[0]["reason"] == "tier_a_disabled"


async def test_legacy_build_failure_records_no_event(
    client: AsyncClient, dbSession: AsyncSession, monkeypatch
) -> None:
    """旧路径构答失败（``l1 is None``）⇒ 问句交给 L4，不能记成 LEGACY 事件。

    与 Tier A 同形：只有 KPI 路径真的服务了这个问句才记；否则 L4 回答的
    问句会被虚增进 Tier 分布。
    """
    ds = await _seedSelfDatasource(dbSession)
    config = await _seedTierBPipelineSupport(dbSession)
    _installTierBFakes(monkeypatch, config)
    await _seedSwitches(dbSession, tierA=False)
    _, kpi = await _seedKpiWithMetric(
        dbSession,
        datasourceId=ds.id,
        kpiCode="KPI_RT_LEGACY_NONE",
        kpiName="运行时Legacy失败指标",
        keywords=["运行时Legacy失败指标"],
        formula=_TIER_A_FORMULA,
    )
    await _warmKpiCache(dbSession)

    import app.api.v1.chat as chat_module

    async def _none(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat_module._service, "_buildL1Response", _none)

    resp = await client.post(
        _CHAT_URL,
        json=_payload("运行时Legacy失败指标是多少", ds.id),
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    assert await _runtimeEvents(dbSession, kpi.id) == []
