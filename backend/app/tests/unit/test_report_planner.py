"""ReportPlanner：mode 模板 / LLM 失败降级 / 数据值不被 LLM 改写。

Task 6 契约测试。持久化输入（session / turn / finding）一律落**真实 PostgreSQL**
（unit/conftest.py 的 `dbSession`），LLM 全 fake（零网络）。

`fakeReportDeps` 返回 `(planner, sessionId, turnId)`；测试按 brief 以
`compose(None, ...)` 调用——`None` 走 planner 的 `sessionFactory` 兜底（生产恒传真
session，见 task-6-report.md「偏差」）。
"""

from __future__ import annotations

import json

import pytest

from app.services.report_planner import ReportPlanner
from app.services.research_finding_aggregator import aggregateRows
from app.services.research_session_service import ResearchSessionService

QUESTION = "供应商收货量为什么下降"
CLAIM = "供应商A供货减少"
# 与 fake step data 逐字一致：verify 阶段把行数据落进 supporting_data["rows"]
FINDING_ROWS = [{"month": "2026-01", "cnt": 100}]


class FakeLlm:
    """确定性 LLM：返回一句解读文本；`fail=True` 模拟调用异常。"""

    def __init__(self, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[list] = []

    async def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if self._fail:
            raise RuntimeError("llm down")
        return type("R", (), {"content": "解读：指标下降明显。"})()


@pytest.fixture
async def fakeReportDeps(dbSession):
    """真库写 1 session + 1 turn + 1 finding（含行数据），返回 (planner, sessionId, turnId)。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=1, question=QUESTION)
    turn = await svc.appendTurn(
        dbSession, sessionId=row.id, role="user", content={"question": QUESTION}
    )
    await svc.saveFinding(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        claimText=CLAIM,
        supportingSql="SELECT 1 AS cnt",
        supportingData={"rowCount": 1, "error": None, "rows": [dict(r) for r in FINDING_ROWS]},
        confidence=0.72,
    )
    await dbSession.commit()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: FakeLlm(),
        sessionFactory=lambda: dbSession,
    )
    return planner, row.id, turn.id


@pytest.mark.asyncio
async def test_research_mode_section_order(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    kinds = [s["kind"] for s in payload["sections"]]
    assert kinds[0] == "executive_summary" and kinds[-1] == "methodology"
    assert any(b["type"] == "chart" for s in payload["sections"] for b in s["blocks"])
    assert "# " in md  # rendered_md 非空


@pytest.mark.asyncio
async def test_chart_data_not_llm_touched(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart_blocks = [b for s in payload["sections"] for b in s["blocks"] if b["type"] == "chart"]
    # R3-B：chart 块 key 统一为 'data'（不是 'rows'）
    assert chart_blocks[0]["content"]["data"][0]["cnt"] == 100  # 与 fake step data 逐字一致


@pytest.mark.asyncio
async def test_llm_failure_degrades_to_template(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    planner._llmClientFactory = lambda: FakeLlm(fail=True)
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    summary = payload["sections"][0]["blocks"][0]["content"]
    assert "结构化摘要" in summary


@pytest.mark.asyncio
async def test_unknown_mode_raises(fakeReportDeps) -> None:
    planner, sessionId, turnId = fakeReportDeps
    with pytest.raises(ValueError):
        await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="nope")


# ---------------------------------------------------------------------------
# 附加：三 mode 模板 / 归档 / 数字免疫（brief 4 条之外的加固）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("attribution", ["conclusion", "hypothesis_table", "data", "alternative", "methodology"]),
        ("compare", ["comparison_table", "data", "diff_analysis", "methodology"]),
    ],
)
async def test_mode_templates(fakeReportDeps, mode: str, expected: list[str]) -> None:
    planner, sessionId, turnId = fakeReportDeps
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode=mode)
    assert [s["kind"] for s in payload["sections"]] == expected


@pytest.mark.asyncio
async def test_compose_is_pure_assembly_no_report_rows(fakeReportDeps, dbSession) -> None:
    """compose 只装配不归档（归档在调用方 `_stageReport`）：报告表不因 compose 新增行。"""
    planner, sessionId, turnId = fakeReportDeps
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    assert await planner._sessions.listReports(dbSession, sessionId) == []
    assert payload["sessionId"] == str(sessionId) and payload["turnId"] == str(turnId)
    assert payload["findingsRef"][0]["claim"] == CLAIM
    assert "| 假设 |" not in md  # research 模式不含假设验证表


@pytest.mark.asyncio
async def test_llm_prompt_excludes_raw_rows(fakeReportDeps) -> None:
    """数字免疫：喂给 LLM 的 prompt 不含原始行数据（100 只出现在 chart 块）。"""
    planner, sessionId, turnId = fakeReportDeps
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    payload, md = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    promptText = "\n".join(m.content for messages in fake.calls for m in messages)
    assert CLAIM in promptText and "100" not in promptText
    chart = next(
        b for s in payload["sections"] for b in s["blocks"] if b["type"] == "chart"
    )
    # R3-B：chart 块 key 统一为 'data'（不是 'rows'）
    assert chart["content"]["data"][0]["cnt"] == 100
    assert chart["sourceRefs"][0]["kind"] == "finding"
    assert "100" in md  # 数字在 MD 里逐字来自 chart 块渲染


# ---------------------------------------------------------------------------
# R2 plan Task 4：stats 进 FindingView + 放开 prompt + 4 section 展示
# ---------------------------------------------------------------------------


@pytest.fixture
async def fakeReportDepsWithStats(dbSession):
    """带 stats 的 finding：用于验证 stats 在 prompt / 各 section 里的展示。"""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=1, question=QUESTION)
    turn = await svc.appendTurn(
        dbSession, sessionId=row.id, role="user", content={"question": QUESTION}
    )
    stats = {
        "topContributors": [
            {"dim": "base", "value": "J1", "sharePct": 60.0, "count": 3},
            {"dim": "base", "value": "T2", "sharePct": 20.0, "count": 1},
            {"dim": "base", "value": "H1", "sharePct": 20.0, "count": 1},
        ],
        "distribution": {"amount": {"min": 10.0, "max": 100.0, "mean": 52.0, "std": 38.2}},
        "mom": [
            {"period": "2026-01", "value": 100, "deltaPct": 0.0},
            {"period": "2026-02", "value": 115, "deltaPct": 15.0},
        ],
        "anomalies": [{"dim": "amount", "value": 1000, "zScore": 2.5}],
        "numericColumns": ("amount",),
        "dimensionColumns": ("base",),
        "rowCount": 5,
    }
    await svc.saveFinding(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        claimText=CLAIM,
        supportingSql="SELECT 1",
        supportingData={"rowCount": 5, "error": None, "rows": [dict(r) for r in FINDING_ROWS]},
        confidence=0.72,
        stats=stats,
    )
    await dbSession.commit()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: FakeLlm(),
        sessionFactory=lambda: dbSession,
    )
    return planner, row.id, turn.id


@pytest.mark.asyncio
async def test_aggregated_stats_included_in_finding_summary(fakeReportDepsWithStats) -> None:
    """_findingSummary 含 stats 数字（top1），但不含原始行。"""
    planner, sessionId, turnId = fakeReportDepsWithStats
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    promptText = "\n".join(m.content for messages in fake.calls for m in messages)
    # Stats 数字（60.0, J1）应进入 prompt —— 任务 4c 验证
    assert "60.0" in promptText and "J1" in promptText
    # 原始行（100 / FINDING_ROWS cnt=100）仍不在 prompt
    assert "100" not in promptText  # 注意：60.0 不含 "100"


@pytest.mark.asyncio
async def test_hypothesis_table_shows_top_contributor(fakeReportDepsWithStats) -> None:
    """attribution 模式的 hypothesis_table 多 1 列「主要贡献者」，值 = top1 标签。"""
    planner, sessionId, turnId = fakeReportDepsWithStats
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="attribution")
    table = next(
        s for s in payload["sections"] if s["kind"] == "hypothesis_table"
    )
    block = table["blocks"][0]
    assert "主要贡献者" in block["content"]["columns"]
    row = block["content"]["rows"][0]
    assert row["主要贡献者"] == "base=J1 (60.0%)"


@pytest.mark.asyncio
async def test_comparison_table_shows_numeric_range(fakeReportDepsWithStats) -> None:
    """compare 模式的 comparison_table 多 1 列「数值范围」，值 = min-max。"""
    planner, sessionId, turnId = fakeReportDepsWithStats
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="compare")
    table = next(
        s for s in payload["sections"] if s["kind"] == "comparison_table"
    )
    block = table["blocks"][0]
    assert "数值范围" in block["content"]["columns"]
    row = block["content"]["rows"][0]
    assert row["数值范围"] == "10–100"


@pytest.mark.asyncio
async def test_data_section_includes_stats_bullet(fakeReportDepsWithStats) -> None:
    """research 模式的 data 段在 chart 后、LLM bullet 前插入 1 个 stats bullet_list 块。"""
    planner, sessionId, turnId = fakeReportDepsWithStats
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    blockTypes = [b["type"] for b in data["blocks"]]
    # chart → stats bullet → LLM bullet
    assert blockTypes == ["chart", "bullet_list", "bullet_list"]
    stats_block = data["blocks"][1]
    assert any("base=J1 (60.0%)" in item for item in stats_block["content"])


# ---------------------------------------------------------------------------
# R3 plan Task D：stats bullet 中文化 + 排序
# ---------------------------------------------------------------------------


@pytest.fixture
async def fakeReportDepsWithSourceTable(dbSession):
    """带 sourceTable 的 finding：让 resolver 命中 OntologyProperty + DISPLAY_VALUE_DICT."""
    svc = ResearchSessionService()
    row = await svc.createSession(dbSession, userId=1, question=QUESTION)
    turn = await svc.appendTurn(
        dbSession, sessionId=row.id, role="user", content={"question": QUESTION}
    )
    stats = {
        "topContributors": [
            {"dim": "site_code", "value": "C1", "sharePct": 60.0, "count": 3},
            {"dim": "site_code", "value": "C2", "sharePct": 20.0, "count": 1},
            {"dim": "site_code", "value": "C3", "sharePct": 20.0, "count": 1},
        ],
        "distribution": {"amt": {"min": 10.0, "max": 100.0, "mean": 52.0, "std": 38.2}},
        "mom": [
            {"period": "2026-01", "value": 100, "deltaPct": 0.0},
            {"period": "2026-02", "value": 115, "deltaPct": 15.0},
        ],
        "anomalies": [{"dim": "amt", "value": 1000, "zScore": 2.5}],
        "numericColumns": ("amt",),
        "dimensionColumns": ("site_code",),
        "rowCount": 5,
    }
    supportingData = {
        "rowCount": 5,
        "error": None,
        "rows": [dict(r) for r in FINDING_ROWS],
        "sourceTable": "dwd_reconciliation_bill_dtl",  # NEW for R3
        "stats": stats,  # also include stats inside supporting_data (matches R2 wiring)
    }
    await svc.saveFinding(
        dbSession,
        sessionId=row.id,
        turnId=turn.id,
        claimText=CLAIM,
        supportingSql="SELECT 1",
        supportingData=supportingData,
        confidence=0.72,
        stats=stats,
    )
    await dbSession.commit()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: FakeLlm(),
        sessionFactory=lambda: dbSession,
    )
    return planner, row.id, turn.id


@pytest.mark.asyncio
async def test_stats_bullet_uses_display_names(fakeReportDepsWithSourceTable) -> None:
    """_formatStatsBullet 中的列名走 resolver；DISPLAY_VALUE_DICT 命中的值走中文."""
    planner, sessionId, turnId = fakeReportDepsWithSourceTable
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    stats_block = next(
        b for b in data["blocks"]
        if b["type"] == "bullet_list" and any("topN" in item for item in b["content"])
    )
    items = stats_block["content"]
    # 1. 列名 site_code 未在 dev DB OntologyProperty → 走 fallback 仍为 site_code
    #    但 anomaly 行含 site_code 之外的 key (amt → fallback)
    # 2. 至少 1 个 bullet 出现 "site_code" 字符串（fallback）or 真实 alias
    # 3. 异常行应存在
    assert any("异常" in item for item in items)


@pytest.mark.asyncio
async def test_stats_bullet_orders_anomaly_first(fakeReportDepsWithSourceTable) -> None:
    """bullet 顺序：异常 → topN → 分布 → 环比."""
    planner, sessionId, turnId = fakeReportDepsWithSourceTable
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    stats_block = next(
        b for b in data["blocks"]
        if b["type"] == "bullet_list" and any("topN" in item for item in b["content"])
    )
    items = stats_block["content"]
    anomaly_idx = next((i for i, item in enumerate(items) if "异常" in item), -1)
    topn_idx = next((i for i, item in enumerate(items) if "维度 topN" in item), -1)
    dist_idx = next((i for i, item in enumerate(items) if "数值分布" in item), -1)
    mom_idx = next((i for i, item in enumerate(items) if "环比" in item), -1)
    # 全部存在
    assert anomaly_idx >= 0 and topn_idx >= 0 and dist_idx >= 0 and mom_idx >= 0
    # 顺序：异常 < topN < 分布 < 环比
    assert anomaly_idx < topn_idx < dist_idx < mom_idx


# ---------------------------------------------------------------------------
# R3 plan Task B：报告接入 chart 渲染（reuse chat-side chart pipeline）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_block_chartType_is_not_none(fakeReportDepsWithStats) -> None:
    """_chartContent 不再 hardcode chartType=None；走 chart_decision 决策引擎.

    注：断言用小写，因为 ChartType 枚举值是 str-enum 小写（前端 VALID_CHART_TYPES
    也是小写白名单）。brief 列了「大写」是契约误导。

    R4-4：原断言「落在 11 种类型之一」等于不断言，收紧到 fixture 形状应得的类型。
    `fakeReportDepsWithStats` 的行是 `[{"month": "2026-01", "cnt": 100}]` ⇒ 1 行 /
    1 指标（`cnt` 数值）/ 0 维度（`month` 被 `_isTimeColumn` 认成时间列，不进
    `dimensions`）⇒ 规则表首个命中的是 `R01_SINGLE_VALUE_KPI`（**无条件**：不读阈值、
    无候选集、`decideChartKind` 不带 labelHint ⇒ 无 LLM 消歧）⇒ 类型确定，不随库/配置漂移。
    """
    planner, sessionId, turnId = fakeReportDepsWithStats
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    chart_block = next(b for b in data["blocks"] if b["type"] == "chart")
    content = chart_block["content"]
    # chartType 不再是 None（也不是字符串 'None'），而是决策引擎给的 kind
    assert content.get("chartType") == "kpi"


@pytest.mark.asyncio
async def test_chart_block_data_field_is_populated(fakeReportDepsWithStats) -> None:
    """chart 块 content 应包含 'data' 键（非 'rows'）且非空."""
    planner, sessionId, turnId = fakeReportDepsWithStats
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart_block = next(
        b for s in payload["sections"] for b in s["blocks"] if b["type"] == "chart"
    )
    content = chart_block["content"]
    assert "data" in content
    # chat-side 决策管道必须返回非空 data 行（fakeReportDepsWithStats 至少 1 行）
    assert isinstance(content["data"], list) and len(content["data"]) > 0
    # 兼容前端契约：columns 也保留
    assert "columns" in content


# ---------------------------------------------------------------------------
# R3 plan Task C：业务语义 prompt 增强（术语 + 分析模板 + 三段式）
# ---------------------------------------------------------------------------

# 术语命中的列：finding 行数据含 site_code，术语字典 mapped_property_name=site_code。
# 注意：语义 context 的 columns 由 **行数据键名** 派生（不是 claimText —— claimText
# 是自然语言句子，拿它当列名永远匹配不上术语）。
TERM_NAME = "基地"
TERM_DEFINITION = "企业经营地理区域单位（如 J1 基地、T2 基地）"
TERM_ROWS = [{"site_code": "C1", "cnt": 100}]

# `_NARRATIVE_SYSTEM` 独有标记（只在改写后的叙述型 system prompt 里）。
# 用来把「叙述型调用」从同一份 compose 里的 `_commentary` 调用中筛出来 —— 否则
# 断言会被 data 段的注释调用顺带满足（双向验证时发现的假绿）。
NARRATIVE_MARKER = "三段式"


def _promptsOf(fake: FakeLlm, marker: str) -> list[str]:
    """筛出 system prompt 含 `marker` 的那些 LLM 调用，返回其拼好的 prompt 文本。"""
    return [
        "\n".join(m.content for m in messages)
        for messages in fake.calls
        if any(marker in m.content for m in messages)
    ]


@pytest.fixture
async def fakeReportDepsWithTermDict(db_session):
    """带 TermDictionary 的 finding：让 report_semantic_context 命中术语.

    走真实 PG 的 `db_session`（非 legacy `dbSession`）—— 需要 seed term_dictionary
    行并断言其渲染结果。

    **自清洁**：term_dictionary 是全套件共享表，残留行若 mapped_property_name 命中
    本次提取出的列名，就会串进 prompt（可能带入数字，破坏 R2 数字免疫不变量）。
    seed 前先删掉同名 term 及命中同样列名的行，保证断言确定性。
    """
    from sqlalchemy import delete

    from app.domain.models import TermDictionary

    termColumns = {k.lower() for k in TERM_ROWS[0]}
    await db_session.execute(
        delete(TermDictionary).where(
            (TermDictionary.term == TERM_NAME)
            | (TermDictionary.mapped_property_name.in_(termColumns))
        )
    )
    db_session.add(
        TermDictionary(
            term=TERM_NAME,
            definition=TERM_DEFINITION,
            mapped_class_name="Site",
            mapped_property_name="site_code",
            formula_hint=None,
            created_by="test",
        )
    )
    await db_session.commit()

    svc = ResearchSessionService()
    row = await svc.createSession(db_session, userId=1, question=QUESTION)
    turn = await svc.appendTurn(
        db_session, sessionId=row.id, role="user", content={"question": QUESTION}
    )
    await svc.saveFinding(
        db_session,
        sessionId=row.id,
        turnId=turn.id,
        claimText=CLAIM,
        supportingSql="SELECT 1",
        supportingData={"rowCount": 1, "error": None, "rows": [dict(r) for r in TERM_ROWS]},
        confidence=0.72,
    )
    await db_session.commit()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: FakeLlm(),
        sessionFactory=lambda: db_session,
    )
    return planner, row.id, turn.id, db_session


@pytest.mark.asyncio
async def test_narrative_prompt_includes_term_dictionary(fakeReportDepsWithTermDict) -> None:
    """executive_summary 段 LLM prompt 注入「维度业务含义」+「分析模板」块.

    断言只看**叙述型调用**（NARRATIVE_MARKER 筛出），并用块头 `## ...`（而非裸词）
    ——`_NARRATIVE_SYSTEM` 正文也含「维度业务含义 / 分析模板」字样，只断言裸词会在
    注入整体失效时依然绿（假绿）。
    """
    planner, sessionId, turnId, _ = fakeReportDepsWithTermDict
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    narrativePrompts = _promptsOf(fake, NARRATIVE_MARKER)
    assert narrativePrompts, "未捕获到叙述型（executive_summary）的 LLM 调用"
    promptText = "\n".join(narrativePrompts)
    # TermDictionary 块：含 '维度业务含义' 段头 + 术语行（行数据键名 site_code 命中）
    assert "## 维度业务含义" in promptText
    assert f"- {TERM_NAME}：" in promptText  # 术语 '基地' 注入
    # 分析模板块
    assert "## 分析模板" in promptText


@pytest.mark.asyncio
async def test_narrative_prompt_requires_three_part_structure(fakeReportDepsWithTermDict) -> None:
    """_NARRATIVE_SYSTEM 改写后要求「数字 + 原因 + 建议」三段式."""
    planner, sessionId, turnId, _ = fakeReportDepsWithTermDict
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    promptText = "\n".join(_promptsOf(fake, NARRATIVE_MARKER))
    # 三段式关键词（「三段式」+ 带序号的「关键数字」只在改写后的 system prompt 里）
    assert "三段式" in promptText
    assert "① 关键数字" in promptText
    assert "原因" in promptText
    assert "建议" in promptText


@pytest.mark.asyncio
async def test_semantic_context_columns_come_from_rows_not_claim_text(
    fakeReportDepsWithTermDict,
) -> None:
    """列名取自 finding 行数据键名（而非 claimText）—— 拿 claimText 当列名永不命中术语.

    反向断言：claimText（自然语言句子）本身不得出现在术语块里。
    """
    planner, sessionId, turnId, _ = fakeReportDepsWithTermDict
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    promptText = "\n".join(_promptsOf(fake, NARRATIVE_MARKER))
    assert "## 维度业务含义" in promptText
    # claimText 是句子，不是列名 —— 它不该被当成 columns 参数喂给术语匹配
    assert f"- {CLAIM}：" not in promptText


@pytest.mark.asyncio
async def test_commentary_prompt_includes_term_dictionary(fakeReportDepsWithTermDict) -> None:
    """data 段的 _commentary prompt 也注入语义 context（同样的 columns 派生规则）."""
    planner, sessionId, turnId, _ = fakeReportDepsWithTermDict
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    # data 段每次 LLM 调用的 user prompt 里都带术语块
    commentaryPrompts = _promptsOf(fake, "关键数字 / 原因 / 建议")
    assert commentaryPrompts, "未捕获到 _commentary 的 LLM 调用"
    assert all("## 维度业务含义" in prompt for prompt in commentaryPrompts)


@pytest.mark.asyncio
async def test_semantic_context_survives_dead_session(fakeReportDepsWithTermDict) -> None:
    """术语加载失败（DB 不可达）只跳过术语段，不炸报告；分析模板段仍在."""
    from app.services.report_semantic_context import assembleSemanticContext

    class _DeadSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("db down")

    context = await assembleSemanticContext(
        _DeadSession(), columns=("site_code",)
    )
    assert "## 分析模板" in context
    assert "## 维度业务含义" not in context


# ---------------------------------------------------------------------------
# R4 plan Task 2：列名/值翻译在真实数据形态（SQL 派生 sourceTable）上生效
# ---------------------------------------------------------------------------


async def _sqlOnlyDeps(db_session, *, rows, supportingSql):
    """R4 核心场景 fixture 构造器：supporting_data **无 sourceTable**，表名只在 SQL 里.

    这是 R4 的核心场景 —— R3 的翻译层在这个形态下完全无效（F1）。

    `stats` **不手搓**，而是对本 fixture 自己的 `rows` 调真实聚合器
    `aggregateRows(rows).asdict()` 派生 —— 与生产写入路径逐字同源：
    `research_agent_phases.py:345` `aggregateRows(rowsForStats)`（无 `dimensionHints`）
    → `:323` `stats=statsObj.asdict()`。因此 `topContributors` 单维度（`dim = dimCols[0]`）、
    `dimensionColumns` 行键序、`distribution.std` 等全部由构造保证保真，两个 fixture
    不可能再漂移（评审第 2 轮 Finding 1/2 即此类手搓漂移）。
    """
    stats = aggregateRows(rows).asdict()
    svc = ResearchSessionService()
    s = await svc.createSession(db_session, userId=1, question="7 月对账确认情况", mode="research")
    turn = await svc.appendTurn(db_session, sessionId=s.id, role="user", content={"q": "x"})
    await svc.saveFinding(
        db_session,
        sessionId=s.id,
        turnId=turn.id,
        claimText="7 月对账确认情况",
        supportingSql=supportingSql,
        # 与生产 findingData(outcome) 同形：仅 rowCount / error / rows
        supportingData={"rowCount": len(rows), "error": None, "rows": rows},
        stats=stats,
        confidence=0.9,
    )
    await db_session.commit()
    fake = FakeLlm()
    planner = ReportPlanner(
        sessionService=svc,
        llmClientFactory=lambda: fake,
        sessionFactory=lambda: db_session,
    )
    return planner, s.id, turn.id


@pytest.fixture
async def fakeReportDepsWithSqlOnly(db_session):
    """维度首选 site_code ⇒ 贡献者全是 site_code（照生产者 `dim = dimCols[0]`）.

    `rows` 行键序把 site_code 放首位 ⇒ `_dimensionColumns`（无 hints）产出
    `["site_code", "confirm_stage"]`，故 topN 贡献者维度是 site_code。
    """
    return await _sqlOnlyDeps(
        db_session,
        supportingSql=(
            "SELECT SITE_CODE, CONFIRM_STAGE, COUNT(*) AS bill_cnt "
            "FROM DWD_RECONCILIATION_BILL_DTL GROUP BY SITE_CODE, CONFIRM_STAGE"
        ),
        rows=[
            {"site_code": "C1", "confirm_stage": "QTY", "bill_cnt": 10},
            {"site_code": "C1", "confirm_stage": "PRICE", "bill_cnt": 5},
        ],
    )


@pytest.fixture
async def fakeReportDepsWithSqlOnlyConfirmStage(db_session):
    """同形态，但维度首选是 confirm_stage ⇒ 贡献者全是 confirm_stage。

    值字典只有在**值**进入 stats 时才可见，而生产者只发 `dimCols[0]` 的贡献者 ——
    故值中文化必须用 confirm_stage 置首的独立 fixture（否则就是生产者发不出的形状）。
    行键序把 confirm_stage 放首位（且是 `GROUP BY` 首列），无 `dimensionHints`
    （见 `research_agent_phases.py:345`）⇒ `dimCols[0] == "confirm_stage"`。
    """
    return await _sqlOnlyDeps(
        db_session,
        supportingSql=(
            "SELECT CONFIRM_STAGE, SITE_CODE, COUNT(*) AS bill_cnt "
            "FROM DWD_RECONCILIATION_BILL_DTL GROUP BY CONFIRM_STAGE, SITE_CODE"
        ),
        rows=[
            {"confirm_stage": "QTY", "site_code": "C1", "bill_cnt": 10},
            {"confirm_stage": "PRICE", "site_code": "C1", "bill_cnt": 5},
        ],
    )


@pytest.mark.asyncio
async def test_stats_bullet_translates_column_names(fakeReportDepsWithSqlOnly) -> None:
    """列名中文化：site_code → 基地，bill_cnt → 账单数（走 SQL 派生的 sourceTable）."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    bullet = next(b for b in data["blocks"] if b["type"] == "bullet_list"
                  and any("维度 topN" in item for item in b["content"]))
    joined = "\n".join(bullet["content"])
    assert "基地=C1" in joined          # dim 译中文 + 值原样（C1 无值字典）
    assert "site_code" not in joined    # 原始列名不再出现
    assert "bill_cnt" not in joined     # 数值分布列名也译中文


