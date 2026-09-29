"""机制 3：冲突检测（feat-wiki-knowledge，Phase 8 M5）。

**四类冲突里只有一类需要模型**，这是本模块最重要的成本设计：

==============  ==========  =======================================================
冲突类型        检测手段    判定依据
==============  ==========  =======================================================
``STALENESS``   确定性      引用的目标条目已失效（status=EXPIRED 或 valid_to 已过）
``GAP``         确定性      关系指向的目标在库里不存在（悬空引用）
``OVERLAP``     确定性      两条条目标题归一化后相同
``CONTRADICTION`` LLM       两条自然语言陈述是否实质矛盾
==============  ==========  =======================================================

前三类的判定是集合/字符串运算，本可以「顺手都交给模型」，但那样做有两个坏处：
一是给每次检测加上一笔固定成本，二是把**本来确定的事实**变成概率输出——一条
指向不存在本体的知识，模型完全可能看走眼说「没问题」。确定的事就用确定的方法判。

LLM 那一路也做了成本收敛：只在**已有关系连接的条目对**之间判定，而不是全库两两
比较。关系本身就是「这两条知识有关」的机器判断（M4 产出），拿它当候选集比让模型
扫全库便宜好几个数量级；召回损失也有限——真正互相矛盾的两条知识通常本就该有引用
关系，没有的话那是机制 2 的召回问题，应该在机制 2 修，不该在这里用全库比对兜底。

与 M4 一致的语义：**产出是待处理的记录而非既成事实**。冲突落库后等人工处置，
处置动作（RESOLVED / IGNORED / MERGED）由 ``WikiConflictService`` 记录。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy import text as saText
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass, OntologyMetric
from app.domain.wiki_learning_models import (
    CONFLICT_SEVERITIES,
    KnowledgeConflict,
)
from app.domain.wiki_models import KnowledgeRelation, WikiPage
from app.services.learning.prompt_fence import neutralizeFence

if TYPE_CHECKING:  # 仅类型标注用，避免 service 间循环导入
    from app.services.learning.llm_invoker import LearningLLMInvoker

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "detect_conflict_v1.txt"

# 冲突类型（与 wiki_learning_models.CONFLICT_TYPES 对齐）
CONFLICT_CONTRADICTION = "CONTRADICTION"
CONFLICT_STALENESS = "STALENESS"
CONFLICT_GAP = "GAP"
CONFLICT_OVERLAP = "OVERLAP"

# LLM 一路的三种结果。语义与 M4 的 classExtractionStatus 完全一致：
# SKIPPED 是「没要求跑」，FAILED 是「跑了没成」，调用方据此决定是否提示重试。
LLM_STATUS_SKIPPED = "SKIPPED"
LLM_STATUS_SUCCEEDED = "SUCCEEDED"
LLM_STATUS_FAILED = "FAILED"

# 严重度默认值：确定性判出的失效引用与悬空引用都是「照着一份错的东西办事」，
# 一律 HIGH；标题重复只是疑似冗余，MEDIUM。
_STALENESS_SEVERITY = "HIGH"
_GAP_SEVERITY = "HIGH"
_OVERLAP_SEVERITY = "MEDIUM"

# 一次检测最多送几对候选给模型。给上限的理由是成本：prompt 随候选数线性增长，
# 而候选本身按 id 排序取前 N 条 —— 顺序确定，同一份数据每次送的是同一批，
# 结果可复现（无 ORDER BY 的 LIMIT 会给出不可复现的候选集，同 M4 的教训）。
_MAX_CONTRADICTION_CANDIDATES = 5

# 送模型的正文上限。冲突判定看的是「规则说了什么」，不需要全文。
_MAX_CONTENT_CHARS = 4000

# 严重度白名单外的回传值一律降级到这里，而不是原样落库 —— severity 是冲突看板
# 的排序与筛选轴，放一个模型编的值进去会让统计出现无法归类的行。
_VALID_SEVERITY_FALLBACK = "MEDIUM"

# SUPERSEDES 的字面值：指向旧版本是**正确用法**，见 _detectStaleness。
_RELATION_SUPERSEDES = "SUPERSEDES"

_TARGET_PAGE = "PAGE"
_TARGET_ONTOLOGY_CLASS = "ONTOLOGY_CLASS"
_TARGET_ONTOLOGY_METRIC = "ONTOLOGY_METRIC"

# 标题归一化：去掉空白与常见中英文标点后比对。
# 「供应商准入规则」与「供应商 准入 规则」「供应商（准入）规则」应视作同一件事。
_TITLE_NOISE = re.compile(r"[\s　_\-—·、，。：；！？（）()【】\[\]「」『』\"'`]+")


@lru_cache(maxsize=1)
def _loadSystemPrompt() -> str:
    """读取冲突判定 system prompt（进程内缓存一次）。"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def normalizeTitle(raw: str) -> str:
    """标题归一化键（仅用于比对，不落库）。"""
    return _TITLE_NOISE.sub("", raw).casefold()


