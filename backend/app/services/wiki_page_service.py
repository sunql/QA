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

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.schemas import _UnsetType
from app.domain.wiki_learning_models import (
    ProcessWorkflow,
    StructureSuggestion,
    WikiRuleExecutable,
)
from app.domain.wiki_models import (
    KNOWLEDGE_DIMENSIONS,
    STRUCTURE_STAGES,
    WIKI_PAGE_STATUSES,
    KnowledgeClaim,
    KnowledgeRelation,
    WikiPage,
)
from app.domain.wiki_schemas import WikiPageCreate, WikiPageUpdate
from app.infrastructure.object_storage import hashContent
from app.services.audit_service import AuditService
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

_audit = AuditService()

# page_id 允许的字符集：大写字母 / 数字 / 连字符 / 下划线。
# 生成的 ID 面向人读与 URL，故限制为 ASCII 安全子集。
_PAGE_ID_SANITIZE = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_PAGE_ID_LEN = 64
_MAX_SLUG_LEN = 40

# page_id 后缀的哈希位数（16 进制）。8 位 = 32 bit，对单库万级条目足够；真撞上
# 时不会静默丢知识 —— _importOne 还会比对 content_hash，不等则判为「同 ID 不同
# 内容」的冲突（计失败）而不是跳过。
_IDENTITY_HASH_LEN = 8

# 身份哈希的字段分隔符。用 NUL 而不是 ``|`` / ``-``：标题与来源里合法出现的
# 任何字符都不该能伪造出「字段边界」—— ``("ab", "c")`` 与 ``("a", "bc")``
# 必须哈希不同。
_IDENTITY_SEPARATOR = "\x00"

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


@dataclass(frozen=True)
class _CascadeCounts:
    """删除前统计到的级联行数（不可变；仅供响应回报，不参与删除决策）。"""

    claims: int = 0
    relations: int = 0
    suggestions: int = 0
    rules: int = 0
    workflows: int = 0


@dataclass(frozen=True)
class BatchDeleteResult:
    """批量删除的结果（不可变视图，service 的对外返回类型）。

    ``notFound`` 保序（与入参顺序一致，已去掉重复项），让调用方能原样回显
    「你没删掉的是这几条」，而不是给一个无序集合让人再去比对。
    """

    requested: int
    deletedPageIds: tuple[str, ...]
    notFound: tuple[str, ...]
    cascade: _CascadeCounts


def _dedupePreservingOrder(values: list[str]) -> list[str]:
    """保序去重。

    ``dict.fromkeys`` 依赖 Python 3.7+ 的「字典保插入序」保证 —— 用 ``set``
    去重会打乱顺序，而 ``notFound`` 是要原样回复给用户的清单。
    """
    return list(dict.fromkeys(values))


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


def contentHashOf(content: str) -> str:
    """正文的 SHA-256 十六进制摘要（小写，恒 64 字符）——转调 ``hashContent``。

    与 RAG 路径写进 ``document_catalog.content_hash`` 的**同一实现**
    （``app.infrastructure.object_storage.hashContent``），因此两处摘要可直接
    比对。为什么必须转调而不是再写一份：两份 ``sha256`` 实现迟早会在编码/
    大小写上漂移，而漂移了不会报错，只会让「同 ID 同内容 → 跳过」的比对永远
    不等、重复项静默入库。这里退化成薄包装，把等价性变成结构保证。
    """
    return hashContent(content.encode("utf-8"))


