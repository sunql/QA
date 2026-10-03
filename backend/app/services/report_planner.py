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
from app.services.learning.prompt_fence import neutralizeFence
from app.services.llm_json_fence import stripJsonFence
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

_SECTION_TITLES: dict[str, str] = {
    "executive_summary": "执行摘要",
    "conclusion": "结论",
    "data": "数据章节",
    "knowledge": "引用知识",
    "methodology": "方法学",
    "hypothesis_table": "假设验证表",
    "alternative": "备选假设",
    "comparison_table": "对比维度表",
    "diff_analysis": "差异分析",
}

# --- 块类型 / SourceRef 类型 --------------------------------------------------
BLOCK_TEXT = "text"
BLOCK_CHART = "chart"
BLOCK_TABLE = "table"
BLOCK_BULLET = "bullet_list"
REF_FINDING = "finding"
REF_WIKI = "wiki"

# --- 文案 / 上限 --------------------------------------------------------------
LLM_FALLBACK_NOTICE = "（自动摘要生成失败，以下为结构化摘要）"
EMPTY_FINDING_TEXT = "（本轮无已验证结论）"
EMPTY_KNOWLEDGE_TEXT = "（本轮未引用知识条目）"
EMPTY_ALTERNATIVE_TEXT = "（本轮无未验证备选假设）"
DEFAULT_TITLE = "研究报告"
CHART_HINT = "图表见 payload"

MAX_REPORT_FINDINGS = 10  # 报告纳入的结论条数上限（同时限定 LLM 调用次数）
MAX_BULLETS = 6
MAX_BULLET_CHARS = 200
MAX_KNOWLEDGE_REFS = 5
SOURCE_LABEL_LIMIT = 60

# --- LLM 提示词（只喂摘要字符串，绝不喂原始行）---------------------------------
_NARRATIVE_SYSTEM = (
    "你是企业数据分析师。仅依据 <user_content> 中给出的结论条目写一段{intent}；"
    "不得引入未给出的数字，不得编造数据；用中文，不超过 200 字。"
)
_NARRATIVE_INTENTS: dict[str, str] = {
    "executive_summary": "执行摘要（总体结论 + 关键发现）",
    "conclusion": "结论（领先假设及其置信度，并点明尚未验证的假设）",
    "diff_analysis": "差异分析（各结论之间的一致与分歧）",
}
_COMMENTARY_SYSTEM = (
    "你是企业数据分析师。仅依据 <user_content> 中的一条结论写 1-3 条要点，"
    "每条一行、以短横线开头；不得引入未给出的数字，不得编造数据；用中文。"
)


# ---------------------------------------------------------------------------
# 报告形状（设计 §4.7 段落形状；frozen dataclass，序列化 camelCase 键）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """块级溯源：结论 / 知识条目（设计 §4.7 kind: step | finding | wiki | kpi）。"""

    kind: str
    refId: str
    label: str


@dataclass(frozen=True)
class ReportBlock:
    """段落内的一个块：text | chart | table | bullet_list。"""

    type: str
    content: Any
    sourceRefs: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class ReportSection:
    """报告段落（id/kind 同值，便于前端按 kind 取段）。"""

    id: str
    kind: str
    title: str
    blocks: tuple[ReportBlock, ...] = ()


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
# 序列化 / 取值助手
# ---------------------------------------------------------------------------


def _blockToDict(block: ReportBlock) -> dict[str, Any]:
    return {
        "type": block.type,
        "content": block.content,
        "sourceRefs": [
            {"kind": ref.kind, "refId": ref.refId, "label": ref.label} for ref in block.sourceRefs
        ],
    }


def _sectionToDict(section: ReportSection) -> dict[str, Any]:
    return {
        "id": section.id,
        "kind": section.kind,
        "title": section.title,
        "blocks": [_blockToDict(block) for block in section.blocks],
    }


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
    )


def _findingRef(view: FindingView) -> SourceRef:
    return SourceRef(
        kind=REF_FINDING, refId=view.findingId, label=view.claimText[:SOURCE_LABEL_LIMIT]
    )


def _findingSummary(view: FindingView) -> str:
    """结论摘要（喂 LLM 的唯一数据形态：不含任何原始行）。"""
    state = "已验证" if view.verified else f"未验证：{view.error}"
    return f"{view.claimText}（置信度 {view.confidence}，{state}，{view.rowCount} 行）"