@dataclass(frozen=True)
class ConflictDetectionResult:
    """一次冲突检测的结果（不可变）。

    ``conflicts`` 只含**本次新增**的冲突 —— 已存在且未处置的同一冲突被幂等跳过。
    """

    conflicts: tuple[KnowledgeConflict, ...]
    llmStatus: str


class ConflictDetector:
    """机制 3：为单条知识检测冲突。"""

    async def detectForPage(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        invoker: LearningLLMInvoker | None = None,
    ) -> ConflictDetectionResult:
        """跑机制 3 并落库冲突，返回**新增**的那些。

        ``invoker`` 为空则只跑确定性三路（不调模型、零 token 成本）。
        LLM 路径失败**不阻断**确定性路径的结果 —— 与机制 2 同一取舍：矛盾判定
        只是四路里的一路，不该让已经查实的三路跟着一起丢。
        """
        page = await self._loadPage(session, pageId)

        proposals: list[dict[str, Any]] = []
        proposals.extend(await self._detectStaleness(session, page))
        proposals.extend(await self._detectGaps(session, page))
        proposals.extend(await self._detectOverlaps(session, page))

        llmStatus = LLM_STATUS_SKIPPED
        if invoker is not None:
            try:
                proposals.extend(await self._detectContradictions(session, page, invoker))
                llmStatus = LLM_STATUS_SUCCEEDED
            except Exception as e:  # noqa: BLE001 - 失败降级，见方法 docstring
                logger.warning("机制 3 矛盾判定失败（确定性三路结果保留）: %s", e)
                llmStatus = LLM_STATUS_FAILED

        created = await self._persistConflicts(session, proposals)
        return ConflictDetectionResult(conflicts=tuple(created), llmStatus=llmStatus)

    async def _loadPage(self, session: AsyncSession, pageId: str) -> WikiPage:
        """取待检测的条目（复用 WikiPageService 的 404 语义）。"""
        from app.services.wiki_page_service import WikiPageService

        return await WikiPageService().getPage(session, pageId)

    # -- 确定性三路 ---------------------------------------------------------

    async def _detectStaleness(
        self, session: AsyncSession, page: WikiPage
    ) -> list[dict[str, Any]]:
        """本文引用的条目已失效 → STALENESS。

        **只看本文的传出关系**，不做反向扫描（「谁引用了我而我已失效」）。反向
        扫描会把检测成本乘上被引次数，而结论完全相同 —— 引用方各自跑一次检测
        就会各自产出一条冲突。一份冲突挂在一个方向上即可。

        ``SUPERSEDES`` 排除在外：指向旧版本正是这条关系的**用途**。把「目标已
        失效」一律当冲突，会让每一次正常的版本演进都刷出一条冲突，看板很快变成
        噪声，用户开始整片忽略——那比不检测更糟。
        """
        rows = (
            await session.execute(
                select(KnowledgeRelation.downstream_id)
                .where(
                    KnowledgeRelation.upstream_page_id == page.page_id,
                    KnowledgeRelation.downstream_type == _TARGET_PAGE,
                    KnowledgeRelation.relation_type != _RELATION_SUPERSEDES,
                )
                .where(
                    KnowledgeRelation.downstream_id.in_(
                        select(WikiPage.page_id).where(
                            (WikiPage.status == "EXPIRED")
                            | (WikiPage.valid_to < datetime.now(UTC))
                        )
                    )
                )
            )
        ).scalars().all()

        return [
            _conflictRow(
                conflictType=CONFLICT_STALENESS,
                pageIds=(page.page_id, downstreamId),
                severity=_STALENESS_SEVERITY,
                detectedBy="RULE",
                description=f"本文引用的知识条目「{downstreamId}」已失效，据此执行可能已不符合现行要求",
            )
            for downstreamId in rows
        ]

    async def _detectGaps(
        self, session: AsyncSession, page: WikiPage
    ) -> list[dict[str, Any]]:
        """关系指向的目标在库里不存在 → GAP（悬空引用）。

        ``ENTITY_MAPPING`` 类型**刻意不查**：那要扫 35 万行 entity_mapping，而
        实体映射是随时增删的运营数据，今天不存在的映射明天可能就有了——为它每次
        检测都做一次大表存在性查询，代价与收益不成比例（方案 §风险 已定：实体
        相关分析走懒查询，见 M8）。这里如实跳过，不假装查过。

        三类目标**各一次批量查询**（``IN`` 收口），而不是逐条关系查一次：N 条
        关系就是 N 次往返，而候选 id 通常不到十个，一次 IN 足够。
        """
        relations = (
            await session.execute(
                select(
                    KnowledgeRelation.downstream_type,
                    KnowledgeRelation.downstream_id,
                ).where(KnowledgeRelation.upstream_page_id == page.page_id)
            )
        ).all()

        byType: dict[str, list[str]] = {}
        for downstreamType, downstreamId in relations:
            if downstreamType in (_TARGET_PAGE, _TARGET_ONTOLOGY_CLASS, _TARGET_ONTOLOGY_METRIC):
                byType.setdefault(downstreamType, []).append(downstreamId)
        if not byType:
            return []

        existing: set[tuple[str, str]] = set()
        if byType.get(_TARGET_PAGE):
            ids = (
                await session.execute(
                    select(WikiPage.page_id).where(
                        WikiPage.page_id.in_(byType[_TARGET_PAGE])
                    )
                )
            ).scalars().all()
            existing.update((_TARGET_PAGE, i) for i in ids)
        if byType.get(_TARGET_ONTOLOGY_CLASS):
            names = (
                await session.execute(
                    select(OntologyClass.class_name).where(
                        OntologyClass.valid_to.is_(None),
                        OntologyClass.class_name.in_(byType[_TARGET_ONTOLOGY_CLASS]),
                    )
                )
            ).scalars().all()
            existing.update((_TARGET_ONTOLOGY_CLASS, n) for n in names)
        if byType.get(_TARGET_ONTOLOGY_METRIC):
            names = (
                await session.execute(
                    select(OntologyMetric.metric_name).where(
                        OntologyMetric.metric_name.in_(byType[_TARGET_ONTOLOGY_METRIC])
                    )
                )
            ).scalars().all()
            existing.update((_TARGET_ONTOLOGY_METRIC, n) for n in names)

        proposals: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for downstreamType, ids in byType.items():
            for downstreamId in ids:
                if (downstreamType, downstreamId) in existing:
                    continue
                if (downstreamType, downstreamId) in seen:
                    continue
                seen.add((downstreamType, downstreamId))
                proposals.append(
                    _conflictRow(
                        conflictType=CONFLICT_GAP,
                        pageIds=(page.page_id,),
                        severity=_GAP_SEVERITY,
                        detectedBy="RULE",
                        description=(
                            f"本文关联的{_targetLabel(downstreamType)}"
                            f"「{downstreamId}」在系统中不存在，关联已悬空"
                        ),
                    )
                )
        return proposals

    async def _detectOverlaps(
        self, session: AsyncSession, page: WikiPage
    ) -> list[dict[str, Any]]:
        """其它条目标题归一化后与本文相同 → OVERLAP（疑似同一件事写了两遍）。

        只取 ``(page_id, title)`` 两列，归一化在内存里做：标题短，全库拉回来
        也不贵，而**在 SQL 里做归一化**意味着要把正则搬进 SQL 且无法用索引 ——
        两端都更差。真到了标题量级不可忽略时，应改为在 PG 里维护一个归一化列
        （写入时算好 + 建索引），而不是把 regexp_replace 塞进 WHERE。
        """
        ownKey = normalizeTitle(page.title)
        if not ownKey:
            # 标题全是标点/空白，归一化后为空。此时「和其它空标题相同」不是
            # 有效的重复信号，报出来只会是噪声。
            return []

        rows = (
            await session.execute(
                select(WikiPage.page_id, WikiPage.title).where(
                    WikiPage.page_id != page.page_id
                )
            )
        ).all()

        proposals: list[dict[str, Any]] = []
        for otherId, otherTitle in rows:
            if normalizeTitle(otherTitle) != ownKey:
                continue
            proposals.append(
                _conflictRow(
                    conflictType=CONFLICT_OVERLAP,
                    pageIds=(page.page_id, otherId),
                    severity=_OVERLAP_SEVERITY,
                    detectedBy="RULE",
                    description=f"本文与「{otherId}」标题重复，疑似同一件事写了两遍",
                )
            )
        return proposals

    # -- LLM 一路 -----------------------------------------------------------

    async def _detectContradictions(
        self, session: AsyncSession, page: WikiPage, invoker: LearningLLMInvoker
    ) -> list[dict[str, Any]]:
        """对「已有关系连接」的条目对跑一次模型矛盾判定。

        **候选集为空时不调模型**：没有候选就没有可判的矛盾，白调一次是纯粹的浪费
        （而且 page 越孤立越容易命中这条路径，不拦的话它反而成了成本大头）。

        模型回传的 ``pageId`` **必须落在本次给出的候选集内**。不校验的后果不是
        「多一行脏数据」那么轻：``page_ids`` 是无外键的多态数组，DB 拦不住，
        冲突表里会多出一条指向空气的记录，看板上永远清不掉（没人能处置一条不
        存在的知识），而它还会参与冲突数统计。校验成本是一次 set 查找。
        """
        candidates = (
            await session.execute(
                select(KnowledgeRelation.downstream_id)
                .where(
                    KnowledgeRelation.upstream_page_id == page.page_id,
                    KnowledgeRelation.downstream_type == _TARGET_PAGE,
                    KnowledgeRelation.relation_type != _RELATION_SUPERSEDES,
                )
                .order_by(KnowledgeRelation.downstream_id)
                .limit(_MAX_CONTRADICTION_CANDIDATES)
            )
        ).scalars().all()
        if not candidates:
            return []

        candidatePages = (
            await session.execute(
                select(WikiPage.page_id, WikiPage.title, WikiPage.content).where(
                    WikiPage.page_id.in_(candidates)
                )
            )
        ).all()
        if not candidatePages:
            return []

        allowedIds = {row.page_id for row in candidatePages}
        parsed, _ = await invoker.completeJson(
            systemPrompt=_loadSystemPrompt(),
            userPrompt=_buildContradictionPrompt(page, candidatePages),
            mechanism="CONFLICT",
            purpose="wiki_conflict_detect",
        )

        rawItems = parsed.get("contradictions")
        if not isinstance(rawItems, list):
            return []

        proposals: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in rawItems:
            if not isinstance(item, dict):
                continue
            otherId = item.get("pageId")
            if not isinstance(otherId, str) or otherId not in allowedIds:
                logger.warning("机制 3 丢弃候选集外的 pageId: %r", otherId)
                continue
            if otherId in seen:  # 同一条候选被模型报了两次
                continue
            seen.add(otherId)
            reason = item.get("reason")
            severity = item.get("severity")
            proposals.append(
                _conflictRow(
                    conflictType=CONFLICT_CONTRADICTION,
                    pageIds=(page.page_id, otherId),
                    severity=severity if severity in CONFLICT_SEVERITIES
                    else _VALID_SEVERITY_FALLBACK,
                    detectedBy="LLM",
                    description=(
                        reason.strip()[:2000]
                        if isinstance(reason, str) and reason.strip()
                        else "模型判定本文与该条目存在矛盾（未给出理由）"
                    ),
                )
            )
        return proposals

    # -- 落库 ---------------------------------------------------------------

    async def _persistConflicts(
        self, session: AsyncSession, proposals: list[dict[str, Any]]
    ) -> list[KnowledgeConflict]:
        """幂等写入冲突，返回**本次真正新增**的行。

        用 ``ON CONFLICT DO NOTHING`` + ``RETURNING``，与 M4 同一取舍：先查后插在
        并发下会双双通过检查，第二条撞唯一约束冒 500；``RETURNING`` 只回吐真正
        插入的行，正好是「本次新增」的定义。

        这里的冲突目标是**部分**唯一索引 ``uq_knowledge_conflict_open``，所以必须
        同时给出 ``index_where``：PG 要用它才能把 ON CONFLICT 推断到那个部分索引上，
        少了它直接报「no unique or exclusion constraint matching」。

        **没有冲突时也必须 commit**：本方法同时承担「把 LLM 计量行落库」的责任，
        理由与 ``RelationDiscovery._persistCandidates`` 完全相同 —— 计量只 flush
        不 commit，请求会话关闭时会一起回滚，「调了模型但没检出冲突」这条路径会把
        花掉的 token 记成 0。
        """
        ids: list[int] = []
        if proposals:
            deduped: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
            for row in proposals:
                deduped.setdefault(
                    (row["conflict_type"], tuple(row["page_ids"])), row
                )

            stmt = (
                pgInsert(KnowledgeConflict)
                .values(list(deduped.values()))
                .on_conflict_do_nothing(
                    index_elements=["conflict_type", "page_ids"],
                    index_where=saText("resolved_at IS NULL"),
                )
                .returning(KnowledgeConflict.id)
            )
            ids = list((await session.execute(stmt)).scalars().all())

        # 单点提交：冲突与计量行同生共死（见 docstring）
        # 必须是 commit 不是 flush：getDb 依赖只在异常时 rollback，正常路径关闭
        # 会话时不 commit，没 commit 的 INSERT 在会话关掉时就回滚掉了——前端点了
        # 「冲突检测」返回 id=N，关掉会话后那条冲突就消失了。
        await session.commit()
        if not ids:
            return []

        result = await session.execute(
            select(KnowledgeConflict)
            .where(KnowledgeConflict.id.in_(ids))
            .order_by(KnowledgeConflict.id)
        )
        return list(result.scalars().all())


