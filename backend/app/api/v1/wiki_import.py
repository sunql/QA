"""Wiki 知识导入 API（feat-wiki-knowledge，Phase 8 M2）。

导入向导的四步契约：

1. ``GET  /wiki/import/models``       拉可选模型（标出哪些真能调）
2. ``POST /wiki/import/preview``      贴原文 → 切出草稿（不落库、不调模型）
3. ``POST /wiki/import/preview-file`` 上传文件 → 纯文本（不落库、不调模型）
4. ``POST /wiki/import/execute``      回传（可编辑的）草稿 → 建 Page + 可选分类
5. ``GET  /wiki/import/tasks``        导入作业台账

``preview`` 与 ``preview-file`` 是两条来源入口、**同一套切分**：上传只做
「文件 → 文本」（PDF/Word 抽取有损，先交用户核对），切分仍是 ``/preview``。

整组路由要求已认证（router 级 ``Depends(getCurrentUser)``），
``execute`` 另取当前用户写入 ``created_by_user_id`` 做溯源。

路由顺序注意：目前 5 条全是**静态路径**，无 ``/{param}`` 遮蔽问题。
后续若加 ``GET /wiki/import/tasks/{taskId}``，它必须排在 ``/tasks`` 之后
不会冲突（段数不同），但如果加的是 ``GET /wiki/import/{id}`` 就必须让
``/models``/``/preview``/``/preview-file``/``/execute``/``/tasks`` 都排它
前面——否则 "models" 会被当成 ``id`` 吞掉（agents.py ``/options`` 踩过这个坑）。
"""

from __future__ import annotations

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import CurrentUser, getCurrentUser, getDb
from app.domain.wiki_schemas import (
    WikiImportDraft,
    WikiImportExecuteRequest,
    WikiImportFileParseRead,
    WikiImportModelRead,
    WikiImportPreviewRead,
    WikiImportPreviewRequest,
    WikiImportTaskListRead,
    WikiImportTaskRead,
)
from app.infrastructure.rate_limit import limiter, rateLimitValue
from app.services.acl_service import ADMIN_ROLE
from app.services.document_parser import (
    DocumentParserError,
    UnsupportedFileTypeError,
)
from app.services.messages_zh import (
    MSG_WIKI_IMPORT_FILE_PARSE_FAILED,
    MSG_WIKI_IMPORT_FILE_TOO_LARGE,
    MSG_WIKI_IMPORT_FILE_TYPE_UNSUPPORTED,
)
from app.services.wiki_import_service import WikiImportService

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/wiki/import",
    tags=["wiki"],
    dependencies=[Depends(getCurrentUser)],
)

_importService = WikiImportService()

# 单次上传的字节上限。粘贴路径有 MAX_SOURCE_CHARS（1M 字符）兜着，上传路径
# 若不加限就等于留了一扇**同样输入、无上限**的后门：`await file.read()` 会
# 把整个文件读进内存，PDF 解析又是 CPU 密集的同步调用。10 MB 对
# 「一篇制度文档」绰绰有余，同时把内存与 CPU 的放大封顶。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
_MB = 1024 * 1024


@router.get("/models", response_model=list[WikiImportModelRead])
async def listImportModels(
    db: AsyncSession = Depends(getDb),
) -> list[WikiImportModelRead]:
    """列出导入可选模型，并标注 ``usable``（能否真正发起调用）。

    ``usable=false`` 的模型（未配 key / 已停用）仍返回，让向导能解释
    「为什么这个模型不能选」，而不是直接消失得莫名其妙。
    """
    rows = await _importService.listUsableModels(db)
    return [
        WikiImportModelRead(
            id=config.id,
            model_name=config.model_name,
            provider=config.provider,
            usable=usable,
            is_active=bool(config.is_active),
        )
        for config, usable in rows
    ]


@router.post("/preview", response_model=WikiImportPreviewRead)
async def previewImport(payload: WikiImportPreviewRequest) -> WikiImportPreviewRead:
    """把原始 Markdown 切成草稿（纯解析，不落库、不调用模型、不计费）。"""
    drafts = await _importService.preview(payload.source)
    return WikiImportPreviewRead(
        drafts=[
            WikiImportDraft(title=d.title, content=d.content) for d in drafts
        ],
        total=len(drafts),
    )