def _findingDigest(ctx: _Context) -> str:
    views = ctx.findings[:MAX_REPORT_FINDINGS]
    if not views:
        return EMPTY_FINDING_TEXT
    return "\n".join(f"- {_findingSummary(view)}" for view in views)


def _degradedText(ctx: _Context) -> str:
    """LLM 降级文案：显式说明 + 结构化摘要列表（brief 指定文案）。"""
    return "\n".join([LLM_FALLBACK_NOTICE, *_bulletItems(_findingDigest(ctx))])


def _bulletItems(text: str) -> list[str]:
    items = [line.strip().lstrip("-•* ").strip() for line in text.splitlines()]
    return [item for item in items if item]


def _splitBullets(text: str) -> list[str]:
    return [item[:MAX_BULLET_CHARS] for item in _bulletItems(text)][:MAX_BULLETS]


def _tableBlock(columns: list[str], rows: list[dict[str, Any]], refs: tuple[SourceRef, ...] = ()) -> ReportBlock:
    return ReportBlock(BLOCK_TABLE, {"columns": list(columns), "rows": rows}, refs)


def _chartContent(view: FindingView) -> dict[str, Any]:
    """chart 块 content：行数据逐字来自 finding.supporting_data["rows"]。"""
    return {
        "title": view.claimText,
        "chartType": None,  # v1 前端以表格兜底渲染（Task 11）；图表接入留 Task 12
        "columns": list(view.rows[0].keys()) if view.rows else [],
        "rows": view.rows,
    }


def _names(items: Any, key: str) -> str:
    values = [str(item.get(key)) for item in (items or []) if isinstance(item, dict) and item.get(key)]
    return "、".join(values)


