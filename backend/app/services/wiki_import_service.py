"""知识导入服务（feat-wiki-knowledge，Phase 8 M2）。

职责：

- **preview**：把原始 Markdown 按标题层级切成「知识条目草稿」，不落库、
  不调模型（纯函数 + 一次只读查询，方便用户先看再改）
- **execute**：按草稿建 Page，可选地对每条跑机制 1 分类，全程记 LLM 计量
  与任务台账
- **listUsableModels**：导入向导的选模下拉，**只返回真正能调的模型**
- **listTasks**：导入作业台账

失败语义（刻意选择）：

- 分类失败**不阻断**知识入库 —— 正文才是价值所在，分类是增强。
  分类挂掉的那条 `dimension=None`、`auto_classification=None`，
  任务汇总为 ``PARTIAL``，由 M3 的批量重分类补。
- 单条 Page 建失败（如显式 pageId 撞号）也只计失败数、继续下一条，
  最终 ``PARTIAL`` —— 批量导入不该被一条脏数据全灭。
- **按页提交**：每条草稿成功后 commit 一次，保证部分成功的进度落库，
  避免「跑完 99 条挂在第 100 条 → 全部回滚」。
- **兜底标记**：任何逃出单条 catch 的异常，都会先把任务落成 ``FAILED``
  再上抛，杜绝台账里挂一条永远 RUNNING 的僵尸任务。

任务状态语义（``SUCCEEDED`` / ``PARTIAL`` / ``FAILED``）：

- ``SUCCEEDED``：所有草稿入库成功，**且**（若开了自动分类）全部完成分类
- ``PARTIAL``：部分入库失败，**或**开了自动分类但有条目没分类成功
- ``FAILED``：一条都没入库成功，或导入被异常中止

「开了分类却没分类成」记 ``PARTIAL`` 而不是 ``SUCCEEDED``：用户显式选了
模型要求分类，拿到「成功」却零分类，等于把「模型没跑成」谎报成「模型认为
这些条目无维度」。降级信息同时写进 ``error_message``。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser
from app.domain.exceptions import (
    ConflictError,
    DomainError,
    ValidationError,
)
from app.domain.models import LlmConfig
from app.domain.wiki_learning_models import (
    IMPORT_SOURCE_TYPES,
    IMPORT_TASK_TYPES,
    WikiImportTask,
    WikiTokenUsage,
)
from app.domain.wiki_models import WikiPage
from app.domain.wiki_schemas import WikiImportDraft, WikiImportExecuteRequest
from app.infrastructure.llm.factory import createClient
from app.infrastructure.object_storage import (
    buildSourceObjectName,
    hashContent,
    putSourceObject,
)
from app.services.document_parser import parse_document
from app.services.wiki_catalog_registrar import WikiCatalogRegistrar
from app.services.learning.auto_classifier import AutoClassifier
from app.services.learning.llm_invoker import LearningLLMInvoker
from app.services.messages_zh import (
    MSG_WIKI_IMPORT_ABORTED,
    MSG_WIKI_IMPORT_ALL_FAILED,
    MSG_WIKI_IMPORT_CLASSIFY_INCOMPLETE,
    MSG_WIKI_IMPORT_MODEL_REQUIRED,
    MSG_WIKI_IMPORT_NO_DRAFTS,
    MSG_WIKI_IMPORT_SOURCE_TYPE_INVALID,
    MSG_WIKI_IMPORT_TASK_TYPE_INVALID,
    MSG_WIKI_PAGE_DUPLICATE,
)
from app.services.wiki_page_service import (
    WikiPageService,
    generatePageId,
    sanitizePageId,
)

logger = logging.getLogger(__name__)

# Markdown ATX 标题：`#` ~ `######` + 文本（行内 `#` 收尾也容忍）
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", re.MULTILINE)

# 草稿标题取自标题文本；超长截断与 DB 的 VARCHAR(200) 对齐
_MAX_TITLE_LEN = 200


@dataclass(frozen=True)
class ParsedDraft:
    """解析出的草稿（不可变）。"""

    title: str
    content: str


def _fallbackTitle(source: str) -> str:
    """没有标题时的兜底标题：取首个非空行，仍没有则用占位名。"""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:_MAX_TITLE_LEN]
    return "未命名知识条目"


def parseDrafts(source: str) -> list[ParsedDraft]:
    """把 Markdown 源文切成知识条目草稿。

    规则（确定性、可预测）：

    1. 取**最浅**的标题层级作为切分点；只在 `##` 与 `#` 之间选一层，
       避免 `#` 标题 + `##` 小节时把一篇文档切成碎片。
    2. 最浅层标题出现 **≥2 次** → 按它切分成多篇；标题前的引言归入第一篇。
    3. 否则整篇作为**一条**草稿，标题取最浅标题（无标题则取首行）。

    空源（全空白）→ ``ValidationError`` 422。
    """
    if not source.strip():
        raise ValidationError(MSG_WIKI_IMPORT_NO_DRAFTS)

    headings = [(len(m.group(1)), m.group(2).strip(), m.start()) for m in _HEADING_RE.finditer(source)]
    if not headings:
        return [ParsedDraft(title=_fallbackTitle(source), content=source.strip())]

    shallowest = min(level for level, _, _ in headings)
    cuts = [(text, pos) for level, text, pos in headings if level == shallowest]

    if len(cuts) < 2:
        # 单标题：整篇一条，标题用该标题（无标题分支已在上方返回）
        title = cuts[0][0] if cuts else _fallbackTitle(source)
        return [ParsedDraft(title=title[:_MAX_TITLE_LEN], content=source.strip())]

    # 首个最浅标题之前的引言（文档摘要/背景）不属于任何标题段。丢给第一篇
    # 而不是丢弃：它是正文的一部分，静默吞掉等于让用户以为导入完整。
    intro = source[: cuts[0][1]].strip()

    drafts: list[ParsedDraft] = []
    for index, (title, start) in enumerate(cuts):
        end = cuts[index + 1][1] if index + 1 < len(cuts) else len(source)
        body = source[start:end].strip()
        if index == 0 and intro:
            body = f"{intro}\n\n{body}".strip()
        if body:
            drafts.append(ParsedDraft(title=title[:_MAX_TITLE_LEN], content=body))
    return drafts or [ParsedDraft(title=_fallbackTitle(source), content=source.strip())]


def sourceTypeFromFilename(filename: str, mime_type: str = "") -> str:
    """把上传文件归一化成 ``IMPORT_SOURCE_TYPES`` 里的来源类型。

    只做**已支持格式**的映射：调用方（``parseFile``）先过 ``parse_document``，
    不支持的格式在那里就被挡住了，所以这里不必对 CSV 之类做兜底。
    无扩展名时退回按 MIME 判，仍是无法识别的就按 MARKDOWN —— 纯文本是
    ``parse_document`` 的默认分支，两者保持一致。
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext == "pdf" or mime_type == "application/pdf":
        return "PDF"
    if ext == "docx" or mime_type == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        return "WORD"
    return "MARKDOWN"


