"""研究报告装配（feat-research-entry Task 6）。

三 mode（research / attribution / compare）段落模板 + 单块 LLM 文本 + 纯字符串 MD
渲染。**归档（publishReport）留在调用方** `ResearchAgentService._stageReport`：本类
是 `Reporter` 端口的唯一实现（Task 5 占位 `DefaultReporter` 已删，见 task-6-report.md
「偏差 2」），只做装配。

**数据来源（与设计 §4.7 的偏差，见 task-6-report.md）**：设计写「读 step_results」，
但步结果只活在 `ResearchAgentService` 的内存 state 里（5 张 research 表无 step 表，
本任务禁造新表）。故 chart/table 块的数据值只取自**持久化输入**：

- `research_finding.supporting_data["rows"]`（Task 5 verify 阶段落库的验证行数据）
  → chart 块的 `content.rows` 与各 table 块的行，**逐字透传**；
- `research_checkpoint.options["arms"]`（ESL 三臂）→ knowledge 段引用的 wiki 条目与
  methodology 段的语义范围；
- `research_session.input_seed` → 报告问题与标题。

**数字免疫（结构上，不靠渲染期 diff）**：LLM 文本块只收到 `finding.claim_text` /
`step.summary` 类的**摘要字符串**，prompt 里没有任何原始行；原始数字只出现在
chart/table 块的 content 里。LLM 即便改写文字，也改不到数字。

**计量**：LLM 一律经调用方传入的 `llmClient` 调用，本模块不记账 —— 计量在
`ResearchAgentService` 的客户端边界（`MeteredClient`）完成，避免双计（核心约束 #3）。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.research_models import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchSession,
    ResearchTurn,
)
from app.infrastructure.llm.base_client import LlmMessage
from app.services.chart_integration import buildReportChart
from app.services.display_name_resolver import (
    deriveSourceTable,
    resolveColumnName,
    resolveValueName,
)
from app.services.learning.prompt_fence import neutralizeFence
from app.services.llm_json_fence import stripJsonFence
from app.services.report_renderer import (
    _SECTION_TITLES,
    BLOCK_BULLET,
    BLOCK_CHART,
    BLOCK_TABLE,
    BLOCK_TEXT,
    CHART_HINT,  # noqa: F401  re-export（表示层常量，planner 侧暂未直接引用）
    DEFAULT_TITLE,
    REF_FINDING,
    REF_WIKI,
    ReportBlock,
    ReportSection,
    SourceRef,
    blockToDict,  # noqa: F401  re-export（表示层序列化，planner 侧暂未直接引用）
    renderMarkdown,
    sectionToDict,
)
from app.services.report_semantic_context import assembleSemanticContext
from app.services.research_agent_ports import DEFAULT_MODE, FINDING_ROWS_KEY, OPT_ARMS
from app.services.research_session_service import ResearchSessionService

logger = logging.getLogger(__name__)

__all__ = ["ReportBlock", "ReportPlanner", "ReportSection", "SourceRef", "renderMarkdown"]

# --- mode 模板（设计 §4.7 段落顺序表）----------------------------------------
MODE_RESEARCH = DEFAULT_MODE
MODE_ATTRIBUTION = "attribution"
MODE_COMPARE = "compare"

_MODE_SECTIONS: dict[str, tuple[str, ...]] = {
    MODE_RESEARCH: ("executive_summary", "data", "knowledge", "methodology"),
    MODE_ATTRIBUTION: ("conclusion", "hypothesis_table", "data", "alternative", "methodology"),
    MODE_COMPARE: ("comparison_table", "data", "diff_analysis", "methodology"),
}

# --- 文案 / 上限 --------------------------------------------------------------
LLM_FALLBACK_NOTICE = "（自动摘要生成失败，以下为结构化摘要）"
EMPTY_FINDING_TEXT = "（本轮无已验证结论）"
EMPTY_KNOWLEDGE_TEXT = "（本轮未引用知识条目）"
EMPTY_ALTERNATIVE_TEXT = "（本轮无未验证备选假设）"

MAX_REPORT_FINDINGS = 10  # 报告纳入的结论条数上限（同时限定 LLM 调用次数）
MAX_BULLETS = 6
MAX_BULLET_CHARS = 200
MAX_KNOWLEDGE_REFS = 5
SOURCE_LABEL_LIMIT = 60

# --- LLM 提示词（只喂摘要字符串，绝不喂原始行）---------------------------------
# R3-C：system prompt 增加「维度业务含义 / 分析模板」输入声明 + 三段式输出要求；
# 实际语义 context 由 `report_semantic_context.assembleSemanticContext` 组装成
# user_content 的独立 part（本常量不渲染任何数据）。
_NARRATIVE_SYSTEM = (
    "你是企业数据分析师。基于 <user_content> 中给出的统计摘要（topN / 分布 / 环比 / 异常）"
    "+ 维度业务含义 + 分析模板，写一段{intent}。\n"
    "输出三段式：① 关键数字（可引用摘要中明确给出的数字） ② 原因解读（为什么是这样） "
    "③ 后续建议（业务上应该关注什么）。\n"
    "不得编造摘要未给出的数字、不得编造数据；用中文，不超过 300 字。"
)
_NARRATIVE_INTENTS: dict[str, str] = {
    "executive_summary": "执行摘要（总体结论 + 关键发现）",
    "conclusion": "结论（领先假设及其置信度，并点明尚未验证的假设）",
    "diff_analysis": "差异分析（各结论之间的一致与分歧）",
}
_COMMENTARY_SYSTEM = (
    "你是企业数据分析师。基于 <user_content> 中给出的一条结论及其统计摘要 + 维度业务含义，"
    "写 1-3 条要点，每条一行、以短横线开头；每条可包含「关键数字 / 原因 / 建议」任一部分；"
    "可以引用摘要中明确给出的数字，但不得编造未给出的数字；用中文。"
)


# ---------------------------------------------------------------------------
# 装配上下文（frozen dataclass）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingView:
    """一条结论的只读视图；`rows` 为 supporting_data 行数据的**逐字**引用。"""

    findingId: str
    claimText: str
    confidence: float
    sql: str | None
    rows: list[dict[str, Any]]
    rowCount: int
    error: str | None
    stats: dict[str, Any] | None = None  # NEW
    sourceTable: str | None = None  # NEW (R3)

    @property
    def verified(self) -> bool:
        """验证是否成功（Task 5 verify 阶段把失败原因写进 supporting_data["error"]）。"""
        return self.error is None


@dataclass(frozen=True)
class _Context:
    """装配上下文：全部来自持久化输入（session / finding / checkpoint options）。"""

    question: str
    title: str
    mode: str
    findings: tuple[FindingView, ...]
    arms: dict[str, Any]


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------


def _findingView(row: ResearchFinding) -> FindingView:
    """ORM 行 → 只读视图；行数据逐字透传（不排序、不截断、不改写）。"""
    data = row.supporting_data or {}
    rows = [item for item in (data.get(FINDING_ROWS_KEY) or []) if isinstance(item, dict)]
    return FindingView(
        findingId=str(row.id),
        claimText=row.claim_text,
        confidence=round(float(row.confidence or 0), 4),
        sql=row.supporting_sql,
        rows=rows,
        rowCount=int(data.get("rowCount") or len(rows)),
        error=data.get("error"),
        stats=data.get("stats"),  # NEW: pull from supporting_data["stats"] if present
        # R4：R3 从未写入 supporting_data["sourceTable"]（真实会话实测只有
        # rows/error/stats/rowCount），故缺失时从支撑 SQL 的 FROM 子句派生 ——
        # 这是唯一能同时修复历史会话与新增会话的读侧方案。
        sourceTable=data.get("sourceTable") or deriveSourceTable(row.supporting_sql),
    )


def _findingRef(view: FindingView) -> SourceRef:
    return SourceRef(
        kind=REF_FINDING, refId=view.findingId, label=view.claimText[:SOURCE_LABEL_LIMIT]
    )


async def _findingSummary(view: FindingView, *, db: Any = None) -> str:
    """结论摘要（喂 LLM 的唯一数据形态：不含任何原始行，但可引用 stats 数字）。

    R4：top1 的列名/值走翻译 —— LLM 看到「按 基地 分，C1 占 100%」才会用中文写叙述。
    """
    state = "已验证" if view.verified else f"未验证：{view.error}"
    base = f"{view.claimText}（置信度 {view.confidence}，{state}，{view.rowCount} 行）"
    if not view.stats or not view.stats.get("topContributors"):
        return base
    top1 = view.stats["topContributors"][0]
    rawDim = str(top1.get("dim") or "")
    dimLabel = await resolveColumnName(db, view.sourceTable, rawDim)
    valueLabel = resolveValueName(view.sourceTable, rawDim, top1.get("value"))
    sharePct = float(top1.get("sharePct") or 0.0)
    return f"{base}；按 {dimLabel} 分，{valueLabel} 占 {sharePct}%"


async def _topContributorLabel(
    stats: dict[str, Any] | None, *, sourceTable: str | None = None, db: Any = None
) -> str:
    """top1 贡献者标签：「基地=C1 (60.0%)」；无 stats → 「—」。"""
    if not stats:
        return "—"
    top = stats.get("topContributors") or []
    if not top:
        return "—"
    top1 = top[0]
    rawDim = str(top1.get("dim") or "")
    dim = await resolveColumnName(db, sourceTable, rawDim)
    value = resolveValueName(sourceTable, rawDim, top1.get("value"))
    return f"{dim}={value} ({float(top1.get('sharePct') or 0):.1f}%)"


async def _findingDigest(ctx: _Context, *, db: Any = None) -> str:
    views = ctx.findings[:MAX_REPORT_FINDINGS]
    if not views:
        return EMPTY_FINDING_TEXT
    lines = [f"- {await _findingSummary(view, db=db)}" for view in views]
    return "\n".join(lines)


def _rowColumns(rows: list[dict[str, Any]]) -> tuple[str, ...]:
    """行数据 → 小写去重列名元组（`assembleSemanticContext` 的 columns 参数）。

    列名只取**键名**（值永不进 prompt），且必须来自 `rows` —— `claimText` 是自然
    语言句子，拿它当列名永远匹配不上术语字典的 `mapped_property_name`。
    """
    if not rows:
        return ()
    return tuple(sorted({key.lower() for key in rows[0]}))


def _semanticColumns(ctx: _Context) -> tuple[str, ...]:
    """报告级语义 context 的列名：各 finding 行数据键名的并集（排序保证确定性）。"""
    columnSet: set[str] = set()
    for view in ctx.findings[:MAX_REPORT_FINDINGS]:
        columnSet.update(_rowColumns(view.rows))
    return tuple(sorted(columnSet))


async def _degradedText(ctx: _Context, *, db: Any = None) -> str:
    """LLM 降级文案：显式说明 + 结构化摘要列表（brief 指定文案）。"""
    digest = await _findingDigest(ctx, db=db)
    return "\n".join([LLM_FALLBACK_NOTICE, *_bulletItems(digest)])


def _bulletItems(text: str) -> list[str]:
    items = [line.strip().lstrip("-•* ").strip() for line in text.splitlines()]
    return [item for item in items if item]


def _splitBullets(text: str) -> list[str]:
    return [item[:MAX_BULLET_CHARS] for item in _bulletItems(text)][:MAX_BULLETS]


async def _formatStatsBullet(
    stats: dict[str, Any],
    *,
    sourceTable: str | None = None,
    db: Any = None,
) -> list[str]:
    """stats → bullet 行（确定性、不经 LLM）。

    顺序：异常 → topN → 分布 → 环比。
    列名走 resolveColumnName（本体别名 → 代码内列名字典 → 原列名），
    值走 resolveValueName（代码内值字典）。R4 前两者在真实数据上都是死的。
    """
    lines: list[str] = []
    # 1. 异常（最优先）
    anomalies = stats.get("anomalies") or []
    if anomalies:
        first = anomalies[0]
        dim = await resolveColumnName(db, sourceTable, str(first.get("dim") or ""))
        lines.append(f"- 异常：{dim} z={float(first.get('zScore', 0)):.2f}")
    # 2. 维度 topN
    top = stats.get("topContributors") or []
    if top:
        parts: list[str] = []
        for item in top[:3]:
            rawDim = str(item.get("dim") or "")
            dim = await resolveColumnName(db, sourceTable, rawDim)
            value = resolveValueName(sourceTable, rawDim, item.get("value"))
            parts.append(f"{dim}={value} ({float(item.get('sharePct') or 0):.1f}%)")
        lines.append(f"- 维度 topN：{', '.join(parts)}")
    # 3. 数值分布
    dist = stats.get("distribution") or {}
    if dist:
        firstCol, firstStats = next(iter(dist.items()), (None, None))
        if firstCol and firstStats:
            col = await resolveColumnName(db, sourceTable, firstCol)
            lines.append(
                f"- 数值分布：{col} "
                f"min={firstStats.get('min', 0):g}, "
                f"max={firstStats.get('max', 0):g}, "
                f"mean={firstStats.get('mean', 0):.1f}, "
                f"std={firstStats.get('std', 0):.1f}"
            )
    # 4. 环比
    mom = stats.get("mom") or []
    if mom:
        firstMom = mom[0]
        lines.append(
            f"- 环比：{firstMom.get('period', '')}→{mom[1].get('period', '')} "
            f"delta={firstMom.get('deltaPct', 0):+.1f}%" if len(mom) >= 2
            else f"- 环比：{firstMom.get('period', '')}（仅 1 期）"
        )
    return lines


def _numericRangeLabel(stats: dict[str, Any] | None) -> str:
    """第一个数值列的 min-max：「10–100」；无 stats → 「—」。"""
    if not stats:
        return "—"
    dist = stats.get("distribution") or {}
    if not dist:
        return "—"
    firstCol, firstStats = next(iter(dist.items()), (None, None))
    if not firstCol or not firstStats:
        return "—"
    lo = float(firstStats.get("min", 0))
    hi = float(firstStats.get("max", 0))
    return f"{lo:g}–{hi:g}"


def _tableBlock(columns: list[str], rows: list[dict[str, Any]], refs: tuple[SourceRef, ...] = ()) -> ReportBlock:
    return ReportBlock(BLOCK_TABLE, {"columns": list(columns), "rows": rows}, refs)


async def _chartContent(view: FindingView, *, session: AsyncSession) -> dict[str, Any]:
    """chart 块 content：行数据逐字来自 finding.supporting_data["rows"].

    R3 接入 chat-side chart 链路（chart_decision + spec_builder + renderer）：
    - plan=None（报告无 QueryPlan，决策引擎按数据形状+问句派生）；
    - 失败降级为 TABLE，绝不抛错（不让整份报告炸）。

    R4：报告无 QueryPlan ⇒ 自建 aliasOverrides（列名 → 中文），否则图表轴/图例
    只能是英文列名（`_aliasByField(plan=None)` 恒返 `{}`）。
    只注入**真的解析出显示名**的列：`resolveColumnName` 未命中时返回原列名（identity），
    而标签构造是 `aliases.get(f) or _prettify(f)` —— identity 也 truthy，会把
    `receipt_qty` 短路成原始列名，连 pre-R4 的 `receipt qty` 都丢掉。
    """
    if not view.rows:
        return {
            "title": view.claimText or "（无数据）",
            "chartType": "table",
            "columns": list(view.rows[0].keys()) if view.rows else [],
            "data": [],
            "echartsOption": None,
        }
    columns = list(view.rows[0].keys())
    resolved = [
        (column, await resolveColumnName(session, view.sourceTable, column))
        for column in columns
    ]
    aliasOverrides = {column: name for column, name in resolved if name != column}
    return await buildReportChart(
        session,
        rows=view.rows,
        columns=columns,
        question=view.claimText or "",
        aliasOverrides=aliasOverrides,
    )


def _names(items: Any, key: str) -> str:
    values = [str(item.get(key)) for item in (items or []) if isinstance(item, dict) and item.get(key)]
    return "、".join(values)


# ---------------------------------------------------------------------------
# ReportPlanner
# ---------------------------------------------------------------------------


class ReportPlanner:
    """三 mode 报告装配（无状态；构造注入协作者）。

    `sessionFactory` 只在 `compose(session=None)` 时兜底（测试夹具用；生产恒传真
    session）。归档（publishReport）由调用方 `_stageReport` 完成，本类只返回
    `(payload, rendered_md)` —— 满足 `Reporter` 协议，测试 fake 可互换。
    """

    def __init__(
        self,
        *,
        sessionService: ResearchSessionService,
        llmClientFactory: Callable[[], Any] | None = None,
        sessionFactory: Callable[[], AsyncSession] | None = None,
    ) -> None:
        self._sessions = sessionService
        self._llmClientFactory = llmClientFactory
        self._sessionFactory = sessionFactory
        self._builders: dict[str, Any] = {
            "executive_summary": self._executiveSummary,
            "conclusion": self._conclusion,
            "diff_analysis": self._diffAnalysis,
            "data": self._dataSection,
            "knowledge": self._knowledgeSection,
            "methodology": self._methodologySection,
            "hypothesis_table": self._hypothesisTable,
            "alternative": self._alternativeSection,
            "comparison_table": self._comparisonTable,
        }

    async def compose(
        self,
        session: AsyncSession | None = None,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        mode: str = DEFAULT_MODE,
        llmClient: Any = None,
    ) -> tuple[dict[str, Any], str]:
        """装配报告：`(payload, renderedMd)`；未知 mode 抛 ValueError。"""
        kinds = _MODE_SECTIONS.get(mode)
        if kinds is None:
            logger.warning("未知报告 mode: %s", mode)
            raise ValueError(f"未知报告 mode {mode}，合法值: {sorted(_MODE_SECTIONS)}")
        active = session if session is not None else self._fallbackSession()
        ctx = await self._collectContext(active, sessionId=sessionId, mode=mode)
        client = llmClient if llmClient is not None else self._createClient()
        sections = tuple([await self._buildSection(kind, ctx, client=client, session=active) for kind in kinds])
        payload = self._payload(sessionId=sessionId, turnId=turnId, ctx=ctx, sections=sections)
        return payload, renderMarkdown(payload)

    # ------------------------------------------------------------------
    # 数据收集（只读持久化输入）
    # ------------------------------------------------------------------

    def _fallbackSession(self) -> AsyncSession:
        if self._sessionFactory is None:
            logger.error("报告装配缺少 AsyncSession，且未注入 sessionFactory")
            raise ValueError("ReportPlanner 需要 AsyncSession（或构造时注入 sessionFactory）")
        return self._sessionFactory()

    def _createClient(self) -> Any:
        """按注入的 factory 取 LLM 客户端；取不到返回 None（文本块降级为模板）。"""
        if self._llmClientFactory is None:
            return None
        try:
            return self._llmClientFactory()
        except Exception:  # noqa: BLE001 —— 客户端创建失败降级为无 LLM，不中断报告
            logger.warning("报告 LLM 客户端创建失败，文本块降级为结构化摘要", exc_info=True)
            return None

    async def _collectContext(
        self, session: AsyncSession, *, sessionId: uuid.UUID, mode: str
    ) -> _Context:
        row = await session.get(ResearchSession, sessionId)
        if row is None:
            logger.warning("报告装配：研究会话不存在 id=%s", sessionId)
            raise ValueError(f"研究会话不存在: {sessionId}")
        findings = tuple([_findingView(item) for item in await self._loadFindings(session, sessionId)])
        return _Context(
            question=row.input_seed or row.title or "",
            title=row.title or row.input_seed or DEFAULT_TITLE,
            mode=mode,
            findings=findings,
            arms=await self._loadArms(session, sessionId),
        )

    async def _loadFindings(
        self, session: AsyncSession, sessionId: uuid.UUID
    ) -> list[ResearchFinding]:
        """结论经 `ResearchSessionService` 读取（读侧唯一入口，见该方法的 docstring）。"""
        return await self._sessions.listFindings(session, sessionId)

    async def _loadArms(self, session: AsyncSession, sessionId: uuid.UUID) -> dict[str, Any]:
        """ESL 三臂：唯一持久化载体是 checkpoint options（Task 5 每个 checkpoint 都带）。

        读失败只降级为空臂（knowledge/methodology 段缺省），但**留痕**不静默；
        用 savepoint 隔离，避免污染调用方事务里已挂起的写入。
        """
        try:
            async with session.begin_nested():
                optionsRows = list(
                    await session.scalars(
                        select(ResearchCheckpoint.options)
                        .join(ResearchTurn, ResearchTurn.id == ResearchCheckpoint.turn_id)
                        .where(ResearchCheckpoint.session_id == sessionId)
                        .order_by(ResearchTurn.turn_index.asc(), ResearchCheckpoint.id.asc())
                    )
                )
        except Exception:  # noqa: BLE001 —— 三臂只是报告装饰，读失败不该炸掉整份报告
            logger.warning("报告装配：ESL 三臂读取失败，knowledge 段降级为空", exc_info=True)
            return {}
        arms: dict[str, Any] = {}
        for options in optionsRows:
            candidate = (options or {}).get(OPT_ARMS)
            if candidate:
                arms = candidate
        return arms

    # ------------------------------------------------------------------
    # 段落分派与装配
    # ------------------------------------------------------------------

    async def _buildSection(
        self, kind: str, ctx: _Context, *, client: Any, session: AsyncSession
    ) -> ReportSection:
        builder = self._builders.get(kind)
        if builder is None:
            logger.error("未知报告段落 kind: %s", kind)
            raise ValueError(f"未知报告段落: {kind}")
        return await builder(ctx, client=client, session=session)

    def _textSection(
        self, kind: str, content: str, refs: tuple[SourceRef, ...] = ()
    ) -> ReportSection:
        block = ReportBlock(BLOCK_TEXT, content, refs)
        return ReportSection(kind, kind, _SECTION_TITLES[kind], (block,))

    def _payload(
        self,
        *,
        sessionId: uuid.UUID,
        turnId: uuid.UUID,
        ctx: _Context,
        sections: tuple[ReportSection, ...],
    ) -> dict[str, Any]:
        return {
            "sessionId": str(sessionId),
            "turnId": str(turnId),
            "mode": ctx.mode,
            "title": ctx.title,
            "question": ctx.question,
            "sections": [sectionToDict(section) for section in sections],
            "findingsRef": [
                {
                    "findingId": view.findingId,
                    "claim": view.claimText,
                    "confidence": view.confidence,
                    "verified": view.verified,
                }
                for view in ctx.findings
            ],
        }

    async def _narrativeSection(
        self, kind: str, ctx: _Context, *, client: Any, session: AsyncSession | None = None
    ) -> ReportSection:
        """叙述型 text 段（执行摘要 / 结论 / 差异分析）：单次 LLM，失败降级模板.

        R3-C：prompt 追加语义 context（术语 + 分析模板）part。columns 取自 finding
        行数据键名并集（`_semanticColumns`），不是 claimText。

        `session` 来自 `_buildSection` 透传；为 None（旧测试夹具直接调本方法）时
        `assembleSemanticContext` 内部降级为「仅模板段」。
        """
        refs = tuple([_findingRef(view) for view in ctx.findings[:MAX_REPORT_FINDINGS]])
        semanticCtx = await assembleSemanticContext(session, columns=_semanticColumns(ctx))
        digest = await _findingDigest(ctx, db=session)
        text = await self._llmText(
            client,
            system=_NARRATIVE_SYSTEM.format(intent=_NARRATIVE_INTENTS[kind]),
            parts=[ctx.question, digest, semanticCtx],
        )
        return self._textSection(kind, text or await _degradedText(ctx, db=session), refs)

    async def _executiveSummary(self, ctx: _Context, *, client: Any, session: AsyncSession | None = None) -> ReportSection:
        return await self._narrativeSection("executive_summary", ctx, client=client, session=session)

    async def _conclusion(self, ctx: _Context, *, client: Any, session: AsyncSession | None = None) -> ReportSection:
        return await self._narrativeSection("conclusion", ctx, client=client, session=session)

    async def _diffAnalysis(self, ctx: _Context, *, client: Any, session: AsyncSession | None = None) -> ReportSection:
        return await self._narrativeSection("diff_analysis", ctx, client=client, session=session)

    async def _dataSection(self, ctx: _Context, *, client: Any, session: AsyncSession) -> ReportSection:
        """数据章节：每条结论「chart（行数据逐字）+ stats bullet（确定性摘要）+ LLM bullet（解读）」."""
        blocks: list[ReportBlock] = []
        for view in ctx.findings[:MAX_REPORT_FINDINGS]:
            ref = _findingRef(view)
            if view.rows:
                chartContent = await _chartContent(view, session=session)
                blocks.append(ReportBlock(BLOCK_CHART, chartContent, (ref,)))
            if view.stats:
                statsLines = await _formatStatsBullet(
                    view.stats, sourceTable=view.sourceTable, db=session
                )
                if statsLines:
                    blocks.append(ReportBlock(BLOCK_BULLET, statsLines, (ref,)))
            bullets = await self._commentary(view, ctx, client=client, session=session)
            blocks.append(ReportBlock(BLOCK_BULLET, bullets, (ref,)))
        if not blocks:
            blocks.append(ReportBlock(BLOCK_TEXT, EMPTY_FINDING_TEXT, ()))
        return ReportSection("data", "data", _SECTION_TITLES["data"], tuple(blocks))

    async def _commentary(
        self, view: FindingView, ctx: _Context, *, client: Any, session: AsyncSession | None = None
    ) -> list[str]:
        """单 finding 单次 LLM 调用（设计 §4.7 约束）；失败降级为该结论的结构化摘要.

        R3-C：prompt 追加语义 context（术语取自本 finding 行数据的列名）。
        """
        semanticCtx = await assembleSemanticContext(session, columns=_rowColumns(view.rows))
        summary = await _findingSummary(view, db=session)
        text = await self._llmText(
            client,
            system=_COMMENTARY_SYSTEM,
            parts=[ctx.question, summary, semanticCtx],
        )
        return _splitBullets(text) if text else [summary]

    async def _llmText(self, client: Any, *, system: str, parts: list[str]) -> str | None:
        """单块文本生成；无客户端 / 调用异常 → None（调用方降级，且降级文案可见）。"""
        if client is None:
            return None
        userPrompt = "<user_content>\n" + "\n".join(neutralizeFence(part) for part in parts)
        userPrompt += "\n</user_content>"
        try:
            response = await client.complete(
                [LlmMessage(role="system", content=system), LlmMessage(role="user", content=userPrompt)]
            )
        except Exception:  # noqa: BLE001 —— LLM 失败降级为结构化摘要（显式文案，不静默）
            logger.warning("报告文本块 LLM 生成失败，降级为结构化摘要", exc_info=True)
            return None
        text = stripJsonFence(str(getattr(response, "content", "") or "")).strip()
        return text or None

    async def _knowledgeSection(self, ctx: _Context, *, client: Any = None, session: AsyncSession | None = None) -> ReportSection:
        """引用知识：ESL knowledge 臂命中的 wiki 条目（checkpoint options 持久化）。"""
        items = [item for item in (ctx.arms.get("knowledge") or []) if isinstance(item, dict)]
        if not items:
            return self._textSection("knowledge", EMPTY_KNOWLEDGE_TEXT)
        picked = items[:MAX_KNOWLEDGE_REFS]
        refs = tuple(
            SourceRef(
                kind=REF_WIKI,
                refId=str(item.get("pageId") or ""),
                label=str(item.get("title") or "")[:SOURCE_LABEL_LIMIT],
            )
            for item in picked
        )
        bullets = [
            f"{item.get('title') or ''}：{str(item.get('snippet') or '')[:MAX_BULLET_CHARS]}"
            for item in picked
        ]
        return ReportSection(
            "knowledge", "knowledge", _SECTION_TITLES["knowledge"], (ReportBlock(BLOCK_BULLET, bullets, refs),)
        )

    async def _methodologySection(self, ctx: _Context, *, client: Any = None, session: AsyncSession | None = None) -> ReportSection:
        """方法学：确定性文本（零 LLM）——口径与数据来源必须逐字可控。"""
        arms = ctx.arms
        lines = [
            f"研究模式：{ctx.mode}；研究问题：{ctx.question}",
            f"数据来源：{len(ctx.findings)} 条结论（research_finding.supporting_data 的行数据）",
            f"语义范围：指标 {_names(arms.get('metrics'), 'kpiCode') or '（无）'}；"
            f"本体 {_names(arms.get('businessObjects'), 'className') or '（无）'}；"
            f"知识 {len(arms.get('knowledge') or [])} 条",
            "口径：叙述文字由 LLM 生成；chart/table 块的数值逐字取自结论数据，不经 LLM 改写。",
        ]
        refs = tuple([_findingRef(view) for view in ctx.findings[:MAX_REPORT_FINDINGS]])
        return self._textSection("methodology", "\n".join(lines), refs)

    async def _hypothesisTable(self, ctx: _Context, *, client: Any = None, session: AsyncSession | None = None) -> ReportSection:
        views = ctx.findings[:MAX_REPORT_FINDINGS]
        if not views:
            return self._textSection("hypothesis_table", EMPTY_FINDING_TEXT)
        columns = ["假设", "置信度", "已验证", "数据行数", "主要贡献者"]  # NEW: 主要贡献者
        rows: list[dict[str, Any]] = []
        for view in views:
            contributor = await _topContributorLabel(
                view.stats, sourceTable=view.sourceTable, db=session
            )
            rows.append(
                {
                    "假设": view.claimText,
                    "置信度": view.confidence,
                    "已验证": "是" if view.verified else "否",
                    "数据行数": view.rowCount,
                    "主要贡献者": contributor,  # NEW
                }
            )
        block = _tableBlock(columns, rows, tuple([_findingRef(view) for view in views]))
        return ReportSection(
            "hypothesis_table", "hypothesis_table", _SECTION_TITLES["hypothesis_table"], (block,)
        )

    async def _alternativeSection(self, ctx: _Context, *, client: Any = None, session: AsyncSession | None = None) -> ReportSection:
        """备选假设：验证未通过的结论（supporting_data["error"] 非空）。"""
        pending = [view for view in ctx.findings if not view.verified][:MAX_REPORT_FINDINGS]
        if not pending:
            return self._textSection("alternative", EMPTY_ALTERNATIVE_TEXT)
        refs = tuple([_findingRef(view) for view in pending])
        bullets = [await _findingSummary(view, db=session) for view in pending]
        return ReportSection(
            "alternative", "alternative", _SECTION_TITLES["alternative"], (ReportBlock(BLOCK_BULLET, bullets, refs),)
        )

    async def _comparisonTable(self, ctx: _Context, *, client: Any = None, session: AsyncSession | None = None) -> ReportSection:
        views = ctx.findings[:MAX_REPORT_FINDINGS]
        if not views:
            return self._textSection("comparison_table", EMPTY_FINDING_TEXT)
        columns = ["结论", "置信度", "数据行数", "支撑 SQL", "数值范围"]  # NEW: 数值范围
        rows = [
            {
                "结论": view.claimText,
                "置信度": view.confidence,
                "数据行数": view.rowCount,
                "支撑 SQL": view.sql or "",
                "数值范围": _numericRangeLabel(view.stats),  # NEW
            }
            for view in views
        ]
        block = _tableBlock(columns, rows, tuple([_findingRef(view) for view in views]))
        return ReportSection(
            "comparison_table", "comparison_table", _SECTION_TITLES["comparison_table"], (block,)
        )
