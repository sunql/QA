"""机制 2：关系发现（feat-wiki-knowledge，Phase 8 M4）。

**输出是候选而非事实**：所有自动发现的关系以 ``confirmed=False`` 落库，
只作为「待审核候选」；业务专家的 confirm/reject 才决定它是否生效
（见 ``WikiRelationService``）。图数据库里没有「待审核」这个概念 ——
一旦写进去就是既成事实，所以审核必须发生在入图之前。

本里程碑实现 4 路里的 2 路：

1. **引用检测**（确定性）：其它条目的标题在本文正文里逐字出现 → ``REFERENCES``。
   零成本、零幻觉、可解释，是关系召回的基本盘。
2. **实体抽取 → 本体类匹配**（LLM）：模型抽出正文里逐字出现的实体名，
   再与 ``ontology_class`` 的 class_name / class_alias 归一化比对 → ``DESCRIBES``。

刻意**不做**的 2 路（不是遗漏，是阶段性取舍）：

- **嵌入相似度**：需要给每条知识建/查 Milvus 向量，成本与运维面都不小，
  且与「引用检测」的召回高度重叠，留到有真实语料后再评估收益。
- **数据流分析**：要扫 ``entity_mapping`` 35 万行反查知识条目影响面，属于
  「影响分析」的下游能力，放 M8 Agent Tool 时按需懒查询（方案 §风险 已定）。

**不做图的写入**：本模块只写 PG。方案定「PG 为 SSOT、Neo4j 为镜像」，而
Neo4j 侧目前**没有 wiki 条目的节点类型**（``_ALLOWED_LABELS`` 仅
Class/Property/Metric）——现在写图只能是静默 no-op（``linkClassRelation``
用的是 MATCH 不是 MERGE，端点节点不存在时不报错也不建边），那种「看起来
同步了其实没有」比不同步更糟。故确认动作只改 PG 状态，入图作为独立特性
排在 M8 之后。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import OntologyClass
from app.domain.wiki_models import KnowledgeRelation, WikiPage
from app.services.learning.prompt_fence import neutralizeFence

if TYPE_CHECKING:  # 仅类型标注用，避免 service 间循环导入
    from app.services.learning.llm_invoker import LearningLLMInvoker

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract_relation_v1.txt"

# 关系类型（与 wiki_models.RELATION_TYPES 对齐的本次用到的那两个）
RELATION_TYPE_REFERENCES = "REFERENCES"
RELATION_TYPE_DESCRIBES = "DESCRIBES"

# 下游目标类型
TARGET_TYPE_PAGE = "PAGE"
TARGET_TYPE_ONTOLOGY_CLASS = "ONTOLOGY_CLASS"

# 置信度：字面引用是确定性证据（标题原样出现），给高分；LLM 抽实体再匹配
# 本体类多了一跳（抽得对不对 + 匹得准不准），给中高分。两者都会落进候选列表
# 由人复核，所以数值只影响排序，不影响正确性。
REFERENCE_CONFIDENCE = Decimal("0.900")
CLASS_CONFIDENCE = Decimal("0.700")

# 参与引用检测的标题最小长度。太短的标题（「制度」「流程」）会在正文里
# 命中一大片，制造出大量无意义候选，把真正有价值的关系淹掉。
MIN_REFERENCE_TITLE_CHARS = 4

# 抽取实体名的长度边界（与 prompt 约定一致，落库前再收一道）
_MIN_ENTITY_CHARS = 2
_MAX_ENTITY_CHARS = 30
_MAX_ENTITIES = 15

# 抽取用的正文上限。关系抽取看的是「提到了什么」，不需要全文；
# 截断同时避免顶爆输入上限并白付 token。
_MAX_CONTENT_CHARS = 6000

# 本体类名归一化：去掉空白/下划线/连字符后大小写无关比对。
# 「Supplier_ID」「supplier id」「SupplierID」应视作同一个类。
_NAME_NOISE = re.compile(r"[\s_\-]+")

# classExtraction 的三种结果。刻意把 SKIPPED 与 FAILED 分开：前者是
# 「没要求跑」，后者是「跑了没成」——调用方据此决定要不要提示用户重试。
CLASS_EXTRACTION_SKIPPED = "SKIPPED"
CLASS_EXTRACTION_SUCCEEDED = "SUCCEEDED"
CLASS_EXTRACTION_FAILED = "FAILED"


@lru_cache(maxsize=1)
def _loadSystemPrompt() -> str:
    """读取实体抽取 system prompt（进程内缓存一次）。"""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def normalizeEntityName(raw: str) -> str:
    """实体名归一化键（仅用于比对，不落库）。"""
    return _NAME_NOISE.sub("", raw).casefold()


@dataclass(frozen=True)
class DiscoveryResult:
    """一次关系发现的结果（不可变）。

    ``candidates`` 只含**本次新增**的候选 —— 已存在的三元组被幂等跳过，
    重复点「发现」不会把列表越滚越长，也不会重复计数。
    """

    candidates: tuple[KnowledgeRelation, ...]
    classExtractionStatus: str


class RelationDiscovery:
    """机制 2：为单条知识发现关系候选。"""

    async def discoverForPage(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        invoker: LearningLLMInvoker | None = None,
    ) -> DiscoveryResult:
        """跑机制 2 并落库候选，返回**新增**的候选。

        ``invoker`` 为空则只跑确定性路径（不调模型、不产生 token 成本）。
        LLM 路径失败**不阻断**确定性路径的结果：关系发现是增强，抽不出实体
        只意味着少一路候选，不该让已经算出来的引用关系也一起丢掉。
        """
        page = await self._loadPage(session, pageId)

        proposals: list[dict[str, Any]] = []
        proposals.extend(await self._detectReferences(session, page))

        classStatus = CLASS_EXTRACTION_SKIPPED
        if invoker is not None:
            try:
                proposals.extend(await self._extractClasses(session, page, invoker))
                classStatus = CLASS_EXTRACTION_SUCCEEDED
            except Exception as e:  # noqa: BLE001 - 失败降级，见方法 docstring
                logger.warning("机制 2 实体抽取失败（引用检测结果保留）: %s", e)
                classStatus = CLASS_EXTRACTION_FAILED

        created = await self._persistCandidates(session, proposals)
        return DiscoveryResult(candidates=tuple(created), classExtractionStatus=classStatus)

    async def _loadPage(self, session: AsyncSession, pageId: str) -> WikiPage:
        """取待发现的条目（复用 WikiPageService 的 404 语义）。"""
        from app.services.wiki_page_service import WikiPageService

        return await WikiPageService().getPage(session, pageId)

    async def _detectReferences(
        self, session: AsyncSession, page: WikiPage
    ) -> list[dict[str, Any]]:
        """其它条目的标题在本文正文里逐字出现 → 本文引用它。

        只取 ``(page_id, title)`` 两列：正文不参与比对（不需要，也没必要把
        全库正文拉进内存）。正文只归一化**一次**，逐标题做 C 级子串查找。

        规模说明：本实现是 O(条目数 × 正文长度) 的全量扫描，知识条目上万后
        会变慢。届时应改用 PG 的 ``pg_trgm`` 倒排或 Milvus 召回候选集，而不是
        继续加大这个循环 —— 现在只有百量级条目，先不引入该复杂度。
        """
        rows = (
            await session.execute(
                select(WikiPage.page_id, WikiPage.title).where(
                    WikiPage.page_id != page.page_id,
                    func.length(WikiPage.title) >= MIN_REFERENCE_TITLE_CHARS,
                )
            )
        ).all()
        if not rows:
            return []

        contentFolded = page.content.casefold()
        proposals: list[dict[str, Any]] = []
        for otherId, otherTitle in rows:
            if otherTitle.casefold() in contentFolded:
                proposals.append(
                    _candidateRow(
                        page.page_id,
                        TARGET_TYPE_PAGE,
                        otherId,
                        RELATION_TYPE_REFERENCES,
                        REFERENCE_CONFIDENCE,
                    )
                )
        return proposals

    async def _extractClasses(
        self,
        session: AsyncSession,
        page: WikiPage,
        invoker: LearningLLMInvoker,
    ) -> list[dict[str, Any]]:
        """LLM 抽实体名 → 与本体类目录归一化比对 → DESCRIBES 候选。

        匹配在本地做（而不是把 67 个类名塞进 prompt）有两个好处：类目录变动
        不必同步改 prompt，且模型不必「记住」一个它没见过的清单——它只负责
        抽原文里真实出现的名词，这一步是它擅长的。
        """
        safeTitle = neutralizeFence(page.title)
        safeContent = neutralizeFence(page.content[:_MAX_CONTENT_CHARS])
        parsed, _ = await invoker.completeJson(
            systemPrompt=_loadSystemPrompt(),
            userPrompt=(
                f"<user_content>\n标题：{safeTitle}\n\n正文：\n{safeContent}\n</user_content>"
            ),
            mechanism="RELATE",
            purpose="wiki_relation_extract",
        )

        rawEntities = parsed.get("entities")
        if not isinstance(rawEntities, list):
            return []
        entities = [
            e.strip()
            for e in rawEntities
            if isinstance(e, str) and _MIN_ENTITY_CHARS <= len(e.strip()) <= _MAX_ENTITY_CHARS
        ][:_MAX_ENTITIES]
        if not entities:
            return []

        catalog = await self._loadClassCatalog(session)
        proposals: list[dict[str, Any]] = []
        for entity in entities:
            className = catalog.get(normalizeEntityName(entity))
            if className is None:
                continue
            proposals.append(
                _candidateRow(
                    page.page_id,
                    TARGET_TYPE_ONTOLOGY_CLASS,
                    className,
                    RELATION_TYPE_DESCRIBES,
                    CLASS_CONFIDENCE,
                )
            )
        return proposals

    async def _loadClassCatalog(self, session: AsyncSession) -> dict[str, str]:
        """归一化名 → 生效中的 class_name（含别名）。

        ``valid_to IS NULL`` 过滤掉软删除的墓碑行：给一个已下线的类挂新关系
        等于往坟头上贴标签，图里查不到、看板上也不该出现。

        ``setdefault`` 让「先到先得」而非「后覆盖前」；**必须配 ``ORDER BY id``**
        才能让「先」有确定含义 —— 没有 ORDER BY 时 PG 的行序不保证，同一个实体
        可能在两次请求里匹配到不同的 ``class_name``，候选集就变得不可复现了。
        """
        rows = (
            await session.execute(
                select(OntologyClass.class_name, OntologyClass.class_alias)
                .where(OntologyClass.valid_to.is_(None))
                .order_by(OntologyClass.id)
            )
        ).all()
        catalog: dict[str, str] = {}
        for className, alias in rows:
            catalog.setdefault(normalizeEntityName(className), className)
            if alias:
                catalog.setdefault(normalizeEntityName(alias), className)
        return catalog

    async def _persistCandidates(
        self, session: AsyncSession, proposals: list[dict[str, Any]]
    ) -> list[KnowledgeRelation]:
        """幂等写入候选，返回**本次真正新增**的行。

        用 ``ON CONFLICT DO NOTHING`` + ``RETURNING`` 而不是「先查后插」：
        并发两次「发现」时先查后插会双双通过检查，第二条撞唯一约束冒 500；
        而 ``RETURNING`` 天然只回吐真正插入的行，正好是「本次新增」的定义。

        **没有候选时也必须 commit**：本方法同时承担「把 LLM 计量行落库」的
        责任。``WikiTokenUsageService.record`` 只 flush 不 commit（事务边界归
        调用方），而请求会话在 ``getDb`` 退出时直接关闭 → 未提交的计量行被回滚。
        于是「模型调用成功、但没有抽出任何能命中本体类的实体」这条路径——恰恰
        是最该被计量观察的路径——会把真金白银花掉的 token 记成 0。项目核心约束
        「每次 LLM 调用必须记录 Token 消耗与成本」在这里是硬的。
        """
        ids: list[int] = []
        if proposals:
            # 同一次发现里可能算出重复三元组（两篇同名条目、两个别名指向同一个类），
            # 先去重，避免把重复行塞进同一条 INSERT。
            deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
            for row in proposals:
                key = (
                    row["upstream_page_id"],
                    row["downstream_type"],
                    row["downstream_id"],
                    row["relation_type"],
                )
                deduped.setdefault(key, row)

            stmt = (
                pgInsert(KnowledgeRelation)
                .values(list(deduped.values()))
                .on_conflict_do_nothing(
                    index_elements=[
                        "upstream_page_id",
                        "downstream_type",
                        "downstream_id",
                        "relation_type",
                    ]
                )
                .returning(KnowledgeRelation.id)
            )
            ids = list((await session.execute(stmt)).scalars().all())

        # 单点提交：候选与计量行同生共死（见 docstring）
        await session.commit()
        if not ids:
            return []

        result = await session.execute(
            select(KnowledgeRelation)
            .where(KnowledgeRelation.id.in_(ids))
            .order_by(KnowledgeRelation.id)
        )
        return list(result.scalars().all())

def _candidateRow(
    upstreamPageId: str,
    downstreamType: str,
    downstreamId: str,
    relationType: str,
    confidence: Decimal,
) -> dict[str, Any]:
    """构造一条候选关系的插入行（``auto_detected=True``：自动产物要能追溯）。"""
    return {
        "upstream_page_id": upstreamPageId,
        "downstream_type": downstreamType,
        "downstream_id": downstreamId,
        "relation_type": relationType,
        "confidence": confidence,
        "auto_detected": True,
        "confirmed": False,
    }


__all__ = [
    "RelationDiscovery",
    "DiscoveryResult",
    "normalizeEntityName",
    "CLASS_EXTRACTION_SKIPPED",
    "CLASS_EXTRACTION_SUCCEEDED",
    "CLASS_EXTRACTION_FAILED",
    "MIN_REFERENCE_TITLE_CHARS",
    "RELATION_TYPE_REFERENCES",
    "RELATION_TYPE_DESCRIBES",
]
