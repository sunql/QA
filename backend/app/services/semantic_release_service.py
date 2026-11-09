"""语义发布写路径：状态机 + 发布闸门（P1b / D10）。

状态机（`SemanticReleaseStatus` 文档串是唯一依据）：

    DRAFT → VALIDATED → ACTIVE → ARCHIVED

- **DRAFT**：编译产物已落库，可反复重编；允许存在「发布闸门阻断项」，
  否则评审看不到被拦下的内容（D6 裁决：快照如实记录）。
- **VALIDATED**：Schema / 哈希 / 发布闸门全部通过，可被激活。
  闸门阻断项在这里**硬拒绝**（D07 给发布闸门的要求：`status=PUBLISHED` 的
  RATIO/DIFFERENCE 指标缺分子/分母落点 ⇒ 拒绝发布；`DRAFT` 指标同型问题仅告警）。
- **ACTIVE**：当前生效快照（同域最多一条）。激活是「顶替」语义：同一作用域
  原有的 ACTIVE 在同一事务里归档，而不是并存的第二条。
- **ARCHIVED**：只读终态。

守卫是**双保险**的一半：库侧有 `ck_semantic_release_status`（字面量）、
`uq_semantic_release_one_active`（同域唯一 ACTIVE）、`ck_semantic_release_manifest_*`。
服务层负责把非法操作变成 409 领域错误，而不是让它们撞库变成 500。

写路径的缓存纪律（D8 约定）：**事务提交之后**才调用 `semanticRegistry.invalidate()`。
在提交前失效会让并发读把旧行重新读回缓存，提交后失效才是正确顺序。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SemanticReleaseStatus
from app.domain.exceptions import ConflictError, NotFoundError, ValidationError
from app.domain.models import DataDomain
from app.domain.semantic_manifest_schema import (
    SCHEMA_VERSION,
    canonicalManifestJson,
    manifestHash,
    validateManifest,
)
from app.domain.semantic_release_models import SemanticRelease
from app.services.audit_service import AuditService
from app.services.messages_zh import (
    MSG_SEMANTIC_RELEASE_ACTIVE_CONFLICT,
    MSG_SEMANTIC_RELEASE_ACTOR_INVALID,
    MSG_SEMANTIC_RELEASE_DEPARTMENTS_TOO_LONG,
    MSG_SEMANTIC_RELEASE_DOMAIN_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_GATE_BLOCKED,
    MSG_SEMANTIC_RELEASE_GATE_NO_LANDING,
    MSG_SEMANTIC_RELEASE_GATE_NO_SOURCE,
    MSG_SEMANTIC_RELEASE_ID_INVALID,
    MSG_SEMANTIC_RELEASE_MANIFEST_TOO_LARGE,
    MSG_SEMANTIC_RELEASE_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_TRANSITION_INVALID,
    MSG_SEMANTIC_RELEASE_VERSION_CONFLICT,
    MSG_SEMANTIC_RELEASE_VERSION_INVALID,
)
from app.services.semantic_compiler import compileManifest
from app.services.semantic_registry_service import (
    MAX_MANIFEST_BYTES,
    MAX_RELEASE_ID,
    semanticRegistry,
    validateDomainCode,
    verifySnapshotIntegrity,
)

logger = logging.getLogger(__name__)

_MAX_VERSION_LENGTH = 32  # 与 semantic_release.version 列宽一致
_MAX_ACTOR_LENGTH = 64  # semantic_release.created_by 列宽（audit_log.actor 是 100，取更严的）
_MAX_DEPARTMENTS_LENGTH = 500  # audit_log.actor_departments 列宽
_MAX_GATE_MESSAGES = 20  # 闸门报告封顶：体量失控的数据不该把响应撑爆

# 合法状态迁移矩阵。ARCHIVED 是终态（回滚 = 重新激活一份历史快照的副本，
# 不是把 ARCHIVED 改回 ACTIVE —— 那会让审计里的历史状态失去意义）。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    SemanticReleaseStatus.DRAFT.value: frozenset({SemanticReleaseStatus.VALIDATED.value}),
    SemanticReleaseStatus.VALIDATED.value: frozenset({SemanticReleaseStatus.ACTIVE.value}),
    SemanticReleaseStatus.ACTIVE.value: frozenset({SemanticReleaseStatus.ARCHIVED.value}),
    SemanticReleaseStatus.ARCHIVED.value: frozenset(),
}

# 「没有落点的比率不可执行」——D07 发布闸门要求只对这两类指标硬拒绝。
_RATIO_METRIC_TYPES = frozenset({"RATIO", "DIFFERENCE"})
_AUDIT_ENTITY = "semantic_release"


@dataclass(frozen=True, slots=True)
class PublishGateResult:
    """发布闸门结论。`blockers` 非空 = 拒绝进入 VALIDATED / ACTIVE。"""

    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def canPublish(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class SemanticReleaseOperation:
    """一次写操作的结果：落库后的行 + 本次闸门报告。"""

    release: SemanticRelease
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def evaluatePublishGate(manifest: dict[str, Any]) -> PublishGateResult:
    """发布闸门（纯函数，不查库）。

    判据按指标状态分级（D07）：
    1. 指标 `sources` 为空 → **告警**（治理债要显式暴露，不静默放过）；
    2. `status=PUBLISHED` 的 RATIO/DIFFERENCE 指标，分子/分母度量没有落点类
       → **阻断**（已发布的比率不可执行，不能上线）；`DRAFT` 指标同型问题仅告警。

    当前治理库 12 条指标全部是 DRAFT，所以首个快照不会被第 2 条卡住；
    但评审预览必须列出无源 / 无落点清单。
    """
    blockers: list[str] = []
    warnings: list[str] = []

    for metric in _iterMetrics(manifest):
        name = _metricName(metric)
        if not metric.get("sources"):
            warnings.append(MSG_SEMANTIC_RELEASE_GATE_NO_SOURCE.format(metric=name))

        missingLanding = _missingLandingParts(metric)
        if not missingLanding:
            continue
        if metric.get("status") == "PUBLISHED" and metric.get("metricType") in _RATIO_METRIC_TYPES:
            for part in missingLanding:
                blockers.append(MSG_SEMANTIC_RELEASE_GATE_NO_LANDING.format(metric=name, part=part))
        else:
            for part in missingLanding:
                warnings.append(MSG_SEMANTIC_RELEASE_GATE_NO_LANDING.format(metric=name, part=part))

    return PublishGateResult(
        blockers=tuple(blockers[:_MAX_GATE_MESSAGES]),
        warnings=tuple(warnings[:_MAX_GATE_MESSAGES]),
    )


def isTransitionAllowed(fromStatus: str, toStatus: str) -> bool:
    return toStatus in _ALLOWED_TRANSITIONS.get(fromStatus, frozenset())


class SemanticReleaseService:
    """发布写路径：`createDraft` / `markValidated` / `activate` / `archive`。"""

    async def createDraft(
        self,
        session: AsyncSession,
        *,
        version: str,
        actor: str,
        domainCode: str | None = None,
        departments: tuple[str, ...] | None = None,
        note: str | None = None,
    ) -> SemanticReleaseOperation:
        """把当前治理表编译成一份 DRAFT 快照。

        DRAFT 允许带阻断项落库：发布评审要看的是「编译出来的是什么」，
        被闸门拦下的内容也必须在预览里可见。
        """
        _validateActor(actor, departments)
        normalizedVersion = _normalizeVersion(version)
        validateDomainCode(domainCode)
        if domainCode is not None:
            await _requireDomainExists(session, domainCode)
        if domainCode is None:
            # 先查再编译：重复版本不值得付一次 ~1MB 的全表编译。
            # 并发窗口由库侧 uq_semantic_release_global_version 兜底（见 except）。
            duplicate = await session.scalar(
                select(SemanticRelease.id).where(
                    SemanticRelease.version == normalizedVersion,
                    SemanticRelease.domain_code.is_(None),
                )
            )
            if duplicate is not None:
                raise ConflictError(
                    MSG_SEMANTIC_RELEASE_VERSION_CONFLICT.format(version=normalizedVersion)
                )

        manifest = await compileManifest(session)
        validateManifest(manifest)
        canonical = canonicalManifestJson(manifest)
        sizeBytes = len(canonical.encode("utf-8"))
        if sizeBytes > MAX_MANIFEST_BYTES:
            raise ValidationError(
                MSG_SEMANTIC_RELEASE_MANIFEST_TOO_LARGE.format(
                    size=sizeBytes, max=MAX_MANIFEST_BYTES
                )
            )

        gate = evaluatePublishGate(manifest)
        row = SemanticRelease(
            version=normalizedVersion,
            domain_code=domainCode,
            status=SemanticReleaseStatus.DRAFT.value,
            schema_version=manifest.get("schemaVersion") or SCHEMA_VERSION,
            manifest_json=manifest,
            manifest_hash=manifestHash(manifest),
            created_by=actor,
            note=note,
        )
        session.add(row)
        try:
            await session.flush()
            await _recordAudit(
                session,
                row,
                fromStatus=None,
                toStatus=SemanticReleaseStatus.DRAFT.value,
                actor=actor,
                departments=departments,
            )
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _mapCreateIntegrityError(exc, normalizedVersion, domainCode) from exc

        await session.refresh(row)
        logger.info(
            "语义发布草稿已落库：id=%s version=%r domain=%s bytes=%s blockers=%s",
            row.id,
            row.version,
            row.domain_code,
            sizeBytes,
            len(gate.blockers),
        )
        return SemanticReleaseOperation(release=row, blockers=gate.blockers, warnings=gate.warnings)

    async def markValidated(
        self,
        session: AsyncSession,
        releaseId: int,
        *,
        actor: str,
        departments: tuple[str, ...] | None = None,
    ) -> SemanticReleaseOperation:
        """DRAFT → VALIDATED：Schema / 哈希 / 发布闸门全部通过才放行。"""
        _validateActor(actor, departments)
        row = await _lockRelease(session, releaseId)
        _requireTransition(row, SemanticReleaseStatus.VALIDATED)

        gate = _verifyBeforePublish(row)
        _ensureGatePasses(row, gate)

        await _applyStatus(session, row, SemanticReleaseStatus.VALIDATED, actor, departments)
        await session.commit()
        return SemanticReleaseOperation(release=row, blockers=(), warnings=gate.warnings)

    async def activate(
        self,
        session: AsyncSession,
        releaseId: int,
        *,
        actor: str,
        departments: tuple[str, ...] | None = None,
    ) -> SemanticReleaseOperation:
        """VALIDATED → ACTIVE：同一作用域原有的 ACTIVE 在同一事务里归档。"""
        _validateActor(actor, departments)
        row = await _lockRelease(session, releaseId)
        _requireTransition(row, SemanticReleaseStatus.ACTIVE)

        gate = _verifyBeforePublish(row)
        _ensureGatePasses(row, gate)

        previousActives = await _lockActivesInScope(session, row)
        for previous in previousActives:
            await _applyStatus(session, previous, SemanticReleaseStatus.ARCHIVED, actor, departments)

        # 必须先 flush 归档，再写目标 ACTIVE：SQLAlchemy 的 unit of work 按主键排序
        # 发送 UPDATE，若目标 id 更小（回滚到历史版本），两条 UPDATE 会「目标先 ACTIVE、
        # 旧版后 ARCHIVED」，瞬时两条 ACTIVE 会撞 uq_semantic_release_one_active，
        # 让合法的回滚确定性失败（并误报成并发冲突）。
        await session.flush()
        await _applyStatus(session, row, SemanticReleaseStatus.ACTIVE, actor, departments)
        row.activated_time = datetime.now(UTC)

        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发激活：库侧 `uq_semantic_release_one_active` 是最后一道闸。
            await session.rollback()
            raise ConflictError(
                MSG_SEMANTIC_RELEASE_ACTIVE_CONFLICT.format(releaseId=releaseId)
            ) from exc

        # 提交成功之后才失效：提交前失效会被并发读把旧行重新回填（D8 代数守卫的反面）。
        semanticRegistry.invalidate()
        logger.info(
            "语义发布已激活：id=%s version=%r domain=%s 归档旧版=%s",
            row.id,
            row.version,
            row.domain_code,
            [previous.id for previous in previousActives],
        )
        return SemanticReleaseOperation(release=row, blockers=(), warnings=gate.warnings)

    async def archive(
        self,
        session: AsyncSession,
        releaseId: int,
        *,
        actor: str,
        departments: tuple[str, ...] | None = None,
    ) -> SemanticReleaseOperation:
        """ACTIVE → ARCHIVED：人工下线（回滚见模块文档串）。"""
        _validateActor(actor, departments)
        row = await _lockRelease(session, releaseId)
        _requireTransition(row, SemanticReleaseStatus.ARCHIVED)

        await _applyStatus(session, row, SemanticReleaseStatus.ARCHIVED, actor, departments)
        await session.commit()

        semanticRegistry.invalidate()
        logger.info("语义发布已归档：id=%s version=%r domain=%s", row.id, row.version, row.domain_code)
        return SemanticReleaseOperation(release=row)


semanticReleaseService = SemanticReleaseService()


# ---------------------------------------------------------------------------
# 内部：输入 / 状态迁移 / 审计
# ---------------------------------------------------------------------------


def _normalizeVersion(version: str) -> str:
    normalized = version.strip() if isinstance(version, str) else ""
    if not normalized or len(normalized) > _MAX_VERSION_LENGTH:
        raise ValidationError(
            MSG_SEMANTIC_RELEASE_VERSION_INVALID.format(
                version=version, max=_MAX_VERSION_LENGTH
            )
        )
    return normalized


async def _requireDomainExists(session: AsyncSession, domainCode: str) -> None:
    """FK 也有兜底，但先给一条可读的 422，而不是把外键错误翻译成 500。"""
    exists = await session.scalar(select(DataDomain.code).where(DataDomain.code == domainCode))
    if exists is None:
        raise ValidationError(MSG_SEMANTIC_RELEASE_DOMAIN_NOT_FOUND.format(code=domainCode))


def _validateActor(actor: str, departments: tuple[str, ...] | None) -> None:
    """身份字段必须落在库列宽内：stub 头 / 异常 token 可能给出任意长度。

    不截断 —— 截断会静默篡改审计里的「谁做的」，宁可 422 拒绝一次写。
    """
    if not isinstance(actor, str) or not actor.strip() or len(actor) > _MAX_ACTOR_LENGTH:
        raise ValidationError(MSG_SEMANTIC_RELEASE_ACTOR_INVALID.format(max=_MAX_ACTOR_LENGTH))
    if departments:
        joined = ",".join(str(department) for department in departments)
        if len(joined) > _MAX_DEPARTMENTS_LENGTH:
            raise ValidationError(
                MSG_SEMANTIC_RELEASE_DEPARTMENTS_TOO_LONG.format(max=_MAX_DEPARTMENTS_LENGTH)
            )


def _validateReleaseId(releaseId: int) -> None:
    if releaseId < 1 or releaseId > MAX_RELEASE_ID:
        raise ValidationError(
            MSG_SEMANTIC_RELEASE_ID_INVALID.format(releaseId=releaseId, max=MAX_RELEASE_ID)
        )


async def _lockRelease(session: AsyncSession, releaseId: int) -> SemanticRelease:
    """`SELECT ... FOR UPDATE`：状态迁移必须串行，避免两个请求同时看到 DRAFT。"""
    _validateReleaseId(releaseId)
    statement = (
        select(SemanticRelease).where(SemanticRelease.id == releaseId).with_for_update()
    )
    row = (await session.execute(statement)).scalars().first()
    if row is None:
        raise NotFoundError(MSG_SEMANTIC_RELEASE_NOT_FOUND.format(releaseId=releaseId))
    return row


async def _lockActivesInScope(
    session: AsyncSession, target: SemanticRelease
) -> list[SemanticRelease]:
    """锁定目标作用域内**其他** ACTIVE 行（全局与按域互不影响）。"""
    domainFilter = (
        SemanticRelease.domain_code.is_(None)
        if target.domain_code is None
        else SemanticRelease.domain_code == target.domain_code
    )
    statement = (
        select(SemanticRelease)
        .where(
            SemanticRelease.status == SemanticReleaseStatus.ACTIVE.value,
            SemanticRelease.id != target.id,
            domainFilter,
        )
        .with_for_update()
    )
    return list((await session.execute(statement)).scalars().all())


def _requireTransition(row: SemanticRelease, target: SemanticReleaseStatus) -> None:
    if not isTransitionAllowed(row.status, target.value):
        raise ConflictError(
            MSG_SEMANTIC_RELEASE_TRANSITION_INVALID.format(
                releaseId=row.id, current=row.status, target=target.value
            )
        )


def _verifyBeforePublish(row: SemanticRelease) -> PublishGateResult:
    """进入 VALIDATED / ACTIVE 前的统一校验：完整性 → Schema → 闸门。"""
    snapshot = verifySnapshotIntegrity(row)
    manifest = snapshot.manifest
    validateManifest(manifest)
    return evaluatePublishGate(manifest)


def _ensureGatePasses(row: SemanticRelease, gate: PublishGateResult) -> None:
    if gate.canPublish:
        return
    raise ConflictError(
        MSG_SEMANTIC_RELEASE_GATE_BLOCKED.format(
            releaseId=row.id,
            count=len(gate.blockers),
            detail="；".join(gate.blockers),
        )
    )


async def _applyStatus(
    session: AsyncSession,
    row: SemanticRelease,
    target: SemanticReleaseStatus,
    actor: str,
    departments: tuple[str, ...] | None,
) -> None:
    """改状态 + 写审计（同一事务）。审计失败则整体回滚 —— 治理动作必须留痕。"""
    fromStatus = row.status
    row.status = target.value
    session.add(row)
    await _recordAudit(
        session,
        row,
        fromStatus=fromStatus,
        toStatus=target.value,
        actor=actor,
        departments=departments,
    )


async def _recordAudit(
    session: AsyncSession,
    row: SemanticRelease,
    *,
    fromStatus: str | None,
    toStatus: str,
    actor: str,
    departments: tuple[str, ...] | None,
) -> None:
    before = None if fromStatus is None else _auditPayload(row, fromStatus)
    await AuditService().record(
        session,
        entity_type=_AUDIT_ENTITY,
        entity_id=row.id,
        action="CREATE" if fromStatus is None else "UPDATE",
        actor=actor,
        actor_departments=departments,
        before=before,
        after=_auditPayload(row, toStatus),
    )


def _auditPayload(row: SemanticRelease, status: str) -> dict[str, Any]:
    return {
        "status": status,
        "version": row.version,
        "domainCode": row.domain_code,
    }


def _iterMetrics(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = manifest.get("metrics") if isinstance(manifest, dict) else None
    if not isinstance(metrics, list):
        return []
    return [metric for metric in metrics if isinstance(metric, dict)]


def _metricName(metric: dict[str, Any]) -> str:
    name = metric.get("metricName")
    return name if isinstance(name, str) and name else "<未命名指标>"


def _missingLandingParts(metric: dict[str, Any]) -> list[str]:
    """返回缺失落点的度量位置（中文，用于报告）；没有缺失则为空。

    分母只对 RATIO/DIFFERENCE 有意义：MEASURE 指标不存在「分母缺落点」这种说法，
    对它报分母会让报告被噪音淹没（评审会开始忽略整份报告）。
    """
    fields = [("numeratorMeasure", "分子")]
    if metric.get("metricType") in _RATIO_METRIC_TYPES:
        fields.append(("denominatorMeasure", "分母"))

    missing: list[str] = []
    for field, label in fields:
        measure = metric.get(field)
        className = measure.get("className") if isinstance(measure, dict) else None
        if not className:
            missing.append(label)
    return missing


def _mapCreateIntegrityError(
    exc: IntegrityError, version: str, domainCode: str | None
) -> Exception:
    """唯一索引 / 外键撞库的翻译：领域错误（409/422），而不是 500。"""
    sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
    if sqlstate == "23503" and domainCode is not None:
        return ValidationError(MSG_SEMANTIC_RELEASE_DOMAIN_NOT_FOUND.format(code=domainCode))
    return ConflictError(MSG_SEMANTIC_RELEASE_VERSION_CONFLICT.format(version=version))