@router.post("/preview-file", response_model=WikiImportFileParseRead)
@limiter.limit(rateLimitValue)
async def previewImportFile(
    request: Request,
    file: UploadFile,
) -> WikiImportFileParseRead:
    """上传文件 → 纯文本 + 来源类型（不落库、不调模型）。

    只解析、不切分：抽取出的文本交回用户核对后，再走既有的 ``/preview``
    切分。这样两条来源路径（粘贴 / 上传）共用同一套切分逻辑。

    **限流 + 大小上限**：与 ``/execute`` 同因——都是用户输入入口。PDF 解析是
    CPU 密集的同步调用，文件字节数又是内存放大面，两者缺一不可。

    错误语义（刻意分三档，因为**处置动作不同**）：

    - 超过 ``MAX_UPLOAD_BYTES`` → 413
    - 格式不支持 → 422，提示「换格式」（含支持列表）
    - 解析失败（损坏/加密）→ 422，提示「换文件」

    **不回显底层异常原文**：``pypdf``/``python-docx`` 的异常会把临时路径、
    库内部状态带进响应，而这些是攻击面信息；前端也不会展示 ``detail``
    （它有自己的可行动文案），所以回显纯属净泄露。详细原因进服务端日志。
    """
    content = await file.read()
    mime = file.content_type or "application/octet-stream"
    fname = file.filename or "unknown"

    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=MSG_WIKI_IMPORT_FILE_TOO_LARGE.format(
                size=round(len(content) / _MB, 1), max=MAX_UPLOAD_BYTES // _MB
            ),
        )

    try:
        text, sourceType = await _importService.parseFile(content, mime, fname)
    except UnsupportedFileTypeError:
        # 用户可行动：换一种格式。文件名回显给他，便于对上自己选的文件。
        raise HTTPException(
            status_code=422,
            detail=MSG_WIKI_IMPORT_FILE_TYPE_UNSUPPORTED.format(filename=fname),
        ) from None
    except DocumentParserError:
        # 底层原因只进日志（含路径/库内部信息），响应只给可行动的提示。
        logger.exception("知识导入文件解析失败: filename=%s mime=%s", fname, mime)
        raise HTTPException(status_code=422, detail=MSG_WIKI_IMPORT_FILE_PARSE_FAILED) from None

    return WikiImportFileParseRead(text=text, source_type=sourceType)


@router.post(
    "/execute",
    response_model=WikiImportTaskRead,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit(rateLimitValue)
async def executeImport(
    request: Request,
    payload: WikiImportExecuteRequest,
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiImportTaskRead:
    """执行导入：建知识条目 + 可选机制 1 分类。

    分类失败不会让条目丢失（内容照常入库、``dimension`` 为空、任务记
    ``PARTIAL``）；模型不可用则整体 503。

    **限流**：本接口是唯一「用户输入 × LLM 调用」相乘的入口——一次请求最多
    ``MAX_IMPORT_DRAFTS`` 条草稿就是同等次数的付费调用。草稿条数上限挡住了
    单次放大，限流挡住单位时间内的重复放大。两者缺一不可（对齐 ``/chat``）。
    """
    task = await _importService.execute(db, payload, createdByUserId=user.dbUserId)
    return WikiImportTaskRead.model_validate(task)


@router.get("/tasks", response_model=WikiImportTaskListRead)
async def listImportTasks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(getCurrentUser),
    db: AsyncSession = Depends(getDb),
) -> WikiImportTaskListRead:
    """导入作业台账（倒序）。

    非 admin 只返回**自己发起**的任务：台账含 ``sourceRef``（用户自填的
    来源出处）与花费，全员可见等于横向泄露他人的作业清单与来源。
    """
    rows, total = await _importService.listTasks(
        db,
        limit=limit,
        offset=offset,
        viewerUserId=user.dbUserId,
        isAdmin=ADMIN_ROLE in (user.roles or ()),
    )
    return WikiImportTaskListRead(
        rows=[WikiImportTaskRead.model_validate(r) for r in rows], total=total
    )