@pytest.mark.asyncio
async def test_stats_bullet_translates_values(fakeReportDepsWithSqlOnlyConfirmStage) -> None:
    """值中文化：confirm_stage 的 QTY/PRICE → 数量确认/价格确认（R3 值字典在真实数据上是死的）."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnlyConfirmStage
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    data = next(s for s in payload["sections"] if s["kind"] == "data")
    bullet = next(b for b in data["blocks"] if b["type"] == "bullet_list"
                  and any("维度 topN" in item for item in b["content"]))
    joined = "\n".join(bullet["content"])
    assert "数量确认" in joined or "价格确认" in joined


@pytest.mark.asyncio
async def test_finding_summary_in_llm_prompt_is_translated(fakeReportDepsWithSqlOnly) -> None:
    """喂给 LLM 的摘要里列名/值应是中文（LLM 据此写叙述）——不是 site_code/QTY."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    fake = FakeLlm()
    planner._llmClientFactory = lambda: fake
    await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    sent = _promptsOf(fake, NARRATIVE_MARKER)
    joined = "\n".join(sent)
    assert "site_code" not in joined
    assert "基地" in joined


# ---------------------------------------------------------------------------
# R4 plan Task 4：报告图表标签中文化（报告无 QueryPlan，只能靠 aliasOverrides 注入）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "brief（task-4-brief.md Step 5）钉死的断言在报告路径上不可达，已 RED 实证："
        "fakeReportDepsWithSqlOnly 的 2 行 × 2 维度 ⇒ R13_RAW_DETAIL_TABLE ⇒ "
        "chartType='table'，其 echartsOption 逐字回显 spec.columns + 原始行"
        "（`{'columns': ['bill_cnt','site_code','confirm_stage'], 'rows': [...原始值...]}`）"
        "⇒ 'site_code' 必然在 dumped 里、'基地' 必然不在。"
        "更根本的是：chart_renderer 在任何 kind 下都**不输出维度标签** —— 类目轴装的是"
        "维度**取值**（C1），饼图/热力图/瀑布同理，scatter 只输出指标名；唯一能落到 "
        "echartsOption 的标签是**指标**标签（系列名/KPI label）。故「维度显示名进 option」"
        "只能靠改 chart_renderer（chat 侧共享、本任务禁止），或改 TABLE 负载的 columns"
        "（前端 DataTable 用 columns 当行取值键，会渲染成空表）。"
        "按 Task 3 既定裁定「改 brief 钉死的测试须经人类裁定」，此处只备案、不放宽断言；"
        "可达面上的等价判别由 test_chart_measure_label_is_translated 承担。"
    ),
)
async def test_chart_labels_are_translated(fakeReportDepsWithSqlOnly) -> None:
    """图表轴/图例标签中文化（报告无 QueryPlan，只能靠 aliasOverrides）."""
    planner, sessionId, turnId = fakeReportDepsWithSqlOnly
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart = next(b for s in payload["sections"] if s["kind"] == "data"
                 for b in s["blocks"] if b["type"] == "chart")
    option = chart["content"]["echartsOption"]
    dumped = json.dumps(option, ensure_ascii=False)
    assert "基地" in dumped
    assert "site_code" not in dumped


