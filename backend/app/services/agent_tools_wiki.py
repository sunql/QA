"""Wiki 知识工具的 Agent 注册（feat-wiki-knowledge，Phase 8 M8）。

把 M1–M7 沉淀的知识资产接到 Agent 运行时上，让已有知识**可被检索、可被引用、
可被执行** —— 这是「自学习」闭环的出口：知识若只能躺在管理页里被翻，它就只是
一份文档库，不是 Agent 能用的知识。

## 四个工具

- ``wiki_search``     —— 按标题/正文模糊检索（纯读，无 LLM）
- ``wiki_read``       —— 读单条知识全文 + 事实原子 + 已确认关系 + 结构化产物
- ``rule_evaluate``   —— 对条目的可执行规则跑 dry-run，报告三态结果
- ``coverage_status`` —— 覆盖度汇总 + 最紧迫缺口 + 未挂业务对象的条目

## 三条刻意的设计约束

**1. 全部只读。** 四个工具都不写库、不改状态。知识的写入走 M1/M2 的导入与
CRUD 链路（那条路上有审核、有学习反馈）；Agent 能改知识的话，「谁在什么时候
把这条规则改错了」就查不清了。

**2. 不调用 LLM，故 Token 计量恒为 0。** 这不是省事——``ToolResult`` 的
tokens/cost 字段默认 0 已表达「本次没有 LLM 调用」。若将来某个 wiki 工具
引入了模型调用，必须同时把用量落到 ``wiki_token_usage``（项目硬约束），
不能只改这里的返回值。

**3. 指称解析失败要**说清楚**，不能静默挑一条。** 用户说「供应商准入要求」，
可能精确命中一条、可能模糊命中八条、也可能一条都没有。后两种只能由调用方
（LLM 或人）决定怎么办，工具替它猜一条是最坏的选择：答案看起来是对的。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError
from app.domain.wiki_coverage_models import COVERAGE_STATUSES
from app.domain.wiki_models import WikiPage
from app.services.agent_tool_types import AgentHandler, AgentToolContext, ArgExtractor, ToolResult
from app.services.learning.coverage_tracker import CoverageTracker
from app.services.messages_zh import (
    MSG_WIKI_AGENT_PAGE_REF_AMBIGUOUS,
    MSG_WIKI_AGENT_PAGE_REF_NOT_FOUND,
    MSG_WIKI_AGENT_RULE_NO_EXAMPLES,
)
from app.services.wiki_page_service import WikiPageService
from app.services.wiki_structure_service import WikiStructureService

logger = logging.getLogger(__name__)

# Agent 上下文里单次检索返回的条数上限。比管理页的 DEFAULT_SEARCH_LIMIT 更小：
# 管理页是人在翻列表，Agent 是要把结果读进上下文再作答 —— 十条摘要就足以
# 挤掉真正相关的那一条。
AGENT_SEARCH_LIMIT = 5

# 覆盖度工具返回的缺口条数。缺口在初始状态下可能上千，全量返回等于把一次
# 工具调用变成一次导出。
AGENT_GAP_LIMIT = 10

# 正文摘要长度（字符）。检索命中只要够人/模型判断「是不是这条」。
SNIPPET_LENGTH = 160


# ---------------------------------------------------------------------------
# 工具参数解析
# ---------------------------------------------------------------------------


def extractWikiText(raw: str) -> dict | None:
    """把整条输入当作检索词 / 条目指称。

    与 ``supplier_key`` 那类正则抽取器不同，Wiki 的指称没有固定格式（是标题、
    是 ID、还是正文里的一句话都可能），所以不做正则切分——切错的代价是**找
    不到**，而不切只是一次范围更大的检索。空输入返回 ``None``，由运行时统一
    转成 422（``MSG_AGENT_RUN_BAD_INPUT``）。
    """
    text = raw.strip()
    return {"query": text} if text else None


def extractWikiNoArgs(raw: str) -> dict:
    """无参工具（``coverage_status``）：输入本身不携带参数。

    返回 ``{}`` 而非 ``None``：``None`` 的语义是「这次输入解析不出参数」，
    对无参工具而言永远不会发生 —— 误用 ``None`` 会让覆盖率查询在用户说了
    句空话时报 422。
    """
    return {}


WIKI_ARG_EXTRACTORS: dict[str, ArgExtractor] = {
    "wiki_text": extractWikiText,
    "wiki_no_args": extractWikiNoArgs,
}


# ---------------------------------------------------------------------------
# 共享助手
# ---------------------------------------------------------------------------


def _snippet(content: str) -> str:
    """正文摘要：压平换行（LLM/终端里孤零零的换行没有信息量）后截断。"""
    flat = " ".join((content or "").split())
    return flat[:SNIPPET_LENGTH]


def _pageBrief(page: WikiPage) -> dict[str, Any]:
    """条目的轻量投影（列表 / 候选共用）。"""
    return {
        "pageId": page.page_id,
        "title": page.title,
        "dimension": page.dimension,
        "status": page.status,
        "structureStage": page.structure_stage,
    }


async def _resolvePageRef(
    session: AsyncSession, ref: str
) -> tuple[WikiPage | None, list[WikiPage]]:
    """解析条目指称 → ``(唯一命中的条目 | None, 全部候选)``。

    「一条都没找到」在这里直接 404：它与「找到多条」的处理方式完全不同 ——
    前者用户需要改说法，后者用户需要在既有条目里挑一个。把两种情况揉成
    「返回候选列表」会让 404 消失，调用方再也分不清是没找到还是没选。
    """
    candidates = await WikiPageService().findPageCandidates(session, ref)
    if not candidates:
        raise NotFoundError(MSG_WIKI_AGENT_PAGE_REF_NOT_FOUND.format(ref=ref))
    if len(candidates) == 1:
        return candidates[0], candidates
    return None, candidates


def _ambiguousResult(ref: str, candidates: list[WikiPage]) -> ToolResult:
    """多条命中：把候选摊开，明确请调用方指明，而不是替它选一条。"""
    return ToolResult(
        data={
            "query": ref,
            "resolved": False,
            "candidates": [_pageBrief(p) for p in candidates],
        },
        answer=(
            MSG_WIKI_AGENT_PAGE_REF_AMBIGUOUS.format(ref=ref, count=len(candidates))
            + "候选："
            + "；".join(f"{p.title}（{p.page_id}）" for p in candidates)
        ),
    )


# ---------------------------------------------------------------------------
# wiki_search
# ---------------------------------------------------------------------------


async def _wikiSearchHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """按标题/正文模糊检索知识条目。"""
    query = args["query"]
    rows, total = await WikiPageService().searchPages(
        session, query=query, limit=AGENT_SEARCH_LIMIT
    )

    if not rows:
        return ToolResult(
            data={"query": query, "total": 0, "items": []},
            answer=f"没有检索到与「{query}」相关的知识条目。",
        )

    answer = (
        f"「{query}」共匹配 {total} 条知识，最相关的前 {len(rows)} 条："
        + "；".join(
            f"{p.title}（{p.dimension or '未分类'}/{p.status}）" for p in rows
        )
    )
    return ToolResult(
        data={
            "query": query,
            "total": total,
            "items": [{**_pageBrief(p), "snippet": _snippet(p.content)} for p in rows],
        },
        answer=answer,
    )


# ---------------------------------------------------------------------------
# wiki_read
# ---------------------------------------------------------------------------


async def _wikiReadHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """读单条知识：全文 + 事实原子 + 已确认关系 + 结构化产物。

    「已确认关系」而非全部关系：候选关系是**机器提的、还没人审过**的猜测
    （M4 的 ``confirmed=false``）。把它当事实喂给 Agent，等于让机器猜的东西
    绕开审核直接变成对外答案。
    """
    ref = args["query"]
    page, candidates = await _resolvePageRef(session, ref)
    if page is None:
        return _ambiguousResult(ref, candidates)

    pages = WikiPageService()
    claims = await pages.listClaims(session, page.page_id)
    relations = await pages.listRelations(session, page.page_id, confirmedOnly=True)

    structure = WikiStructureService()
    rule = await structure.findRule(session, page.page_id)
    workflow = await structure.findWorkflow(session, page.page_id)

    answerParts = [
        f"《{page.title}》({page.page_id})，维度 {page.dimension or '未分类'}，"
        f"状态 {page.status}，结构阶段 {page.structure_stage}。"
    ]
    if claims:
        answerParts.append(f"含 {len(claims)} 条事实原子。")
    if relations:
        answerParts.append(f"已确认 {len(relations)} 条对外关系。")
    if rule is not None:
        answerParts.append(f"含可执行规则（{rule.rule_kind}）。")
    if workflow is not None:
        answerParts.append(f"含结构化流程（{len(workflow.steps)} 步）。")

    return ToolResult(
        data={
            "pageId": page.page_id,
            "title": page.title,
            "content": page.content,
            "dimension": page.dimension,
            "status": page.status,
            "structureStage": page.structure_stage,
            "authorityLevel": page.authority_level,
            "version": page.version,
            "claims": [{"id": c.id, "text": c.claim_text, "type": c.claim_type} for c in claims],
            "relations": [
                {
                    "downstreamType": r.downstream_type,
                    "downstreamId": r.downstream_id,
                    "relationType": r.relation_type,
                    "confidence": float(r.confidence) if r.confidence is not None else None,
                }
                for r in relations
            ],
            "rule": _ruleBrief(rule),
            "workflow": _workflowBrief(workflow),
        },
        answer="".join(answerParts),
    )


def _ruleBrief(rule: Any) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "ruleKind": rule.rule_kind,
        "ruleExpression": rule.rule_expression,
        "targetEntity": rule.target_entity,
        "authorityChain": list(rule.authority_chain or []),
        "version": rule.version,
        "exampleCount": len(_storedExamples(rule)),
    }


def _workflowBrief(workflow: Any) -> dict[str, Any] | None:
    if workflow is None:
        return None
    return {
        "workflowVersion": workflow.workflow_version,
        "triggerCondition": workflow.trigger_condition,
        "steps": workflow.steps,
    }


def _storedExamples(rule: Any) -> list[Any]:
    """规则上存的 dry-run 样例（形状不合时当「没有」——真正的形状校验在引擎里）。"""
    raw = rule.dry_run_examples
    return raw if isinstance(raw, list) else []


# ---------------------------------------------------------------------------
# rule_evaluate
# ---------------------------------------------------------------------------


async def _ruleEvaluateHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """对条目的可执行规则跑 dry-run，报告通过 / 失败 / 判不了三态。

    **为什么是跑存量样例，而不是拿用户口述的记录来算**：从自然语言里凑一条
    ``{field: value}`` 记录，字段名对不上时求值器会判成 UNDECIDABLE 或 False
    ——前者让用户以为规则有问题，后者更糟，会让一条正确的规则看起来「没命中」。
    样例是**审核这条规则的人写下的期望值**，拿它跑出来的结论才可信。

    没有样例时返回 ``status=NO_EXAMPLES`` 的正常结果而非报错：失败会让整次
    Agent 运行中止，连带丢掉「这条规则确实存在」这个有用信息。
    """
    ref = args["query"]
    page, candidates = await _resolvePageRef(session, ref)
    if page is None:
        return _ambiguousResult(ref, candidates)

    rule = await WikiStructureService().findRule(session, page.page_id)
    if rule is None:
        raise NotFoundError(
            f"知识条目「{page.page_id}」没有可执行规则，无法试跑"
        )

    examples = _storedExamples(rule)
    if not examples:
        return ToolResult(
            data={
                "pageId": page.page_id,
                "title": page.title,
                "status": "NO_EXAMPLES",
                "ruleKind": rule.rule_kind,
                "ruleExpression": rule.rule_expression,
            },
            answer=MSG_WIKI_AGENT_RULE_NO_EXAMPLES.format(pageId=page.page_id),
        )

    _, report = await WikiStructureService().dryRun(session, page.page_id)

    return ToolResult(
        data={
            "pageId": page.page_id,
            "title": page.title,
            "status": "EVALUATED",
            "ruleKind": rule.rule_kind,
            "ruleExpression": rule.rule_expression,
            "summary": {
                "total": report.total,
                "passed": report.passed,
                "failed": report.failed,
                "undecidable": report.undecidable,
            },
            "cases": [
                {
                    "index": r.index,
                    "input": r.input,
                    "expectedMatched": r.expectedMatched,
                    "actualMatched": r.actualMatched,
                    "status": r.status,
                }
                for r in report.results
            ],
        },
        answer=_dryRunAnswer(page.page_id, report),
    )


def _dryRunAnswer(pageId: str, report: Any) -> str:
    """把三态说清楚。

    尤其 ``判不了`` 必须单列：把它并进「失败」会把「样例没给全字段」误导成
    「规则写错了」，推着人去改一条本来正确的规则（与 ``wiki_rule_engine``
    的模块说明同源）。
    """
    parts = [
        f"《{pageId}》的规则试跑：{report.total} 条样例，"
        f"通过 {report.passed}、失败 {report.failed}、判不了 {report.undecidable}。"
    ]
    if report.failed:
        failed = [r for r in report.results if r.status == "FAILED"]
        parts.append(
            "失败样例（实际与期望不符）："
            + "；".join(
                f"#{r.index} 期望 {r.expectedMatched} 实际 {r.actualMatched}" for r in failed
            )
            + "。"
        )
    if report.undecidable:
        parts.append(
            "判不了的样例是**样例缺字段**（不是规则错），补齐字段后才能定论。"
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# coverage_status
# ---------------------------------------------------------------------------


async def _coverageStatusHandler(
    session: AsyncSession, args: dict, ctx: AgentToolContext
) -> ToolResult:
    """覆盖度汇总 + 最紧迫缺口 + 未挂业务对象的条目。

    读的是 ``coverage_cell`` 快照（M7 的刷新产物），不是现算的 —— 看板的价值
    在于「同一份数字被反复引用」，每次工具调用都重算会让 Agent 引用的数字与
    管理页看到的对不上。
    """
    tracker = CoverageTracker()
    summary = await tracker.summarize(session)
    gaps = await tracker.listGaps(session, limit=AGENT_GAP_LIMIT)
    unlinked = await tracker.countUnlinkedPages(session)

    byStatus = summary.get("byStatus", {})
    # 按词表顺序展示，保证同一份数据每次的句子长得一样（便于 diff 与复述）
    statusText = "、".join(
        f"{status} {byStatus[status]}" for status in COVERAGE_STATUSES if status in byStatus
    )

    parts = [
        f"覆盖度矩阵共 {summary.get('totalCells', 0)} 格"
        + (f"（{statusText}）" if statusText else "")
        + f"，其中 {summary.get('unassignedCells', 0)} 格未标业务域。"
    ]
    if gaps:
        parts.append(
            f"最紧迫的 {len(gaps)} 个缺口："
            + "；".join(
                f"{g.domain}/{g.className or '未归属类'}·{g.dimension}"
                f"（{g.status}，{g.pageCount} 条/已审 {g.approvedCount} 条）"
                for g in gaps
            )
            + "。"
        )
    else:
        parts.append("当前没有缺口。")
    if unlinked.pageCount:
        parts.append(
            f"另有 {unlinked.pageCount} 条知识没有挂到任何已确认的业务对象 —— "
            "矩阵看不见这部分，但它们按业务对象检索不到。"
        )

    return ToolResult(
        data={
            "summary": summary,
            "gaps": [
                {
                    "dimension": g.dimension,
                    "ontologyClassId": g.ontologyClassId,
                    "className": g.className,
                    "domain": g.domain,
                    "status": g.status,
                    "pageCount": g.pageCount,
                    "approvedCount": g.approvedCount,
                }
                for g in gaps
            ],
            "unlinked": {
                "pageCount": unlinked.pageCount,
                "dimensions": unlinked.dimensions,
            },
        },
        answer="".join(parts),
    )


WIKI_HANDLERS: dict[str, AgentHandler] = {
    "wiki_search": _wikiSearchHandler,
    "wiki_read": _wikiReadHandler,
    "rule_evaluate": _ruleEvaluateHandler,
    "coverage_status": _coverageStatusHandler,
}