def _now() -> datetime:
    return datetime.now(UTC)


class WikiImportService:
    """导入任务的编排层。"""

    def __init__(
        self,
        *,
        classifier: AutoClassifier | None = None,
        catalog: WikiCatalogRegistrar | None = None,
    ) -> None:
        self._classifier = classifier or AutoClassifier()
        self._catalog = catalog or WikiCatalogRegistrar()
        self._pageService = WikiPageService()

    async def listUsableModels(self, session: AsyncSession) -> list[tuple[LlmConfig, bool]]:
        """列出可选的导入模型，并为每个标注「是否真的能调」。

        可用性 = ``is_active`` **且** ``createClient`` 能造出客户端。
        后者是关键：库里可能存在没配 key 的模型（如本地 Qwen 未设
        ``QWEN_API_KEY``），向导若把这种模型放进下拉，用户选完才在
        execute 时吃 503 —— 在这里提前标出来。
        """
        stmt = select(LlmConfig).order_by(LlmConfig.id)
        configs = list((await session.execute(stmt)).scalars().all())
        return [(c, bool(c.is_active) and createClient(c) is not None) for c in configs]

    async def preview(self, source: str) -> list[ParsedDraft]:
        """预检：切草稿，不落库、不调模型。"""
        return parseDrafts(source)

    async def parseFile(
        self, content: bytes, mime_type: str, filename: str
    ) -> tuple[str, str]:
        """把上传文件解析成纯文本 + 来源类型（不落库、不调模型）。

        只做「文件 → 文本」这一步，**切分仍走 ``preview``**：PDF/Word 的文本
        抽取是有损的（版式、表格会丢），把抽取结果先交回用户核对再切分，
        比一步到位地生成草稿更可控；也让两条来源路径共用同一套切分逻辑。

        Raises:
            DocumentParserError: 格式不支持或解析失败（API 层转 422）。
        """
        blocks = await parse_document(content, mime_type, filename)
        # 预览仍要扁平文本（前端按段落编辑），由带定位的块拼回。
        text = "\n\n".join(b.text for b in blocks)
        return text, sourceTypeFromFilename(filename, mime_type)

    async def persistSourceFile(
        self,
        session: AsyncSession,
        *,
        content: bytes,
        mime_type: str,
        filename: str,
        actor: CurrentUser,
    ) -> str:
        """留存源文件并登记 document_catalog，返回 ``storage_url``。

        内容寻址：同一份文件重复上传落到同一对象名，天然去重；catalog 登记
        再按 ``content_hash`` 幂等一层。两层都不依赖调用方传任何东西进来 ——
        哈希与 URL 全部由服务端从**真实字节**算出。catalog 行的 ``owner`` 由
        ``actor.departments[0]`` 派生（entity_mapping 同模式）。

        Raises:
            ObjectStorageError: MinIO 不可用或写入失败（API 层转 503）
        """
        contentHash = hashContent(content)
        objectName = buildSourceObjectName(contentHash, filename)
        storageUrl = putSourceObject(objectName, content, mime_type)
        await self._catalog.upsertByContentHash(
            session,
            document_name=filename,
            content_hash=contentHash,
            storage_url=storageUrl,
            actor=actor,
        )
        await session.commit()
        return storageUrl

    async def listTasks(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        viewerUserId: int | None = None,
        isAdmin: bool = False,
    ) -> tuple[list[WikiImportTask], int]:
        """导入任务台账（倒序）。

        非 admin 只看到**自己发起**的任务：任务台账带 ``source_ref``（用户
        自填的来源出处）与花费，全员可见等于横向泄露别人的作业清单与来源。
        admin 看全量（运维需要排查失败批次）。

        ``viewerUserId`` 为 None 且非 admin 时返回空集——宁可少看，也不
        因为「拿不到身份」而退化成全员可见。
        """
        conditions = []
        if not isAdmin:
            if viewerUserId is None:
                return [], 0
            conditions.append(WikiImportTask.created_by_user_id == viewerUserId)

        total = (
            await session.execute(
                select(func.count()).select_from(WikiImportTask).where(*conditions)
            )
        ).scalar_one()
        rows = list(
            (
                await session.execute(
                    select(WikiImportTask)
                    .where(*conditions)
                    .order_by(WikiImportTask.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return rows, total

    async def execute(
        self,
        session: AsyncSession,
        dto: WikiImportExecuteRequest,
        *,
        createdByUserId: int | None = None,
    ) -> WikiImportTask:
        """执行导入：建 Page（可选分类）+ 记任务台账与 LLM 计量。"""
        if dto.auto_classify and dto.model_id is None:
            raise ValidationError(MSG_WIKI_IMPORT_MODEL_REQUIRED)
        if not dto.drafts:
            raise ValidationError(MSG_WIKI_IMPORT_NO_DRAFTS)
        if dto.task_type not in IMPORT_TASK_TYPES:
            raise ValidationError(
                MSG_WIKI_IMPORT_TASK_TYPE_INVALID.format(taskType=dto.task_type)
            )
        if dto.source_type is not None and dto.source_type not in IMPORT_SOURCE_TYPES:
            raise ValidationError(
                MSG_WIKI_IMPORT_SOURCE_TYPE_INVALID.format(sourceType=dto.source_type)
            )

        invoker = (
            LearningLLMInvoker(
                session,
                primaryModelId=dto.model_id,
                fallbackModelId=dto.fallback_model_id,
            )
            if dto.auto_classify and dto.model_id is not None
            else None
        )
        # 动手前的可用性预检：无凭据的模型对**每一条**草稿都会失败。若等到
        # 循环里逐条降级，用户会拿到「全部导入成功、全部未分类」的假象。
        # 配置性错误必须在这里就炸成 503，且不留下半截任务。
        if invoker is not None:
            await invoker.preflight()

        task = WikiImportTask(
            task_type=dto.task_type,
            source_type=dto.source_type,
            source_ref=dto.source_ref,
            selected_model_id=dto.model_id,
            fallback_model_id=dto.fallback_model_id,
            status="RUNNING",
            page_ids=[],
            total_pages=len(dto.drafts),
            created_by_user_id=createdByUserId,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)

        if invoker is not None:
            invoker.bindImportTask(task.id)

        pageIds: list[str] = []
        failedPages = 0
        classifiedPages = 0

        try:
            for draft in dto.drafts:
                try:
                    # SAVEPOINT 隔离单条：失败只回滚这一条，外层 session 仍可用
                    # （裸 session.rollback() 会把 task 一起卷掉）。
                    async with session.begin_nested():
                        page, classified = await self._importOne(
                            session,
                            task,
                            draft,
                            invoker=invoker,
                            createdByUserId=createdByUserId,
                        )
                except (ConflictError, ValidationError) as e:
                    # 单条脏数据不毁整批：记失败、继续
                    logger.warning(
                        "导入第 %d 条失败: %s", len(pageIds) + failedPages + 1, e
                    )
                    failedPages += 1
                    continue

                pageIds.append(page.page_id)
                classifiedPages += 1 if classified else 0
                # 按页提交：部分成功的进度必须落库（避免最后一条挂了全回滚）
                await session.commit()
        except Exception as e:
            # 兜底：task 已在上面 commit 成 RUNNING，任何逃出单条 catch 的异常
            # 都必须先把它标成 FAILED 再上抛，否则台账里永远挂着一条「进行中」
            # 的僵尸任务（用户看到 RUNNING，实际早就没人跑了）。
            logger.exception("导入任务 %s 异常中止", task.id)
            await self._markFailedBestEffort(
                session, task, cause=e, pageIds=pageIds, failedPages=failedPages
            )
            raise

        task.page_ids = pageIds
        task.success_pages = len(pageIds)
        task.failed_pages = failedPages
        # 成本以 wiki_token_usage 台账为准（SUM），不在 Python 里累加：
        # 「调用成功但输出解析失败」这类路径也会留下计量行，靠累加会漏账。
        task.total_cost_usd = (
            await session.execute(
                select(func.coalesce(func.sum(WikiTokenUsage.cost), 0)).where(
                    WikiTokenUsage.import_task_id == task.id
                )
            )
        ).scalar_one()
        task.finished_time = _now()
        # 「一条都没分类成功」必须在任务上留痕：用户显式选了模型并要求分类，
        # 却拿到 SUCCEEDED + 全部 dimension 为空，是最容易被误读成「模型认为
        # 这些条目无维度」的状态。分类是增强，不该让状态码骗人。
        # 未开自动分类时缺口恒为 0 —— 没请求分类就没有「未分类」这回事。
        classificationGap = len(pageIds) - classifiedPages if invoker else 0
        if classificationGap > 0:
            task.error_message = MSG_WIKI_IMPORT_CLASSIFY_INCOMPLETE.format(
                count=classificationGap
            )
        if failedPages == 0 and classificationGap == 0:
            task.status = "SUCCEEDED"
        elif pageIds:
            task.status = "PARTIAL"
        else:
            task.status = "FAILED"
            task.error_message = MSG_WIKI_IMPORT_ALL_FAILED
        session.add(task)
        await session.commit()
        await session.refresh(task)

        logger.info(
            "导入任务 %s 完成: status=%s 成功=%d 失败=%d 分类成功=%d 成本=%s",
            task.id,
            task.status,
            task.success_pages,
            task.failed_pages,
            classifiedPages,
            task.total_cost_usd,
        )
        return task

    async def _markFailedBestEffort(
        self,
        session: AsyncSession,
        task: WikiImportTask,
        *,
        cause: Exception,
        pageIds: list[str],
        failedPages: int,
    ) -> None:
        """尽力把任务标记成 FAILED 落库；失败只记日志，绝不掩盖原始异常。

        中止的诱因可能正是「数据库连不上」，补的那一刀 commit 同样会炸。
        此时若让新异常冒出去，调用方看到的是 commit 的错误而不是真正的病根
        —— 所以这里吞掉，原始异常由调用方继续上抛。

        属性赋值放在 ``rollback()`` **之后**：rollback 会 expire 掉实例上的
        所有属性，先赋值再回滚等于白写（下次访问会把 DB 里的 RUNNING 读回来）。
        """
        # rollback 同样会 expire 主键，先取值供日志与 add 使用
        taskId = task.id
        try:
            # 先清干净：诱因若是「上一次 commit 失败」，session 正卡在
            # PendingRollbackError，不 rollback 的话 add+commit 会再炸一次。
            await session.rollback()
            task.status = "FAILED"
            task.error_message = MSG_WIKI_IMPORT_ABORTED
            # 已 commit 的页是真落库了的，台账要如实反映，否则「失败」看起来
            # 像一条都没进去，实际库里躺着一半。
            task.page_ids = pageIds
            task.success_pages = len(pageIds)
            task.failed_pages = failedPages
            task.finished_time = _now()
            session.add(task)
            await session.commit()
        except Exception:
            logger.exception(
                "导入任务 %s 标记 FAILED 失败（原始异常: %s）", taskId, cause
            )

    async def _importOne(
        self,
        session: AsyncSession,
        task: WikiImportTask,
        draft: WikiImportDraft,
        *,
        invoker: LearningLLMInvoker | None,
        createdByUserId: int | None,
    ) -> tuple[WikiPage, bool]:
        """建一条 Page（可选分类）。返回 (page, 是否给出了分类建议)。

        先查 page_id 冲突**再**调模型：撞号是纯本地就能判定的错误，
        不该白烧一次 LLM 调用。
        """
        title = draft.title
        content = draft.content
        # 显式 page_id 走与生成 ID 同一套字符集收敛（斜杠会让条目在
        # GET /wiki/pages/{pageId} 里不可达）
        rawPageId = draft.page_id
        pageId = sanitizePageId(rawPageId) if rawPageId else generatePageId(title)

        existing = await session.execute(
            select(WikiPage.id).where(WikiPage.page_id == pageId)
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictError(MSG_WIKI_PAGE_DUPLICATE.format(pageId=pageId))

        suggestion = None
        modelConfigId: int | None = None
        if invoker is not None:
            suggestion, modelConfigId = await self._classify(invoker, title, content)

        page = WikiPage(
            page_id=pageId,
            title=title,
            content=content,
            dimension=suggestion.primary if suggestion else None,
            auto_classification=suggestion.toDict() if suggestion else None,
            status="DRAFT",
            structure_stage="MARKDOWN",
            version="v1.0",
            imported_via_task_id=task.id,
            processing_model_id=modelConfigId,
            created_by_user_id=createdByUserId,
        )
        session.add(page)
        try:
            await session.flush()
        except IntegrityError as e:
            # 「先查后插」挡不住两个并发请求同时通过检查；第二个 INSERT 会
            # 以用户主键冲突出现在 flush 时。转成 ConflictError 让它走
            # 单条失败的分支（PARTIAL），而不是冒成 500 毁掉整批。
            raise ConflictError(MSG_WIKI_PAGE_DUPLICATE.format(pageId=pageId)) from e
        return page, suggestion is not None

    async def _classify(
        self,
        invoker: LearningLLMInvoker,
        title: str,
        content: str,
    ) -> tuple[object, int | None]:
        """跑机制 1 分类；失败降级为「无建议」，不让知识入库失败。

        返回 ``(建议或 None, 实际生效的模型 id)``。

        注意降级**不吞计量**：即便建议不可用，``invoker`` 已经写进 session 的
        ``wiki_token_usage`` 行仍会随后续 commit 落库（token 是真花了）。
        代价是解析失败的场景 ``processing_model_id`` 为 None —— 因为该字段
        语义是「哪个模型给出了这条知识的分类」，没给出就不该记。

        捕获的是 ``DomainError`` 而非单个 ``LLMUnavailableError``：
        ``LlmClientError``（网络/超时/响应非 JSON）是它的**兄弟**类而不是
        子类，只挡前者会让一次瞬时网络抖动冒到循环外、中止整批导入。
        ``NotFoundError``（配置被并发删除）同理——分类是增强，不值得毁掉
        已经成功的入库进度。
        """
        try:
            suggestion, result = await self._classifier.classify(
                invoker, title=title, content=content
            )
        except DomainError as e:
            logger.warning("分类失败，条目按未分类入库（title=%s）: %s", title, e)
            return None, None

        return suggestion, (result.modelConfigId if suggestion else None)


__all__ = [
    "WikiImportService",
    "ParsedDraft",
    "parseDrafts",
    "sourceTypeFromFilename",
]