def _identityDigest(sourceRef: str, title: str, content: str) -> str:
    """身份哈希：``sha256(source_ref \\x00 title \\x00 content)`` 的十六进制。

    用**内容**（而非随机数）回答「这条知识是不是已经在了」。字段以 NUL 分隔，
    保证 ``("ab", "c")`` 与 ``("a", "bc")`` 不会撞成同一条。
    """
    payload = _IDENTITY_SEPARATOR.join((sourceRef, title, content))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def generatePageId(title: str, sourceRef: str = "", content: str = "") -> str:
    """由**内容**派生稳定可读的 page_id（业务专家未显式提供时）。

    形如 ``PAGE-<SLUG>-<8位身份哈希>``。与旧实现（``PAGE-<SLUG>-<8位随机>``）
    的唯一差别是**幂等**：同一份知识重跑得到同一个 ID，于是导入路径既有的
    「冲突检查 + ``uq_wiki_page_page_id``」原样生效，重复项根本落不了库 ——
    不需要引入第二套去重机制，也不需要有人去读 ``wiki_import_task.page_ids``。

    ``sourceRef`` 参与派生：同一标题同一正文但来源不同（两个部门各写了一份
    同样的模板）是**两条**知识。同一份文件重跑时来源也相同，故仍会合并。
    调用方负责把 ``None`` 归一成 ``""``（签名是 str，不接受 None）。

    注意 slug 沿用 ``_PAGE_ID_SANITIZE`` 的 ASCII 字符集，故**纯中文标题会折叠
    成 ``UNTITLED``**：唯一性由哈希后缀保证，可读性在中文场景是净损失。这是
    刻意保留既有字符集契约（page_id 要能安全放进 ``GET /wiki/pages/{pageId}``
    的路径段）的结果，不是疏忽。
    """
    slug = _PAGE_ID_SANITIZE.sub("-", title.strip()).strip("-").upper()
    slug = slug[:_MAX_SLUG_LEN].strip("-") or "UNTITLED"
    digest = _identityDigest(sourceRef, title, content)[:_IDENTITY_HASH_LEN].upper()
    return f"PAGE-{slug}-{digest}"[:_MAX_PAGE_ID_LEN]


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


# 删除审计 ``before`` 快照的字段表（顺序即 dict 顺序，单一事实源）。
#
# 三点刻意为之：
# - **不含 ``content``**：正文是 Markdown、长度上限 ``MAX_CONTENT_CHARS``，落进
#   ``audit_log.before_json`` 会让审计表随内容膨胀，而它对「谁在什么时候删了
#   什么」毫无贡献。真要看内容该去历史快照（``history_service``），不是审计。
# - ``id`` 与 ``page_id`` 都给：前者是 ``audit_log.entity_id`` 的对应物，
#   后者是人眼与 API 认的业务键。
# - 这份表同时用于**只取这几列**的 SELECT（见 ``deletePages``）：不能改写成
#   ``select(WikiPage)`` 再取属性 —— 查整实体会连带触发 ``claims`` /
#   ``evidence`` 的 ``lazy="selectin"`` 预加载（实测 1 条 SELECT 变 3 条），
#   等于为了记「删了哪几条」把**即将被删掉的子行**先全读进内存。取消批量
#   上限之后这条路径没有规模保护，所以必须限定列。
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "id",
    "page_id",
    "title",
    "dimension",
    "status",
    "structure_stage",
    "version",
    "authority_level",
)


def _wikiPageToDict(page: WikiPage) -> dict[str, Any]:
    """从已加载的实体取删除审计快照（单条删除路径用）。"""
    return {field: getattr(page, field) for field in _SNAPSHOT_FIELDS}


