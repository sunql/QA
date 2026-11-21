"""文档源文件读取（原文阅览的后端出口，设计稿需求 1/2/4）。

职责边界（**只做三件事**）：
1. **ACL 先行**：取 catalog 行必须过密级闸（复用 ``DocumentService.getDocument``——
   它已经实现「密级不在 clearance 内 ⇒ 403」），**不许**自己查库绕过；
2. 从 ``storage_url`` 解析对象名 → 读回字节（``object_storage.getSourceObject``
   —— 这个函数此前是**死代码**，本服务是它的第一个调用方）；
3. 组装响应所需的元信息（Content-Type / 文件名 / 体积 / 是否可内联）。

**为什么原文阅览必须比检索更严**：
检索只回 chunk 片段（几百字），原文是**整份文件** ⇒ 一个 403 的疏漏
＝ 整份 L3 合同泄漏。所以「先 ACL、后读字节」的顺序不可颠倒，
且**错误语义要分清**：403（无密级权限）/ 404（不存在或无源文件）/ 503（对象存储不可用）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.models import DocumentCatalog
from app.services.document_preview_kinds import (
    isInlineViewable,
    kindFor,
    mimeFor,
)


@dataclass(frozen=True)
class SourceFile:
    """一份可回传的源文件（字节 + 响应元信息）。"""

    filename: str
    contentType: str
    content: bytes
    previewKind: str
    inline: bool

    @property
    def size(self) -> int:
        return len(self.content)


def objectNameFromStorageUrl(storageUrl: str) -> str | None:
    """``s3://bucket/sources/xx/<hash>/<name>`` → ``sources/xx/<hash>/<name>``。

    对象名里的中文已 URL 编码 ⇒ 需 ``unquote``（MinIO 的 key 是解码后的原文）。
    """
    if not storageUrl.startswith("s3://"):
        return None
    rest = storageUrl[len("s3://"):]
    slash = rest.find("/")
    if slash == -1:
        return None
    objectName = rest[slash + 1:]
    if not objectName:
        return None
    return unquote(objectName)


def filenameFromStorageUrl(storageUrl: str, fallback: str) -> str:
    """从 storage_url 取原文件名（最后一段）；取不到 ⇒ 用 catalog 的名字。"""
    objectName = objectNameFromStorageUrl(storageUrl)
    if not objectName:
        return fallback
    name = objectName.rsplit("/", 1)[-1]
    return name or fallback


def contentDisposition(filename: str, *, inline: bool) -> str:
    """``Content-Disposition`` 头。

    **中文文件名必须用 RFC 5987**（``filename*=UTF-8''…``）：
    只给 ASCII 的 ``filename=`` 会被浏览器按 latin-1 解释 ⇒ 下载下来是乱码。
    """
    from urllib.parse import quote

    disposition = "inline" if inline else "attachment"
    # ASCII 回退名：**保留扩展名**（否则全中文名会退成 ".pdf"，某些浏览器
    # 当成隐藏文件或解析失败）；主名剥不到 ASCII 就用 "document" 占位。
    extension = PurePosixPath(filename).suffix
    stem = filename[: -len(extension)] if extension else filename
    asciiStem = stem.encode("ascii", "ignore").decode("ascii").strip()
    asciiFallback = f"{asciiStem or 'document'}{extension}"
    quoted = quote(filename, safe="")
    return f"{disposition}; filename=\"{asciiFallback}\"; filename*=UTF-8''{quoted}"


def buildSourceFile(catalog: DocumentCatalog, content: bytes) -> SourceFile:
    """catalog 行 + 字节 → 回传对象（**不改 catalog**，纯视图）。"""
    mimeType = catalog.mime_type
    filename = filenameFromStorageUrl(catalog.storage_url or "", catalog.document_name)
    resolvedMime = mimeFor(mimeType, filename)
    kind = catalog.preview_kind or kindFor(mimeType, filename)
    return SourceFile(
        filename=filename,
        contentType=resolvedMime,
        content=content,
        previewKind=kind,
        inline=isInlineViewable(mimeType, filename),
    )


async def readSourceBytes(storageUrl: str | None) -> bytes | None:
    """读回源文件字节；对象不存在 ⇒ None（正常未命中）；存储故障 ⇒ 抛。"""
    from app.infrastructure.object_storage import (
        ObjectStorageError,
        getSourceObject,
    )

    if not storageUrl:
        return None
    objectName = objectNameFromStorageUrl(storageUrl)
    if not objectName:
        return None
    try:
        return getSourceObject(objectName)
    except ObjectStorageError as exc:
        # 「对象不存在」与「基础设施故障」要分清：前者是正常未命中（返回 None），
        # 后者必须冒泡（API 层转 503）——不能都吞成「文件没了」。
        if _isNotFound(exc):
            return None
        raise


def _isNotFound(error: Exception) -> bool:
    """对象存储的「不存在」判定（不同 SDK 的错误码/文案不同）。"""
    text = str(error).lower()
    return "no such key" in text or "not found" in text or "nosuchkey" in text


async def loadSourceFile(
    session: AsyncSession, catalog: DocumentCatalog
) -> SourceFile | None:
    """取源文件；没有源文件（storage_url 空 / 对象不存在）⇒ None。"""
    content = await readSourceBytes(catalog.storage_url)
    if content is None:
        return None
    return buildSourceFile(catalog, content)
