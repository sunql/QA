"""机制 6：覆盖度自感知（feat-wiki-knowledge，Phase 8 M7）。

回答一个问题：**「系统到底掌握了哪些知识、缺口在哪」**。答案是
(dimension × ontology_class × domain) 三个轴上的一个矩阵。

## 一条知识落在哪一格

判据是**已确认的**「条目 → 本体类」关系：

    wiki_page.dimension  决定维度轴
    knowledge_relation   （downstream_type='ONTOLOGY_CLASS'，confirmed）决定类轴
    class_domain_mapping 决定域轴

三点必须说清：

1. **只认已确认的关系。** 机制 2 自动发现的关系是**候选**（``confirmed=False``）。
   拿候选去算覆盖度，等于让机器的猜测把看板刷成绿色 —— 而看板的全部价值就是
   「这个数字可信」。宁可先全红，也不要假绿：关系一被确认，下一轮刷新自动变绿。
2. **``knowledge_relation.downstream_id`` 存的是 ``class_name``（字符串），不是
   数值 id**（见 relation_discovery：类目录是「归一化名 → class_name」的映射）。
   故这里的连接键是 ``class_name``；而 ``coverage_cell`` 存 ``ontology_class_id``
   —— ``class_name`` 不唯一（本体类可有多版本行），拿它当矩阵的键会在本体演进后
   把两代类混成一格。
3. **``valid_to IS NULL`` 过滤软删的类。** 已下线的类不该出现在覆盖度看板上，
   与 relation_discovery 的类目录口径保持一致 —— 两处口径一旦不同，就会出现
   「关系匹配得到、覆盖度算不到」的鬼影。

## 为什么整体重算而不是增量

见 ``wiki_coverage_models`` 模块说明。一句话：增量会漂移且无人能发现，重算自愈。
全量规模 = 类数 × 维度数 × 域数（当前上限千级），比维护增量便宜得多。

## 孤儿格的自愈

每轮刷新给写出的行盖同一个时间戳，**再把时间戳更早的行删掉**。这样「类被软删」
「域映射被摘掉」「维度被改掉」留下旧格都会在下一轮消失，而不是永远挂在那里。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy import text as saText
from sqlalchemy.dialects.postgresql import insert as pgInsert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import NotFoundError, ValidationError
from app.domain.models import OntologyClass, _utcnow
from app.domain.wiki_coverage_models import (
    COVERAGE_ACTIVE_STATUSES,
    DOMAIN_MAX_LENGTH,
    DOMAIN_UNASSIGNED,
    GAP_UNLINKED,
    ClassDomainMapping,
    CoverageCell,
)
from app.domain.wiki_models import (
    KNOWLEDGE_DIMENSIONS,
    KnowledgeRelation,
    WikiPage,
)
from app.services.messages_zh import (
    MSG_WIKI_COVERAGE_CLASS_NOT_FOUND,
    MSG_WIKI_COVERAGE_DOMAIN_EMPTY,
    MSG_WIKI_COVERAGE_DOMAIN_TOO_LONG,
    MSG_WIKI_COVERAGE_MAPPING_NOT_FOUND,
)

logger = logging.getLogger(__name__)

# 缺口清单的默认条数上限。缺口在初始状态下可能上千（每个未覆盖的格就是一条），
# 不设上限等于把一个「给人看的待办列表」变成一次全表导出。
DEFAULT_GAP_LIMIT = 200
MAX_GAP_LIMIT = 1000


@dataclass(frozen=True)
class CoverageRefreshResult:
    """一次全量刷新的结果（不可变）。"""

    cellCount: int
    classCount: int
    unmappedClassCount: int
    removedCount: int
    refreshedAt: datetime


@dataclass(frozen=True)
class CoverageGap:
    """一条缺口：某一格没有可用知识，或一批知识没有归属。

    ``pageCount`` 与 ``approvedCount`` 一起给出，是为了让「完全没写过」与
    「写过但没审到生效」在列表里一次就分得清 —— 两者的下一步动作不同。
    """

    dimension: str
    ontologyClassId: int | None
    className: str | None
    domain: str
    status: str
    pageCount: int
    approvedCount: int


@dataclass(frozen=True)
class UnlinkedPagesGap:
    """「有维度但没挂到任何业务对象」的条目数（单条汇总，不展开成 N 行）。"""

    pageCount: int
    dimensions: dict[str, int]


class CoverageTracker:
    """机制 6：覆盖度刷新、矩阵读取、缺口清单、域映射维护。"""

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    async def refresh(self, session: AsyncSession) -> CoverageRefreshResult:
        """全量重算覆盖度矩阵（幂等，可反复调用）。"""
        runAt = _utcnow()
        classes = await _loadLiveClasses(session)
        domainsByClass = await _loadDomains(session, [c["id"] for c in classes])
        counts = await _loadCounts(session)

        rows = _buildCells(classes, domainsByClass, counts, runAt)
        if rows:
            await _upsertCells(session, rows)

        # 清理本轮没写到的旧格（见模块说明「孤儿格的自愈」）。
        removed = await session.execute(
            delete(CoverageCell).where(CoverageCell.last_refreshed_at < runAt)
        )
        await session.commit()

        unmapped = sum(1 for c in classes if c["id"] not in domainsByClass)
        logger.info(
            "覆盖度刷新完成：%d 格 / %d 类（%d 类未标域）/ 清理 %d 格",
            len(rows), len(classes), unmapped, removed.rowcount or 0,
        )
        return CoverageRefreshResult(
            cellCount=len(rows),
            classCount=len(classes),
            unmappedClassCount=unmapped,
            removedCount=removed.rowcount or 0,
            refreshedAt=runAt,
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def listCells(
        self,
        session: AsyncSession,
        *,
        domain: str | None = None,
        dimension: str | None = None,
        coverageStatus: str | None = None,
    ) -> list[tuple[CoverageCell, str]]:
        """读矩阵（带类名），可按域/维度/状态过滤。"""
        statement = (
            select(CoverageCell, OntologyClass.class_name)
            .join(OntologyClass, OntologyClass.id == CoverageCell.ontology_class_id)
            .order_by(
                CoverageCell.domain, OntologyClass.class_name, CoverageCell.dimension
            )
        )
        if domain:
            statement = statement.where(CoverageCell.domain == _normalizeDomain(domain))
        if dimension:
            statement = statement.where(CoverageCell.dimension == dimension)
        if coverageStatus:
            statement = statement.where(CoverageCell.coverage_status == coverageStatus)
        return list((await session.execute(statement)).all())

    async def listGaps(
        self,
        session: AsyncSession,
        *,
        domain: str | None = None,
        includeUnassigned: bool = False,
        limit: int = DEFAULT_GAP_LIMIT,
    ) -> list[CoverageGap]:
        """缺口清单：不是 COMPLETE 的格，按「缺得最狠」在前排序。

        默认**排除** ``UNASSIGNED`` 域：一个没标业务域的类会产出「维度数」条
        缺口（8 条），几十个未标类的类就能把待办列表淹掉。而「这个类还没标域」
        是一条元待办，不是 8 条 —— 它由 ``unmappedClassCount`` 单独汇报。
        ``includeUnassigned=True`` 时才把它们放进来（界面上「显示未分配」开关）。

        排序把 ``MISSING`` 排在 ``PARTIAL`` 前面：完全没写过比「写了没审」更急。
        """
        statement = (
            select(CoverageCell, OntologyClass.class_name)
            .join(OntologyClass, OntologyClass.id == CoverageCell.ontology_class_id)
            .where(CoverageCell.coverage_status != "COMPLETE")
            .order_by(CoverageCell.domain, OntologyClass.class_name)
        )
        if not includeUnassigned:
            statement = statement.where(CoverageCell.domain != DOMAIN_UNASSIGNED)
        if domain:
            statement = statement.where(CoverageCell.domain == _normalizeDomain(domain))
        statement = statement.limit(_boundedLimit(limit))

        rows = (await session.execute(statement)).all()
        gaps = [
            CoverageGap(
                dimension=cell.dimension,
                ontologyClassId=cell.ontology_class_id,
                className=className,
                domain=cell.domain,
                status=cell.coverage_status,
                pageCount=cell.page_count,
                approvedCount=cell.approved_count,
            )
            for cell, className in rows
        ]
        return sorted(gaps, key=_gapSortKey)

    async def summarize(self, session: AsyncSession) -> dict[str, Any]:
        """看板顶部的一行汇总（按状态计数 + 未标域类数）。"""
        rows = (
            await session.execute(
                select(CoverageCell.coverage_status, CoverageCell.domain)
            )
        ).all()
        byStatus: dict[str, int] = {}
        unmapped = 0
        for status, domain in rows:
            byStatus[status] = byStatus.get(status, 0) + 1
            if domain == DOMAIN_UNASSIGNED:
                unmapped += 1
        return {
            "totalCells": len(rows),
            "byStatus": byStatus,
            "unassignedCells": unmapped,
        }

    async def countUnlinkedPages(self, session: AsyncSession) -> UnlinkedPagesGap:
        """统计「有维度但没挂到任何已确认业务对象」的条目。

        这是覆盖度矩阵**看不见**的那部分缺口（矩阵的每一格都挂了类），却常常是
        初始阶段最大的一个：条目都在，只是 Agent 按业务对象根本检索不到它们。
        """
        linked = (
            select(KnowledgeRelation.upstream_page_id)
            .where(
                KnowledgeRelation.downstream_type == "ONTOLOGY_CLASS",
                KnowledgeRelation.confirmed.is_(True),
                KnowledgeRelation.rejected_at.is_(None),
            )
            .distinct()
        )
        rows = (
            await session.execute(
                select(WikiPage.dimension, saText("COUNT(*)"))
                .where(
                    WikiPage.dimension.is_not(None),
                    WikiPage.page_id.not_in(linked),
                )
                .group_by(WikiPage.dimension)
            )
        ).all()
        byDimension = {dim: int(count) for dim, count in rows}
        return UnlinkedPagesGap(
            pageCount=sum(byDimension.values()), dimensions=byDimension
        )

    # ------------------------------------------------------------------
    # 域映射维护
    # ------------------------------------------------------------------

    async def assignDomain(
        self,
        session: AsyncSession,
        ontologyClassId: int,
        domain: str,
        *,
        userId: int | None = None,
    ) -> ClassDomainMapping:
        """给本体类标注一个业务域（幂等：重复标注返回既有行）。"""
        normalized = _normalizeDomain(domain)
        await _requireLiveClass(session, ontologyClassId)

        # ON CONFLICT DO NOTHING 之后**统一回查**，而不是用 RETURNING 分流：
        # 「新插入」与「已存在」两条路径要返回的东西完全一样（那一行），
        # 分流只是把同一件事写两遍，还得处理 RETURNING 空结果的解释。
        statement = (
            pgInsert(ClassDomainMapping)
            .values(
                ontology_class_id=ontologyClassId,
                domain=normalized,
                created_by_user_id=userId,
            )
            .on_conflict_do_nothing(index_elements=["ontology_class_id", "domain"])
        )
        await session.execute(statement)
        await session.commit()

        return (
            await session.execute(
                select(ClassDomainMapping).where(
                    ClassDomainMapping.ontology_class_id == ontologyClassId,
                    ClassDomainMapping.domain == normalized,
                )
            )
        ).scalar_one()

    async def removeDomain(
        self, session: AsyncSession, ontologyClassId: int, domain: str
    ) -> None:
        """摘掉一个域标注（没有则 404，不静默成功）。"""
        normalized = _normalizeDomain(domain)
        result = await session.execute(
            delete(ClassDomainMapping).where(
                ClassDomainMapping.ontology_class_id == ontologyClassId,
                ClassDomainMapping.domain == normalized,
            )
        )
        if not result.rowcount:
            raise NotFoundError(
                MSG_WIKI_COVERAGE_MAPPING_NOT_FOUND.format(
                    classId=ontologyClassId, domain=normalized
                )
            )
        await session.commit()

    async def listDomainMappings(
        self, session: AsyncSession
    ) -> list[tuple[ClassDomainMapping, str]]:
        """列出全部域映射（带类名），供「域标注」界面对账。"""
        statement = (
            select(ClassDomainMapping, OntologyClass.class_name)
            .join(
                OntologyClass, OntologyClass.id == ClassDomainMapping.ontology_class_id
            )
            .order_by(OntologyClass.class_name, ClassDomainMapping.domain)
        )
        return list((await session.execute(statement)).all())

    async def listDomains(self, session: AsyncSession) -> list[str]:
        """已使用过的业务域词表（去重，含未分配哨兵）。

        刻意从**数据里**取而不是维护一份常量词表：域是分类轴，知识积累到新领域
        时就该冒出新域（见 ``wiki_coverage_models`` 为什么不复用
        ``AGENT_DATA_DOMAINS``）。写死一份清单等于把那句话反着做。
        """
        rows = (
            await session.execute(
                select(ClassDomainMapping.domain).distinct().order_by(
                    ClassDomainMapping.domain
                )
            )
        ).scalars().all()
        return list(rows)


# ---------------------------------------------------------------------------
# 内部：读取与拼装
# ---------------------------------------------------------------------------


async def _loadLiveClasses(session: AsyncSession) -> list[dict[str, Any]]:
    """生效中的本体类（``valid_to IS NULL``），同名只取最早一行。

    同名多行取最早，与 ``relation_discovery._loadClassCatalog`` 的 ``setdefault``
    + ``ORDER BY id`` 同款 —— 两处的「同名类指哪一个」必须一致，否则关系能挂上
    而覆盖度算到另一代类上。
    """
    rows = (
        await session.execute(
            select(OntologyClass.id, OntologyClass.class_name)
            .where(OntologyClass.valid_to.is_(None))
            .order_by(OntologyClass.id)
        )
    ).all()
    seen: dict[str, int] = {}
    for classId, className in rows:
        seen.setdefault(className, classId)
    return [{"id": classId, "name": name} for name, classId in seen.items()]


async def _loadDomains(
    session: AsyncSession, classIds: Sequence[int]
) -> dict[int, list[str]]:
    """类 id → 域列表（无标注的类不出现在结果里）。"""
    if not classIds:
        return {}
    rows = (
        await session.execute(
            select(ClassDomainMapping.ontology_class_id, ClassDomainMapping.domain)
            .where(ClassDomainMapping.ontology_class_id.in_(list(classIds)))
            .order_by(ClassDomainMapping.domain)
        )
    ).all()
    mapping: dict[int, list[str]] = {}
    for classId, domain in rows:
        mapping.setdefault(classId, []).append(domain)
    return mapping


async def _loadCounts(session: AsyncSession) -> dict[tuple[str, str], dict[str, int]]:
    """按 (维度, class_name) 聚合条目数 / 已生效数 / 已失效数。

    ``COUNT(DISTINCT page_id)`` 而非 ``COUNT(*)``：一条知识可能对同一个类有
    多条关系（不同 relation_type），去重才不会把一条知识数成三条。
    """
    active = ", ".join(f"'{s}'" for s in COVERAGE_ACTIVE_STATUSES)
    activeFilter = (
        f"p.status IN ({active}) AND (p.valid_to IS NULL OR p.valid_to > NOW())"
    )
    staleFilter = "p.status = 'EXPIRED' OR (p.valid_to IS NOT NULL AND p.valid_to <= NOW())"

    rows = (
        await session.execute(
            saText(
                f"""
                SELECT p.dimension AS dimension,
                       r.downstream_id AS class_name,
                       COUNT(DISTINCT p.page_id) AS page_count,
                       COUNT(DISTINCT p.page_id) FILTER (WHERE {activeFilter}) AS approved_count,
                       COUNT(DISTINCT p.page_id) FILTER (WHERE {staleFilter}) AS stale_count
                FROM wiki_page p
                JOIN knowledge_relation r
                  ON r.upstream_page_id = p.page_id
                 AND r.downstream_type = 'ONTOLOGY_CLASS'
                 AND r.confirmed IS TRUE
                 AND r.rejected_at IS NULL
                WHERE p.dimension IS NOT NULL
                GROUP BY p.dimension, r.downstream_id
                """  # noqa: S608 - 拼接的是本模块的常量白名单，无外部输入
            )
        )
    ).mappings().all()
    return {
        (row["dimension"], row["class_name"]): {
            "page": int(row["page_count"]),
            "approved": int(row["approved_count"]),
            "stale": int(row["stale_count"]),
        }
        for row in rows
    }


def _buildCells(
    classes: Sequence[dict[str, Any]],
    domainsByClass: dict[int, list[str]],
    counts: dict[tuple[str, str], dict[str, int]],
    runAt: datetime,
) -> list[dict[str, Any]]:
    """拼出完整网格：每个类 × 每个维度 × 每个域（无域则 UNASSIGNED）。"""
    rows: list[dict[str, Any]] = []
    for entry in classes:
        domains = domainsByClass.get(entry["id"]) or [DOMAIN_UNASSIGNED]
        for domain in domains:
            for dimension in KNOWLEDGE_DIMENSIONS:
                stat = counts.get((dimension, entry["name"]))
                pageCount = stat["page"] if stat else 0
                approvedCount = stat["approved"] if stat else 0
                staleCount = stat["stale"] if stat else 0
                rows.append(
                    {
                        "dimension": dimension,
                        "ontology_class_id": entry["id"],
                        "domain": domain,
                        "page_count": pageCount,
                        "approved_count": approvedCount,
                        "coverage_status": _statusFor(
                            pageCount, approvedCount, staleCount
                        ),
                        "last_refreshed_at": runAt,
                    }
                )
    return rows


def _statusFor(pageCount: int, approvedCount: int, staleCount: int) -> str:
    """由计数推出覆盖状态（四态各有不同的修复动作，见 ``COVERAGE_STATUSES``）。

    顺序即优先级：一条都没有 → 全失效 → 一条都没生效 → 部分生效 → 全覆盖。
    """
    if pageCount == 0:
        return "MISSING"
    if approvedCount == 0:
        return "OUTDATED" if staleCount > 0 else "PARTIAL"
    if approvedCount < pageCount:
        return "PARTIAL"
    return "COMPLETE"


async def _upsertCells(session: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """UPSERT 全部单元格（唯一键 = dimension + class_id + domain）。"""
    statement = pgInsert(CoverageCell).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["dimension", "ontology_class_id", "domain"],
            set_={
                "page_count": statement.excluded.page_count,
                "approved_count": statement.excluded.approved_count,
                "coverage_status": statement.excluded.coverage_status,
                "last_refreshed_at": statement.excluded.last_refreshed_at,
            },
        )
    )


async def _requireLiveClass(session: AsyncSession, classId: int) -> None:
    """类必须存在且未软删（否则 404 而不是留下一条指向墓碑的标注）。"""
    found = (
        await session.execute(
            select(OntologyClass.id).where(
                OntologyClass.id == classId, OntologyClass.valid_to.is_(None)
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise NotFoundError(
            MSG_WIKI_COVERAGE_CLASS_NOT_FOUND.format(classId=classId)
        )


def _normalizeDomain(domain: str) -> str:
    """域归一化：strip + upper + 长度校验（与 ``normalizeAgentDomain`` 同手法）。

    **不设白名单**：域是分类轴不是授权词表，锁死取值等于把「不锁业务域」做废。
    详见 ``wiki_coverage_models`` 模块说明。
    """
    normalized = (domain or "").strip().upper()
    if not normalized:
        raise ValidationError(MSG_WIKI_COVERAGE_DOMAIN_EMPTY)
    if len(normalized) > DOMAIN_MAX_LENGTH:
        raise ValidationError(
            MSG_WIKI_COVERAGE_DOMAIN_TOO_LONG.format(
                domain=normalized, maxLength=DOMAIN_MAX_LENGTH
            )
        )
    return normalized


def _boundedLimit(limit: int) -> int:
    """把 limit 收进 [1, MAX_GAP_LIMIT]，非法值退回默认。"""
    if limit <= 0:
        return DEFAULT_GAP_LIMIT
    return min(limit, MAX_GAP_LIMIT)


def _gapSortKey(gap: CoverageGap) -> tuple[int, str, str]:
    """MISSING 在前（完全没写过比「写了没审」更急），再按域/类名稳定排序。"""
    return (0 if gap.status == "MISSING" else 1, gap.domain, gap.className or "")


__all__ = [
    "CoverageTracker",
    "CoverageRefreshResult",
    "CoverageGap",
    "UnlinkedPagesGap",
    "DEFAULT_GAP_LIMIT",
    "MAX_GAP_LIMIT",
    "GAP_UNLINKED",
]