async def _countCascadeRows(
    session: AsyncSession, pageIds: list[str]
) -> _CascadeCounts:
    """统计这 5 张表里将被级联清掉的行数（**删除前**调用）。

    逐表 COUNT 而不是一条带 5 个标量子查询的 SQL：5 张表的过滤列都有索引
    （claim/relation/建议各有 ix_*，规则/流程是 page_id UNIQUE），
    展开写更直白，而这是低频管理操作，多 4 次往返无所谓。
    """
    if not pageIds:
        return _CascadeCounts()

    async def _count(model, column) -> int:
        return (
            await session.execute(
                select(func.count()).select_from(model).where(column.in_(pageIds))
            )
        ).scalar_one()

    return _CascadeCounts(
        claims=await _count(KnowledgeClaim, KnowledgeClaim.page_id),
        relations=await _count(KnowledgeRelation, KnowledgeRelation.upstream_page_id),
        suggestions=await _count(StructureSuggestion, StructureSuggestion.page_id),
        rules=await _count(WikiRuleExecutable, WikiRuleExecutable.page_id),
        workflows=await _count(ProcessWorkflow, ProcessWorkflow.page_id),
    )


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

    async def deletePage(
        self, session: AsyncSession, pageId: str, actor: CurrentUser
    ) -> None:
        """删除 Page（DB 级联清理 claim / evidence / relation）。

        审计与删除**同一事务**：``AuditService.record`` 只 ``session.add``，
        commit 由本方法收口 —— 于是不存在「删成功但审计没落」的窗口。
        快照必须在 ``session.delete`` **之前**取：delete 之后对象进 deleted
        状态，读属性会触发一次已无意义的刷新。
        """
        entity = await self.getPage(session, pageId)
        entityId = entity.id
        before = _wikiPageToDict(entity)
        await session.delete(entity)
        await _audit.record(
            session,
            entity_type="wiki_page",
            entity_id=entityId,
            action="DELETE",
            actor=actor.userId,
            actor_departments=actor.departments,
            before=before,
        )
        await session.commit()

    async def deletePages(
        self, session: AsyncSession, pageIds: list[str], actor: CurrentUser
    ) -> BatchDeleteResult:
        """批量删除知识条目（级联语义与 ``deletePage`` 完全一致）。

        与单条删除的唯一区别是「一批」以及由此带来的边界行为：

        - **重复 id 去重**（保序），不报错 —— 前端多选跨页保留很容易带重复项。
        - **部分成功**：不存在的 id 进 ``notFound`` 而**不整体失败**。并发下
          别的用户先删了同一条是正常情况，为此回滚整批会让用户永远删不掉；
          失败项也已显式回报，不是静默吞掉。
        - **级联只靠 DB**：``knowledge_claim`` / ``knowledge_relation`` /
          ``structure_suggestion`` / ``wiki_rule_executable`` /
          ``process_workflow`` 的 5 个 FK（迁移已落库）``delete_rule`` 全是
          ``CASCADE``，``evidence`` 二级挂在 claim 之下。故这里只发一条
          ``DELETE``，不手工删子行 —— 少 N 次往返，也不会漏表。
        - **入站关系刻意不删**：``knowledge_relation.downstream_id`` 是
          **多态业务键**（按 ``downstream_type`` 解释，无 DB 级 FK），指向本条
          的那些行属于**别的条目**。删掉它们等于替别人静默丢掉一条已确认关系；
          保留后由机制 3 的 GAP 检测标成「悬空引用」，处置权仍在业务专家手里。
          ``deletePage`` 同理，这里保持一致。
        - **审计**：与 ``deletePage`` 同款，**逐条**写 DELETE 审计（一条一实体）。
          ``audit_log`` 是以 ``entity_id`` 建索引的按实体记账表，
          ``AuditService.record`` 的契约就是「一次一个实体」；合成一条批审计会
          让「这条条目是谁删的」在按实体查审计时无解。
          ``notFound`` 的那些**不写** —— 伪造一条未曾发生的删除，审计就不再可信。

        返回的 ``cascade`` 是**删除前**统计的行数，供调用方给用户一个交代。
        """
        targets = _dedupePreservingOrder(pageIds)
        if not targets:
            # 正常调用被 DTO 的 min_length=1 挡住；这里是内部调用者的兜底：
            # 返回空结果比「用空 IN 列表去删」安全（后者语义可疑且无意义）。
            return BatchDeleteResult(
                requested=0, deletedPageIds=(), notFound=(), cascade=_CascadeCounts()
            )

        # 审计快照要 id + 各字段，且必须在下面的 bulk DELETE **之前**取
        # （删完 identity map 里的对象已失效，对象属性也读不出来了）。
        # 只取 _SNAPSHOT_FIELDS 这几列 —— 理由见该常量的注释（避免 selectin
        # 把即将被删的 claims/evidence 全读进内存）。
        snapshotRows = (
            await session.execute(
                select(*(getattr(WikiPage, f) for f in _SNAPSHOT_FIELDS)).where(
                    WikiPage.page_id.in_(targets)
                )
            )
        ).mappings().all()
        foundById = {row["page_id"]: dict(row) for row in snapshotRows}
        hit = [pageId for pageId in targets if pageId in foundById]
        missing = [pageId for pageId in targets if pageId not in foundById]
        # 保序对齐 targets，保证审计行顺序与用户看到的结果一致。
        snapshots = [(foundById[pageId]["id"], foundById[pageId]) for pageId in hit]

        cascade = await _countCascadeRows(session, hit)
        if hit:
            await session.execute(delete(WikiPage).where(WikiPage.page_id.in_(hit)))
            for entityId, before in snapshots:
                await _audit.record(
                    session,
                    entity_type="wiki_page",
                    entity_id=entityId,
                    action="DELETE",
                    actor=actor.userId,
                    actor_departments=actor.departments,
                    before=before,
                )
            await session.commit()

        return BatchDeleteResult(
            requested=len(targets),
            deletedPageIds=tuple(hit),
            notFound=tuple(missing),
            cascade=cascade,
        )

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
