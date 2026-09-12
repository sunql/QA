"""Wiki 知识条目服务（feat-wiki-knowledge，Phase 8 M1）。

职责边界：
- Page 的 CRUD + 分页 + 维度/结构阶段校验
- page_id 生成（业务专家不填时）
- **M3 起**：维度被人工覆盖时把这次覆盖写成学习反馈（``reclassify`` /
  ``updatePage`` → ``FeedbackLoop``）。反馈的是「用户如何处置已有建议」，
  本层**仍不调用 LLM** —— 分类/关系/冲突的模型调用在 learning 包下的
  各机制 service 里。

设计要点：
- ``dimension`` 校验用白名单（KNOWLEDGE_DIMENSIONS），拒绝任意字符串，
  避免脏数据污染后续覆盖度矩阵的聚合轴。
- 删除 Page 时靠 DB 的 ON DELETE CASCADE 连带清理 claim / evidence /
  relation，service 不做手工级联（少一次往返 + 不会漏）。
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.schemas import _UnsetType
from app.domain.wiki_models import (
    KNOWLEDGE_DIMENSIONS,
    STRUCTURE_STAGES,
    WIKI_PAGE_STATUSES,
    KnowledgeClaim,
    KnowledgeRelation,
    WikiPage,
)
from app.domain.wiki_schemas import WikiPageCreate, WikiPageUpdate
from app.services.learning.feedback_loop import (
    FeedbackLoop,
    decideClassificationAction,
    snapshotPageInput,
)
from app.services.messages_zh import (
    MSG_WIKI_PAGE_DIMENSION_INVALID,
    MSG_WIKI_PAGE_DUPLICATE,
    MSG_WIKI_PAGE_ID_INVALID,
    MSG_WIKI_PAGE_NOT_FOUND,
    MSG_WIKI_PAGE_STAGE_INVALID,
    MSG_WIKI_PAGE_STATUS_INVALID,
)

# page_id 允许的字符集：大写字母 / 数字 / 连字符 / 下划线。
# 生成的 ID 面向人读与 URL，故限制为 ASCII 安全子集。
_PAGE_ID_SANITIZE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_PAGE_ID_LEN = 64
_MAX_SLUG_LEN = 40

# ``dimension`` 的「未提供」哨兵。不能复用 None：显式传 null 是合法入参
# （= 打回该分类），与「没提这个字段」语义相反。
_UNSET_DIMENSION: object = object()

# ILIKE 的转义字符（必须同时传给 `.ilike(..., escape=...)`，否则 PG 不认）。
_LIKE_ESCAPE = "\\"

# 检索结果的默认条数。LLM/Agent 的上下文窗口不是垃圾桶：命中 200 条时全塞进去，
# 既挤掉真正相关的条目，也让「检索到了什么」无法复述。
DEFAULT_SEARCH_LIMIT = 10

# 「指称 → 条目」的候选上限。给候选人看的是「你要找的是哪个」，超过几个就不再是
# 澄清而是又一次检索了。
DEFAULT_CANDIDATE_LIMIT = 5


def _escapeLike(raw: str) -> str:
    """把用户输入里的 ILIKE 通配符转义成字面量。

    不转义的后果不是注入（参数仍走绑定），而是**语义失控**：一个 ``%`` 就让
    条件退化成「匹配任意内容」。用户搜「增长率 % 的算法」本意是找那个百分号，
    结果拿到全表。
    """
    return (
        raw.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )


def sanitizePageId(raw: str) -> str:
    """把**用户显式提供**的 page_id 收敛到与生成 ID 相同的字符集。

    为什么必须做：`generatePageId` 会过滤字符，但显式传入的 `pageId` 之前
    是原样落库的——大小写、斜杠、空格、控制字符都能进 DB。斜杠尤其麻烦：
    `GET /wiki/pages/{pageId}` 的路径参数匹配不到含 `/` 的 ID，条目就此
    「不可达」。同一字段两条路径两套规则是 bug，这里统一。
    """
    cleaned = _PAGE_ID_SANITIZE.sub("-", raw.strip()).strip("-")
    if not cleaned:
        raise ValidationError(MSG_WIKI_PAGE_ID_INVALID)
    return cleaned[:_MAX_PAGE_ID_LEN]


def generatePageId(title: str) -> str:
    """由标题生成稳定可读的 page_id（业务专家未显式提供时）。

    形如 ``PAGE-<SLUG>-<8位随机>``：短横线大写 slug 便于人读，
    随机后缀保证并发创建不撞车（title 可重复，不构成唯一键）。
    """
    slug = _PAGE_ID_SANITIZE.sub("-", title.strip()).strip("-").upper()
    slug = slug[:_MAX_SLUG_LEN].strip("-") or "UNTITLED"
    suffix = uuid.uuid4().hex[:8].upper()
    return f"PAGE-{slug}-{suffix}"[:_MAX_PAGE_ID_LEN]


def _assertDimension(dimension: str | None) -> None:
    """维度白名单校验（None 表示「待机制 1 填建议值」，合法）。"""
    if dimension is not None and dimension not in KNOWLEDGE_DIMENSIONS:
        raise ValidationError(MSG_WIKI_PAGE_DIMENSION_INVALID.format(dimension=dimension))


def _assertStage(stage: str) -> None:
    """结构阶段白名单校验。"""
    if stage not in STRUCTURE_STAGES:
        raise ValidationError(MSG_WIKI_PAGE_STAGE_INVALID.format(stage=stage))


def _assertStatus(status: str) -> None:
    """生命周期状态白名单校验（与维度同理：挡住脏值污染状态轴统计）。"""
    if status not in WIKI_PAGE_STATUSES:
        raise ValidationError(MSG_WIKI_PAGE_STATUS_INVALID.format(status=status))


class WikiPageService:
    """知识条目 CRUD + 分页。"""

    async def listPages(
        self,
        session: AsyncSession,
        *,
        dimension: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[WikiPage], int]:
        """分页列出知识条目，可按维度/状态过滤；返回 (rows, total)。"""
        conditions = []
        if dimension is not None:
            _assertDimension(dimension)
            conditions.append(WikiPage.dimension == dimension)
        if status is not None:
            _assertStatus(status)
            conditions.append(WikiPage.status == status)

        totalStmt = select(func.count()).select_from(WikiPage)
        rowsStmt = select(WikiPage).order_by(WikiPage.id.desc())
        for cond in conditions:
            totalStmt = totalStmt.where(cond)
            rowsStmt = rowsStmt.where(cond)

        total = (await session.execute(totalStmt)).scalar_one()
        rows = list(
            (await session.execute(rowsStmt.limit(limit).offset(offset))).scalars().all()
        )
        return rows, total

    async def getPage(self, session: AsyncSession, pageId: str) -> WikiPage:
        """按业务主键取 Page；不存在抛 NotFoundError。"""
        result = await session.execute(
            select(WikiPage).where(WikiPage.page_id == pageId)
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise NotFoundError(MSG_WIKI_PAGE_NOT_FOUND.format(pageId=pageId))
        return entity

    async def searchPages(
        self,
        session: AsyncSession,
        *,
        query: str,
        dimension: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
        offset: int = 0,
    ) -> tuple[list[WikiPage], int]:
        """按标题/正文模糊检索（ILIKE），返回 (rows, total)。

        与 ``listPages`` 分开而不是加一个可选 ``query`` 参数：那个方法的
        ``total`` 被分页器当「总量」用，而这里的 ``total`` 是「命中数」。
        同一个方法返回两种语义的 total，前端迟早会拿错。
        """
        needle = query.strip()
        if not needle:
            return [], 0

        pattern = f"%{_escapeLike(needle)}%"
        condition = or_(
            WikiPage.title.ilike(pattern, escape=_LIKE_ESCAPE),
            WikiPage.content.ilike(pattern, escape=_LIKE_ESCAPE),
        )
        if dimension is not None:
            _assertDimension(dimension)
            condition = and_(condition, WikiPage.dimension == dimension)

        total = (
            await session.execute(
                select(func.count()).select_from(WikiPage).where(condition)
            )
        ).scalar_one()
        rows = list(
            (
                await session.execute(
                    select(WikiPage)
                    .where(condition)
                    .order_by(WikiPage.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def findPageCandidates(
        self,
        session: AsyncSession,
        ref: str,
        *,
        limit: int = DEFAULT_CANDIDATE_LIMIT,
    ) -> list[WikiPage]:
        """把「人对条目的指称」解析成候选条目，按匹配强度排序、去重。

        三档由强到弱：``page_id`` 精确 → 标题精确 → 标题/正文模糊。分档的
        意义在于精确命中必须**压过**模糊命中 —— 用户抄了 ID 或全名时，混进
        一堆「正文里提过这个词」的条目会让人以为指称没生效。

        返回列表而非单个对象：命中多条时该由调用方决定是让用户澄清、还是
        按最近一条作答。本方法不替它做这个决定。
        """
        needle = ref.strip()
        if not needle:
            return []

        ordered: list[WikiPage] = []
        seen: set[str] = set()

        def _absorb(rows) -> None:
            for row in rows:
                if row.page_id not in seen:
                    seen.add(row.page_id)
                    ordered.append(row)

        _absorb(
            (
                await session.execute(select(WikiPage).where(WikiPage.page_id == needle))
            )
            .scalars()
            .all()
        )
        if ordered:
            # page_id 是唯一键 → 这一档最多命中一条，命中即决定性答案。
            # 继续往下捞只会把「用户抄了 ID」降级成「请从 N 条里挑一条」：
            # 别的条目正文里提过这个 ID 也会被 ILIKE 捞上来当候选。
            return ordered[:limit]

        _absorb(
            (
                await session.execute(
                    select(WikiPage)
                    .where(WikiPage.title == needle)
                    .order_by(WikiPage.id.desc())
                )
            )
            .scalars()
            .all()
        )
        if len(ordered) >= limit:
            return ordered[:limit]

        pattern = f"%{_escapeLike(needle)}%"
        _absorb(
            (
                await session.execute(
                    select(WikiPage)
                    .where(
                        or_(
                            WikiPage.title.ilike(pattern, escape=_LIKE_ESCAPE),
                            WikiPage.content.ilike(pattern, escape=_LIKE_ESCAPE),
                        )
                    )
                    .order_by(WikiPage.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return ordered[:limit]

    async def createPage(
        self,
        session: AsyncSession,
        dto: WikiPageCreate,
        *,
        createdByUserId: int | None = None,
    ) -> WikiPage:
        """创建知识条目（page_id 未给则生成；重名抛 ConflictError）。

        显式 pageId 的重复走「先查后插」+ 兜底捕获 IntegrityError：前者给
        出友好文案，后者挡住两个并发请求同时通过检查的竞态（只靠 SELECT
        判断会漏，第二个 INSERT 会以 500 冒出来）。与
        entity_mapping_service / feature_rule_service 的写法一致。
        """
        _assertDimension(dto.dimension)
        pageId = (
            sanitizePageId(dto.page_id) if dto.page_id else generatePageId(dto.title)
        )

        existing = await session.execute(
            select(WikiPage.id).where(WikiPage.page_id == pageId)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(MSG_WIKI_PAGE_DUPLICATE.format(pageId=pageId))

        entity = WikiPage(
            page_id=pageId,
            title=dto.title,
            content=dto.content,
            dimension=dto.dimension,
            authority_level=dto.authority_level,
            status="DRAFT",
            structure_stage="MARKDOWN",
            version="v1.0",
            created_by_user_id=createdByUserId,
        )
        session.add(entity)
        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            raise ConflictError(MSG_WIKI_PAGE_DUPLICATE.format(pageId=pageId)) from e
        await session.refresh(entity)
        return entity

    async def updatePage(
        self,
        session: AsyncSession,
        pageId: str,
        dto: WikiPageUpdate,
        *,
        updatedByUserId: int | None = None,
    ) -> WikiPage:
        """PATCH 更新：未提供的字段跳过（UNSET 哨兵）。

        ``dimension`` 一旦被提供（含显式 null），就构成对机制 1 建议的一次
        处置，会在**同一事务**里写一条 learning_feedback（见 ``reclassify``
        与 ``FeedbackLoop.recordClassification``）。
        """
        entity = await self.getPage(session, pageId)

        # 反馈的输入快照必须在**任何 setattr 之前**取。这个 PATCH 可能同时改
        # title/content 与 dimension：改完再读，快照里就是新标题配旧建议——
        # 一组从未同时出现过的「输入 → 输出」，写进学习闭环即污染训练数据。
        inputSnapshot = snapshotPageInput(entity)

        # 逐个字段读 `model_fields_set`，不用 `model_dump(exclude_unset=True)`：
        # _UnsetType 的 core schema 挂了 plain_serializer(lambda _: None)，
        # 在 `_UnsetType | str` 这类 union 里它总是先命中，导致 dump 把
        # **已提供**的字段也序列化成 None（会把 title 写空，触发 NOT NULL）。
        # 仓库既有 PATCH service（agent_tool_config_service）同样走逐字段判断。
        newDimension = _UNSET_DIMENSION
        for field in dto.model_fields_set:
            value = getattr(dto, field)
            if isinstance(value, _UnsetType):
                continue  # 显式传了哨兵 → 视为「未提供」
            if field == "dimension":
                _assertDimension(value)
                newDimension = value
            elif field == "structure_stage":
                _assertStage(value)
            elif field == "status":
                _assertStatus(value)
            setattr(entity, field, value)

        # 反馈与维度改写同事务：先 flush 再统一 commit，避免「维度改了但反馈没落」
        # 这类只有一半事实的中间态。无建议时 recordClassification 返回 None（不写）。
        if newDimension is not _UNSET_DIMENSION:
            await FeedbackLoop().recordClassification(
                session,
                page=entity,
                newDimension=newDimension,
                inputSnapshot=inputSnapshot,
                userId=updatedByUserId,
            )

        entity.updated_time = datetime.now(UTC)
        await session.commit()
        await session.refresh(entity)
        return entity

    async def reclassify(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        dimension: str | None,
        updatedByUserId: int | None = None,
    ) -> tuple[WikiPage, str | None]:
        """业务专家调整机制 1 的分类结论，返回 ``(条目, 本次记录的处置动作)``。

        动作在**改之前**由 ``decideClassificationAction`` 判定（依据是尚未被
        覆盖的建议），写入则统一交给 ``updatePage`` —— 判定函数是唯一口径，
        ``updatePage`` 是唯一写入点，本方法只负责把两者接起来。

        ``dimension=None`` 是合法入参（= 打回该分类）；「漏传」与「传 null」
        的区别由 DTO 层保证（``WikiReclassifyRequest.dimension`` 是必填键）。
        """
        page = await self.getPage(session, pageId)
        action = decideClassificationAction(page.auto_classification, dimension)
        updated = await self.updatePage(
            session,
            pageId,
            WikiPageUpdate(dimension=dimension),
            updatedByUserId=updatedByUserId,
        )
        return updated, action

    async def deletePage(self, session: AsyncSession, pageId: str) -> None:
        """删除 Page（DB 级联清理 claim / evidence / relation）。"""
        entity = await self.getPage(session, pageId)
        await session.delete(entity)
        await session.commit()

    async def listClaims(self, session: AsyncSession, pageId: str) -> list[KnowledgeClaim]:
        """列出某 Page 的全部事实原子（含证据，靠 selectin 预取）。"""
        await self.getPage(session, pageId)  # 404 早失败
        result = await session.execute(
            select(KnowledgeClaim)
            .where(KnowledgeClaim.page_id == pageId)
            .order_by(KnowledgeClaim.id)
        )
        return list(result.scalars().all())

    async def listRelations(
        self,
        session: AsyncSession,
        pageId: str,
        *,
        confirmedOnly: bool = False,
    ) -> list[KnowledgeRelation]:
        """列出某 Page 的传出关系；``confirmedOnly`` 只看已审核生效的。"""
        await self.getPage(session, pageId)  # 404 早失败
        stmt = (
            select(KnowledgeRelation)
            .where(KnowledgeRelation.upstream_page_id == pageId)
            .order_by(KnowledgeRelation.id)
        )
        if confirmedOnly:
            stmt = stmt.where(KnowledgeRelation.confirmed.is_(True))
        result = await session.execute(stmt)
        return list(result.scalars().all())