@pytest.fixture
async def fakeReportDepsWithSqlOnlyOneDim(db_session):
    """单维度形态（1 维度 + 1 指标）⇒ 决策引擎给 BAR，指标标签才进得了 echartsOption.

    两维形态走 R13 明细表，TABLE 负载逐字回显原始列名（前端表格按列名取值，契约冻结），
    「标签中文化」在那条路径上不可观测 —— 这是形态决定的，不是缺陷。报告里真实存在
    单维度聚合（`SELECT SITE_CODE, COUNT(*) ... GROUP BY SITE_CODE`）。
    """
    return await _sqlOnlyDeps(
        db_session,
        supportingSql=(
            "SELECT SITE_CODE, COUNT(*) AS bill_cnt "
            "FROM DWD_RECONCILIATION_BILL_DTL GROUP BY SITE_CODE"
        ),
        rows=[
            {"site_code": "C1", "bill_cnt": 10},
            {"site_code": "C2", "bill_cnt": 5},
        ],
    )


@pytest.mark.asyncio
async def test_chart_measure_label_is_translated(fakeReportDepsWithSqlOnlyOneDim) -> None:
    """报告图表的中文标签真的渲染进 echartsOption（可达面上的端到端判别）.

    BAR 的系列名取自指标标签（图例），这正是报告路径此前显示英文列名的位置。
    """
    planner, sessionId, turnId = fakeReportDepsWithSqlOnlyOneDim
    payload, _ = await planner.compose(None, sessionId=sessionId, turnId=turnId, mode="research")
    chart = next(
        b for s in payload["sections"] if s["kind"] == "data"
        for b in s["blocks"] if b["type"] == "chart"
    )
    assert chart["content"]["chartType"] == "bar"
    dumped = json.dumps(chart["content"]["echartsOption"], ensure_ascii=False)
    assert "账单数" in dumped          # 指标标签中文化（系列名 = 图例）
    assert "bill_cnt" not in dumped    # 原始列名不再出现
    assert "site_code" not in dumped   # 维度列名也不再出现（类目轴装的是取值 C1/C2）
