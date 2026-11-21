"""文档预览类别（设计稿需求 4「通用查看方式」的单一事实源）。

为什么落库而不是每次推断：
- 推断规则会变（新增格式、调整优先级）⇒ 落库后**已入库文档的呈现不变**；
- 目录页要显示「预览类别」与「是否已向量化」，不能每次都去解文件名。

六类的渲染方式（服务端统一渲染成 HTML，PDF 例外走原件直出）：

============  ==========================  =========================================
类别          对应格式                      渲染
============  ==========================  =========================================
``TEXT``      txt / csv                   ``<pre>`` 转义
``MARKDOWN``  md / markdown               ``markdown`` 库渲染 + HTML 消毒
``HTML``      html / htm / svg            剥脚本后 iframe sandbox 包裹
``PDF``       pdf                          **原件直出**（浏览器原生查看器，保真 100%）
``OFFICE``    docx / xlsx / xls / pptx    用已有解析库渲染结构（标题/表格/页）
``IMAGE``     png / jpg / jpeg / gif       ``<img>``（原件字节）
============  ==========================  =========================================
"""

from __future__ import annotations

KIND_TEXT = "TEXT"
KIND_MARKDOWN = "MARKDOWN"
KIND_HTML = "HTML"
KIND_PDF = "PDF"
KIND_OFFICE = "OFFICE"
KIND_IMAGE = "IMAGE"

#: catalog.preview_kind 的合法取值（迁移 0163 的 CHECK 约束同源）。
PREVIEW_KINDS: frozenset[str] = frozenset(
    {KIND_TEXT, KIND_MARKDOWN, KIND_HTML, KIND_PDF, KIND_OFFICE, KIND_IMAGE}
)

#: 扩展名 → 预览类别（**唯一**映射表；MIME 优先，扩展名兜底）。
_EXTENSION_KINDS: dict[str, str] = {
    "txt": KIND_TEXT,
    "text": KIND_TEXT,
    "csv": KIND_TEXT,
    "log": KIND_TEXT,
    "md": KIND_MARKDOWN,
    "markdown": KIND_MARKDOWN,
    "html": KIND_HTML,
    "htm": KIND_HTML,
    "svg": KIND_HTML,
    "pdf": KIND_PDF,
    "docx": KIND_OFFICE,
    "doc": KIND_OFFICE,
    "xlsx": KIND_OFFICE,
    "xls": KIND_OFFICE,
    "pptx": KIND_OFFICE,
    "ppt": KIND_OFFICE,
    "png": KIND_IMAGE,
    "jpg": KIND_IMAGE,
    "jpeg": KIND_IMAGE,
    "gif": KIND_IMAGE,
    "webp": KIND_IMAGE,
}

#: MIME → 预览类别（MIME 优先于扩展名：扩展名可被伪造，MIME 是上传时的真实声明）。
_MIME_KINDS: dict[str, str] = {
    "text/plain": KIND_TEXT,
    "text/csv": KIND_TEXT,
    "text/markdown": KIND_MARKDOWN,
    "text/x-markdown": KIND_MARKDOWN,
    "text/html": KIND_HTML,
    "image/svg+xml": KIND_HTML,
    "application/pdf": KIND_PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": KIND_OFFICE,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": KIND_OFFICE,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": KIND_OFFICE,
    "application/msword": KIND_OFFICE,
    "application/vnd.ms-excel": KIND_OFFICE,
    "application/vnd.ms-powerpoint": KIND_OFFICE,
    "image/png": KIND_IMAGE,
    "image/jpeg": KIND_IMAGE,
    "image/gif": KIND_IMAGE,
    "image/webp": KIND_IMAGE,
}

#: 预览时可直接在浏览器渲染的 MIME（``/source`` 用 inline）。
INLINE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",
        "text/html",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
    }
)


def extensionOf(filename: str) -> str:
    """取小写扩展名（无扩展名 ⇒ 空串）。"""
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].strip().lower()


def kindFor(mimeType: str | None, filename: str) -> str:
    """MIME 优先、扩展名兜底 → 预览类别。

    都认不出来 ⇒ ``TEXT``（最保守：按纯文本渲染总不会执行脚本）。
    """
    if mimeType:
        normalized = mimeType.split(";")[0].strip().lower()
        kind = _MIME_KINDS.get(normalized)
        if kind is not None:
            return kind
    kind = _EXTENSION_KINDS.get(extensionOf(filename))
    if kind is not None:
        return kind
    return KIND_TEXT


def mimeFor(mimeType: str | None, filename: str) -> str:
    """响应 ``Content-Type``：优先真实 MIME，缺失则按扩展名推断。"""
    if mimeType:
        normalized = mimeType.split(";")[0].strip().lower()
        if normalized and normalized != "application/octet-stream":
            return normalized
    extension = extensionOf(filename)
    inferred = {
        "txt": "text/plain",
        "text": "text/plain",
        "csv": "text/csv",
        "md": "text/markdown",
        "markdown": "text/markdown",
        "html": "text/html",
        "htm": "text/html",
        "svg": "image/svg+xml",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(extension)
    return inferred or "application/octet-stream"


def isInlineViewable(mimeType: str | None, filename: str) -> bool:
    """能否浏览器直接内联打开（否则只能下载）。"""
    resolved = mimeFor(mimeType, filename)
    if resolved in INLINE_MIME_TYPES:
        return True
    return kindFor(mimeType, filename) == KIND_IMAGE
