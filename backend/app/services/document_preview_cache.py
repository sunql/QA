"""Office → PDF 预览的缓存层（按内容寻址，跨请求复用）。

缓存键 = ``previews/<转换器版本>/<content_hash>.pdf``：

- **按内容寻址**：同一份文件（哪怕改了名/重复上传）只转一次；
- **带转换器版本**：升级 LibreOffice 后旧缓存自然失效（不用手工清）；
- **不缓存失败**：转换失败不写缓存 ⇒ 下次请求会重试（否则一次瞬时空闲
  会把「预览不可用」永久钉死）。

缓存写在对象存储而不是本地磁盘：容器是无状态的（重启即丢），多副本部署时
本地缓存命中率也低。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 缓存对象名前缀（与源文件的 ``sources/`` 分开，生命周期不同）。
CACHE_PREFIX = "previews"


def cacheObjectName(contentHash: str, converterVersion: str) -> str:
    """``previews/<版本>/<hash 前2位>/<hash>.pdf``（分片目录避免单目录海量对象）。"""
    return f"{CACHE_PREFIX}/{converterVersion}/{contentHash[:2]}/{contentHash}.pdf"


def readCachedPreview(contentHash: str, converterVersion: str) -> bytes | None:
    """读缓存 PDF；未命中/读失败 ⇒ None（读失败不阻断转换）。"""
    from app.infrastructure.object_storage import (
        ObjectStorageError,
        getSourceObject,
    )

    try:
        return getSourceObject(cacheObjectName(contentHash, converterVersion))
    except ObjectStorageError:
        # 未命中是常态（_not found_）；真故障也不该挡住「重新转换」这条主路
        return None
    except Exception:  # noqa: BLE001
        logger.warning("预览缓存读取异常（忽略，继续转换）: %s", contentHash[:12], exc_info=True)
        return None


def writeCachedPreview(contentHash: str, converterVersion: str, pdf: bytes) -> None:
    """写缓存 PDF；失败只记日志（缓存是优化，不是正确性依赖）。"""
    from app.infrastructure.object_storage import ObjectStorageError, putSourceObject

    try:
        putSourceObject(
            cacheObjectName(contentHash, converterVersion), pdf, "application/pdf"
        )
    except ObjectStorageError:
        logger.warning("预览缓存写入失败（不影响本次返回）: %s", contentHash[:12])
    except Exception:  # noqa: BLE001
        logger.warning("预览缓存写入异常（忽略）: %s", contentHash[:12], exc_info=True)