# ---------------------------------------------------------------------------
# MD 渲染（纯字符串模板，零 LLM）
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _mdTable(columns: list[str], rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(_cell(column) for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines += [
        "| " + " | ".join(_cell(row.get(column)) for column in columns) + " |" for row in rows
    ]
    return lines


def _renderBlock(block: dict[str, Any]) -> list[str]:
    """单块 → MD 行。chart 块渲染为表格 + 「图表见 payload」提示（brief 指定）。"""
    kind, content = block["type"], block["content"]
    if kind == BLOCK_TEXT:
        return [str(content), ""]
    if kind == BLOCK_BULLET:
        return [f"- {item}" for item in content] + [""]
    if kind == BLOCK_TABLE:
        return _mdTable(content["columns"], content["rows"]) + [""]
    if kind == BLOCK_CHART:
        table = _mdTable(content["columns"], content["rows"])
        return [*table, "", f"> {CHART_HINT}（chartType={content.get('chartType') or 'table'}）", ""]
    logger.warning("报告 MD 渲染遇到未知块类型，跳过: %s", kind)
    return []


def renderMarkdown(payload: dict[str, Any]) -> str:
    """payload → Markdown（章节标题 + 块渲染；可下载存档）。"""
    lines = [
        f"# {payload.get('title') or DEFAULT_TITLE}",
        "",
        f"> 模式：{payload.get('mode')}｜问题：{payload.get('question')}",
        "",
    ]
    for section in payload.get("sections") or []:
        lines += [f"## {section['title']}", ""]
        for block in section["blocks"]:
            lines += _renderBlock(block)
    return "\n".join(lines).strip() + "\n"


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
        sections = tuple([await self._buildSection(kind, ctx, client=client) for kind in kinds])
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
        self, kind: str, ctx: _Context, *, client: Any
    ) -> ReportSection:
        builder = self._builders.get(kind)
        if builder is None:
            logger.error("未知报告段落 kind: %s", kind)
            raise ValueError(f"未知报告段落: {kind}")
        return await builder(ctx, client=client)

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
            "sections": [_sectionToDict(section) for section in sections],
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
        self, kind: str, ctx: _Context, *, client: Any
    ) -> ReportSection:
        """叙述型 text 段（执行摘要 / 结论 / 差异分析）：单次 LLM，失败降级模板。"""
        refs = tuple([_findingRef(view) for view in ctx.findings[:MAX_REPORT_FINDINGS]])
        text = await self._llmText(
            client,
            system=_NARRATIVE_SYSTEM.format(intent=_NARRATIVE_INTENTS[kind]),
            parts=[ctx.question, _findingDigest(ctx)],
        )
        return self._textSection(kind, text or _degradedText(ctx), refs)

    async def _executiveSummary(self, ctx: _Context, *, client: Any) -> ReportSection:
        return await self._narrativeSection("executive_summary", ctx, client=client)

    async def _conclusion(self, ctx: _Context, *, client: Any) -> ReportSection:
        return await self._narrativeSection("conclusion", ctx, client=client)

    async def _diffAnalysis(self, ctx: _Context, *, client: Any) -> ReportSection:
        return await self._narrativeSection("diff_analysis", ctx, client=client)

    async def _dataSection(self, ctx: _Context, *, client: Any) -> ReportSection:
        """数据章节：每条结论一段「chart（行数据逐字）+ bullet（LLM 解读）」。"""
        blocks: list[ReportBlock] = []
        for view in ctx.findings[:MAX_REPORT_FINDINGS]:
            ref = _findingRef(view)
            if view.rows:
                blocks.append(ReportBlock(BLOCK_CHART, _chartContent(view), (ref,)))
            bullets = await self._commentary(view, ctx, client=client)
            blocks.append(ReportBlock(BLOCK_BULLET, bullets, (ref,)))
        if not blocks:
            blocks.append(ReportBlock(BLOCK_TEXT, EMPTY_FINDING_TEXT, ()))
        return ReportSection("data", "data", _SECTION_TITLES["data"], tuple(blocks))

    async def _commentary(self, view: FindingView, ctx: _Context, *, client: Any) -> list[str]:
        """单 finding 单次 LLM 调用（设计 §4.7 约束）；失败降级为该结论的结构化摘要。"""
        text = await self._llmText(
            client, system=_COMMENTARY_SYSTEM, parts=[ctx.question, _findingSummary(view)]
        )
        return _splitBullets(text) if text else [_findingSummary(view)]

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

    async def _knowledgeSection(self, ctx: _Context, *, client: Any = None) -> ReportSection:
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

    async def _methodologySection(self, ctx: _Context, *, client: Any = None) -> ReportSection:
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

    async def _hypothesisTable(self, ctx: _Context, *, client: Any = None) -> ReportSection:
        views = ctx.findings[:MAX_REPORT_FINDINGS]
        if not views:
            return self._textSection("hypothesis_table", EMPTY_FINDING_TEXT)
        columns = ["假设", "置信度", "已验证", "数据行数"]
        rows = [
            {
                "假设": view.claimText,
                "置信度": view.confidence,
                "已验证": "是" if view.verified else "否",
                "数据行数": view.rowCount,
            }
            for view in views
        ]
        block = _tableBlock(columns, rows, tuple([_findingRef(view) for view in views]))
        return ReportSection(
            "hypothesis_table", "hypothesis_table", _SECTION_TITLES["hypothesis_table"], (block,)
        )

    async def _alternativeSection(self, ctx: _Context, *, client: Any = None) -> ReportSection:
        """备选假设：验证未通过的结论（supporting_data["error"] 非空）。"""
        pending = [view for view in ctx.findings if not view.verified][:MAX_REPORT_FINDINGS]
        if not pending:
            return self._textSection("alternative", EMPTY_ALTERNATIVE_TEXT)
        refs = tuple([_findingRef(view) for view in pending])
        bullets = [_findingSummary(view) for view in pending]
        return ReportSection(
            "alternative", "alternative", _SECTION_TITLES["alternative"], (ReportBlock(BLOCK_BULLET, bullets, refs),)
        )

    async def _comparisonTable(self, ctx: _Context, *, client: Any = None) -> ReportSection:
        views = ctx.findings[:MAX_REPORT_FINDINGS]
        if not views:
            return self._textSection("comparison_table", EMPTY_FINDING_TEXT)
        columns = ["结论", "置信度", "数据行数", "支撑 SQL"]
        rows = [
            {
                "结论": view.claimText,
                "置信度": view.confidence,
                "数据行数": view.rowCount,
                "支撑 SQL": view.sql or "",
            }
            for view in views
        ]
        block = _tableBlock(columns, rows, tuple([_findingRef(view) for view in views]))
        return ReportSection(
            "comparison_table", "comparison_table", _SECTION_TITLES["comparison_table"], (block,)
        )