def _buildContradictionPrompt(
    page: WikiPage, candidates: list[Any]
) -> str:
    """拼装矛盾判定的 user prompt（所有动态内容都过围栏中和）。

    围栏中和是硬要求而非风格：正文里写一句 ``</user_content>`` 就能把后续文字
    抬成「围栏之外的指令」；而这里被抬高的指令可以命令模型**输出指定 pageId 的
    矛盾**——那会直接落成一条真实的冲突记录。M4 曾因替换值写成空操作而失去这层
    防护（见 summary §8.5），故本模块一律经 ``neutralizeFence``。
    """
    blocks = [
        f"<user_content>\n[基准条目] id={page.page_id}\n标题：{neutralizeFence(page.title)}\n"
        f"正文：\n{neutralizeFence(page.content[:_MAX_CONTENT_CHARS])}\n</user_content>"
    ]
    for candidate in candidates:
        blocks.append(
            f"<user_content>\n[候选条目] id={candidate.page_id}\n"
            f"标题：{neutralizeFence(candidate.title)}\n"
            f"正文：\n{neutralizeFence(candidate.content[:_MAX_CONTENT_CHARS])}\n</user_content>"
        )
    return "\n\n".join(blocks)


def _conflictRow(
    *,
    conflictType: str,
    pageIds: tuple[str, ...],
    severity: str,
    detectedBy: str,
    description: str,
) -> dict[str, Any]:
    """构造一条冲突的插入行。

    ``page_ids`` 在此**排序去重**。这不是美化输出：``uq_knowledge_conflict_open``
    是数组列上的部分唯一索引，而 ``['B','A']`` 与 ``['A','B']`` 在数组意义上
    不相等 —— 不排序则唯一索引形同虚设，同一对冲突每次检测都会新增一行。
    放在这个唯一的构造点做，比指望每个调用方都记得调 ``sorted()`` 可靠。
    """
    return {
        "conflict_type": conflictType,
        "page_ids": sorted(set(pageIds)),
        "description": description,
        "severity": severity,
        "detected_by": detectedBy,
    }


def _targetLabel(downstreamType: str) -> str:
    """GAP 描述里的人话目标类型。"""
    return {
        _TARGET_PAGE: "知识条目",
        _TARGET_ONTOLOGY_CLASS: "本体类",
        _TARGET_ONTOLOGY_METRIC: "指标",
    }.get(downstreamType, "对象")


__all__ = [
    "ConflictDetector",
    "ConflictDetectionResult",
    "normalizeTitle",
    "CONFLICT_CONTRADICTION",
    "CONFLICT_STALENESS",
    "CONFLICT_GAP",
    "CONFLICT_OVERLAP",
    "LLM_STATUS_SKIPPED",
    "LLM_STATUS_SUCCEEDED",
    "LLM_STATUS_FAILED",
]
