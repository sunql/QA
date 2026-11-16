"""P4 一次查询的**发布面**（一次查询只定一个发布）。

为什么单独一个模块
------------------
「这一轮读哪一版发布」是**编排层**的决定，不属于任何一路：
四路必须读**同一版**发布，否则「引用可回溯」会变成「四路各自回溯到不同版本」。

降级语义（本模块与 `semantic_schema_adapter.resolveSchemaRelease` **刻意不同**）
--------------------------------------------------------------------------
- schema 文本路径：发布不可用 ⇒ **降级直读治理表**（那里本来就在读治理表）。
- 知识服务路径：发布不可用 ⇒ **空结果 + 显式降级标记**。
  原因：治理表是**原地 UPDATE** 的活数据，直读它拿到的内容**没有版本可引**
  ⇒ 引用就无法回溯 ⇒ 宁可给「空 + 降级标记」，也不给「看着像发布、其实不是」的东西。

⚠️ 因此本模块**不复用** `resolveSchemaRelease`：那边的降级后果是「换成活数据」，
这边是「没有数据」⇒ 失败分类与提示语都不同，各写一份比硬凑一个通用函数更清楚。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.domain.enums import SemanticReleaseStatus
from app.domain.exceptions import NotFoundError, ValidationError
from app.services.knowledge.messages import (
    MSG_PINNED_RELEASE_UNAVAILABLE,
    MSG_RELEASE_UNAVAILABLE,
)
from app.services.semantic_registry_service import (
    SemanticReleaseIntegrityError,
    semanticRegistry,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedRelease:
    """一次查询钉住的发布面。

    Attributes:
        manifest: 发布快照正文（`None` ⇒ 本轮没有发布面可用）。
        release_id: 发布主键（降级时为 `None`）。
        version: 发布版本号（降级时为 `None`）。
        manifest_hash: 快照哈希（降级时为 `None`）。
        degraded: 是否处于降级（没读到发布面）。
        reason: 降级原因（可读的中文说明）。
    """

    manifest: dict[str, Any] | None
    release_id: int | None
    version: str | None
    manifest_hash: str | None
    degraded: bool
    reason: str | None = None


async def resolveKnowledgeRelease(
    session: Any,
    *,
    domainCode: str,
    releaseId: int | None = None,
    isAdmin: bool = False,
) -> ResolvedRelease:
    """定出这一轮该用哪一版发布。

    - 钉住（给了 `releaseId`）⇒ 按主键取**那一版**，**不去问 ACTIVE**
      （钉住的意义就是「重放还是上次那一版」）；但**必须过授权**：
      域要对得上（全局发布 `domain_code IS NULL` 对任何域成立）、
      状态必须是 **ACTIVE**（除非调用方是 admin）。
    - 没钉住 ⇒ 取该域当前 ACTIVE（没有 ⇒ 降级）。

    ⚠️ **钉住不是越权开关**：没授权就降级，且**文案统一**
    （`MSG_PINNED_RELEASE_UNAVAILABLE`）⇒ 不给「这个 id 存不存在」的探针。

    任何失败（不存在 / id 非法 / 无权 / 完整性校验不过 / 没有 ACTIVE）都**不抛异常**：
    知识服务不该因为发布面问题整体失败 ⇒ 降级 + 原因。
    """
    if releaseId is not None:
        try:
            snapshot = await semanticRegistry.getById(session, releaseId)
        except (NotFoundError, ValidationError) as exc:
            logger.warning(
                "钉住的语义发布不可用（releaseId=%s）：%s，本次降级", releaseId, exc
            )
            return _degraded(MSG_PINNED_RELEASE_UNAVAILABLE)
        except SemanticReleaseIntegrityError as exc:
            logger.error(
                "钉住的语义发布完整性校验失败，本次降级：releaseId=%s %s", releaseId, exc
            )
            return _degraded(MSG_PINNED_RELEASE_UNAVAILABLE)
        denial = _pinnedReleaseDenial(
            snapshot, domainCode=domainCode, isAdmin=isAdmin
        )
        if denial is not None:
            logger.warning(
                "钉住的语义发布不可用（releaseId=%s）：%s，本次降级", releaseId, denial
            )
            return _degraded(MSG_PINNED_RELEASE_UNAVAILABLE)
        return _fromSnapshot(snapshot)

    try:
        snapshot = await semanticRegistry.getActive(session, domainCode=domainCode)
    except SemanticReleaseIntegrityError as exc:
        logger.error("语义发布完整性校验失败，本次降级：domain=%s %s", domainCode, exc)
        return _degraded("语义发布完整性校验失败")

    if snapshot is None:
        logger.info("当前没有 ACTIVE 语义发布（domain=%s），本次降级", domainCode)
        return _degraded(f"该域当前没有已激活的语义发布（域={domainCode}）")
    return _fromSnapshot(snapshot)


def _pinnedReleaseDenial(
    snapshot: Any, *, domainCode: str, isAdmin: bool
) -> str | None:
    """钉住发布的授权判定；**返回拒绝原因**（`None` ⇒ 放行）。

    两条规则：
    1. **域一致**：`domain_code IS NULL`（全局发布）对任何域成立；
       否则必须与请求的域相同（admin 也不例外 —— 这是正确性，不是权限）。
    2. **状态可见**：非 admin 只可见 `ACTIVE`（发布治理闸在读侧的对齐）；
       admin 可读任意状态（DRAFT / VALIDATED / ARCHIVED 都可自查、预演）。
    """
    releaseDomain = getattr(snapshot, "domain_code", None)
    if releaseDomain is not None and releaseDomain != domainCode:
        return f"发布域不匹配（发布域={releaseDomain}，请求域={domainCode}）"
    status = getattr(snapshot, "status", None)
    if not isAdmin and status != SemanticReleaseStatus.ACTIVE.value:
        return f"发布状态不可见（status={status}）"
    return None


def _fromSnapshot(snapshot: Any) -> ResolvedRelease:
    return ResolvedRelease(
        manifest=snapshot.manifest,
        release_id=snapshot.id,
        version=snapshot.version,
        manifest_hash=snapshot.manifest_hash,
        degraded=False,
        reason=None,
    )


def _degraded(reason: str) -> ResolvedRelease:
    return ResolvedRelease(
        manifest=None,
        release_id=None,
        version=None,
        manifest_hash=None,
        degraded=True,
        reason=reason,
    )


__all__ = [
    "MSG_PINNED_RELEASE_UNAVAILABLE",
    "MSG_RELEASE_UNAVAILABLE",
    "ResolvedRelease",
    "resolveKnowledgeRelease",
]
