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
    ManifestSchemaError,
    canonicalManifestJson,
    manifestHash,
    validateManifest,
)
from app.domain.semantic_release_models import SemanticRelease
from app.services.audit_service import AuditService
from app.services.messages_zh import (
    MSG_SEMANTIC_RELEASE_ACTIVE_CONFLICT,
    MSG_SEMANTIC_RELEASE_ACTOR_INVALID,
    MSG_SEMANTIC_RELEASE_CHECK_CONSTRAINT,
    MSG_SEMANTIC_RELEASE_DEPARTMENTS_TOO_LONG,
    MSG_SEMANTIC_RELEASE_DOMAIN_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_GATE_BLOCKED,
    MSG_SEMANTIC_RELEASE_ID_INVALID,
    MSG_SEMANTIC_RELEASE_MANIFEST_TOO_LARGE,
    MSG_SEMANTIC_RELEASE_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_TRANSITION_INVALID,
    MSG_SEMANTIC_RELEASE_VERSION_CONFLICT,
    MSG_SEMANTIC_RELEASE_VERSION_INVALID,
)
from app.services.semantic_compiler import compileManifest
from app.services.semantic_compiler_bindings import SourceBindingCompileError
from app.services.semantic_registry_service import (
    MAX_MANIFEST_BYTES,
    MAX_RELEASE_ID,
    SemanticReleaseIntegrityError,
    semanticRegistry,
    validateDomainCode,
    verifySnapshotIntegrity,
)
from app.services.semantic_release_check_service import semanticReleaseCheckService
from app.services.semantic_release_checks import (
    CHECK_MANIFEST_SCHEMA,
    CHECK_SNAPSHOT_INTEGRITY,
    ITEM_SNAPSHOT_INTEGRITY,
    RESULT_FAIL,
    RESULT_PASS,
    SEVERITY_ERROR,
    TARGET_RELEASE,
    PublishCheck,
    PublishGateResult,
    capChecks,
    checksToGateResult,
    evaluateManifestChecks,
    evaluatePublishChecks,
)

logger = logging.getLogger(__name__)

_MAX_VERSION_LENGTH = 32  # 与 semantic_release.version 列宽一致
_MAX_ACTOR_LENGTH = 64  # semantic_release.created_by 列宽（audit_log.actor 是 100，取更严的）
_MAX_DEPARTMENTS_LENGTH = 500  # audit_log.actor_departments 列宽

# 合法状态迁移矩阵。ARCHIVED 是终态（回滚 = 重新激活一份历史快照的副本，
# 不是把 ARCHIVED 改回 ACTIVE —— 那会让审计里的历史状态失去意义）。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    SemanticReleaseStatus.DRAFT.value: frozenset({SemanticReleaseStatus.VALIDATED.value}),
    SemanticReleaseStatus.VALIDATED.value: frozenset({SemanticReleaseStatus.ACTIVE.value}),
    SemanticReleaseStatus.ACTIVE.value: frozenset({SemanticReleaseStatus.ARCHIVED.value}),
    SemanticReleaseStatus.ARCHIVED.value: frozenset(),
}

_AUDIT_ENTITY = "semantic_release"


@dataclass(frozen=True, slots=True)
class SemanticReleaseOperation:
    """一次写操作的结果：落库后的行 + 本次闸门报告。"""

    release: SemanticRelease
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseEvaluation:
    """一次发布校验的结论：逐项校验项 + 闸门报告 + 失败原因。

    `failure` 非空 ⇒ 本次状态迁移必须被拒绝，且**异常类型沿用既有契约**
    （完整性 / 契约错 = 原异常上抛；闸门阻断 = `ConflictError` 409）。
    """

    checks: tuple[PublishCheck, ...] = ()
    gate: PublishGateResult = PublishGateResult()
    failure: Exception | None = None


def evaluatePublishGate(manifest: dict[str, Any]) -> PublishGateResult:
    """发布闸门（纯函数，不查库）。

    判据按指标状态分级（D07），逐项语义见 `semantic_release_checks`：
    1. 指标 `sources` 为空 → **告警**（治理债要显式暴露，不静默放过）；
    2. `status=PUBLISHED` 的 RATIO/DIFFERENCE 指标，分子/分母度量没有落点类
       → **阻断**（已发布的比率不可执行，不能上线）；`DRAFT` 指标同型问题仅告警。

    当前治理库 12 条指标全部是 DRAFT，所以首个快照不会被第 2 条卡住；
    但评审预览必须列出无源 / 无落点清单。

    ⚠️ 逐项化（为落表）之后口径**一字未变**：本函数 = 逐项结论的失败行聚合，
    单测逐条比对两者相等。
    """
    return checksToGateResult(evaluatePublishChecks(manifest))


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

        manifest = await _compileOrReject(session)
        validateManifest(manifest)
        canonical = canonicalManifestJson(manifest)
        sizeBytes = len(canonical.encode("utf-8"))
        if sizeBytes > MAX_MANIFEST_BYTES:
            raise ValidationError(
                MSG_SEMANTIC_RELEASE_MANIFEST_TOO_LARGE.format(
                    size=sizeBytes, max=MAX_MANIFEST_BYTES
                )
            )

        # ⚠️ 单一事实源：**闸门结论与落表行来自同一份校验项集合**
        # （此前是「闸门算一遍、落表再算一遍」⇒ 两边可能静默错位）。
        draftChecks = _draftChecks(manifest)
        gate = checksToGateResult(draftChecks)
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
            # 草稿也要落校验结论：评审要看到「编译出来的东西被查过什么」。
            # （编译产物刚刚规范化 + 算过哈希 ⇒ 完整性天然通过）
            await semanticReleaseCheckService.replaceChecks(
                session,
                row.id,
                draftChecks,
                actor=actor,
            )
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
        """DRAFT → VALIDATED：Schema / 哈希 / 发布闸门全部通过才放行。

        ⚠️ **失败路径会提交本 session**（证据先行）：校验结论先落库并提交，
        再把原异常上抛（409 / 500，契约不变）。⇒ 调用方**不得**把本方法
        包进更大的事务里，否则「抛异常 ⇒ 外层回滚」会把刚落下的证据一起回滚。
        """
        _validateActor(actor, departments)
        row = await _lockRelease(session, releaseId)
        _requireTransition(row, SemanticReleaseStatus.VALIDATED)

        evaluation = _evaluateReleaseChecks(row)
        await semanticReleaseCheckService.replaceChecks(
            session, row.id, evaluation.checks, actor=actor
        )
        if evaluation.failure is not None:
            # 证据先落库并提交，再抛回原异常 —— 失败结论必须可查（M1-a DoD 第 3 条）。
            await _recordBlockedAudit(
                session,
                row,
                phase="validate",
                evaluation=evaluation,
                actor=actor,
                departments=departments,
            )
            await session.commit()
            raise evaluation.failure

        await _applyStatus(session, row, SemanticReleaseStatus.VALIDATED, actor, departments)
        await session.commit()
        return SemanticReleaseOperation(
            release=row, blockers=(), warnings=evaluation.gate.warnings
        )

    async def activate(
        self,
        session: AsyncSession,
        releaseId: int,
        *,
        actor: str,
        departments: tuple[str, ...] | None = None,
    ) -> SemanticReleaseOperation:
        """VALIDATED → ACTIVE：同一作用域原有的 ACTIVE 在同一事务里归档。

        ⚠️ **失败路径会提交本 session**（证据先行），与 `markValidated` 同：
        校验结论先落库并提交，再抛原异常；此时**同作用域的旧 ACTIVE 不受影响**
        （归档只发生在校验通过之后）。调用方不得把本方法包进更大的事务。
        """
        _validateActor(actor, departments)
        row = await _lockRelease(session, releaseId)
        _requireTransition(row, SemanticReleaseStatus.ACTIVE)

        evaluation = _evaluateReleaseChecks(row)
        await semanticReleaseCheckService.replaceChecks(
            session, row.id, evaluation.checks, actor=actor
        )
        if evaluation.failure is not None:
            # 同 markValidated：证据先落库再抛错，且**不碰**同作用域的旧 ACTIVE
            # （本事务在抛错前只提交了校验结论，旧版仍是 ACTIVE）。
            await _recordBlockedAudit(
                session,
                row,
                phase="activate",
                evaluation=evaluation,
                actor=actor,
                departments=departments,
            )
            await session.commit()
            raise evaluation.failure

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
        return SemanticReleaseOperation(
            release=row, blockers=(), warnings=evaluation.gate.warnings
        )

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


async def _compileOrReject(session: AsyncSession) -> dict[str, Any]:
    """编译治理表；源绑定数据不满足发布不变量 ⇒ 422 而不是 500。

    为什么单独一层：编译器对「数据被写坏」抛 `ValueError` 系异常
    （`SourceBindingCompileError`），直接冒到 API 层会变成 500 ——
    而这是**治理数据问题**，调用方能修（去改绑定），必须给一条说得清
    「哪条绑定、为什么」的 422。
    """
    try:
        return await compileManifest(session)
    except SourceBindingCompileError as exc:
        raise ValidationError(f"源绑定数据不满足发布约束：{exc}") from exc


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


def _integrityCheck(passed: bool, detail: str) -> PublishCheck:
    """快照完整性校验项（发布级）。

    完整性是「行」的属性（库里那一行的 `manifest_hash`），不属于 manifest 自身，
    所以它不在 `evaluateManifestChecks` 里，由本模块按上下文追加。
    """
    return PublishCheck(
        code=CHECK_SNAPSHOT_INTEGRITY,
        item=ITEM_SNAPSHOT_INTEGRITY,
        severity=SEVERITY_ERROR,
        result=RESULT_PASS if passed else RESULT_FAIL,
        targetType=TARGET_RELEASE,
        detail=detail,
    )


def _draftChecks(manifest: dict[str, Any]) -> tuple[PublishCheck, ...]:
    """草稿编译时的校验项集合。

    编译产物刚刚规范化并算过哈希 ⇒ 完整性**必然通过**（这里仍然显式落一行，
    让「草稿阶段查了什么」与「validate 阶段查了什么」是同一套目录）。
    """
    return capChecks(
        (
            _integrityCheck(True, "编译产物已规范化并计算哈希（manifest_hash 与正文一致）"),
            *evaluateManifestChecks(manifest),
        )
    )


def _evaluateReleaseChecks(row: SemanticRelease) -> ReleaseEvaluation:
    """进入 VALIDATED / ACTIVE 前的统一校验：完整性 → Schema → 闸门。

    **不抛异常**：把「失败原因」放进返回值，让调用方先把校验结论**落库并提交**，
    再把原异常抛回（证据先行，契约不变）。
    """
    try:
        snapshot = verifySnapshotIntegrity(row)
    except SemanticReleaseIntegrityError as exc:
        return ReleaseEvaluation(
            checks=(_integrityCheck(False, str(exc)),),
            gate=PublishGateResult(),
            failure=exc,
        )

    manifest = snapshot.manifest
    checks = capChecks(
        (
            _integrityCheck(True, "快照规范化、体量与哈希校验通过"),
            *evaluateManifestChecks(manifest),
        )
    )
    gate = checksToGateResult(checks)

    schemaRow = next(
        (check for check in checks if check.code == CHECK_MANIFEST_SCHEMA),
        None,
    )
    if schemaRow is not None and schemaRow.result == RESULT_FAIL:
        # 契约不满足：沿用既有异常类型（ValueError 系），不改成 409/422 ——
        # 「快照坏了」与「契约坏了」都不是资源冲突。
        return ReleaseEvaluation(
            checks=checks,
            gate=gate,
            failure=ManifestSchemaError(schemaRow.detail),
        )

    if not gate.canPublish:
        return ReleaseEvaluation(
            checks=checks,
            gate=gate,
            failure=ConflictError(
                MSG_SEMANTIC_RELEASE_GATE_BLOCKED.format(
                    releaseId=row.id,
                    count=len(gate.blockers),
                    detail="；".join(gate.blockers),
                )
            ),
        )

    return ReleaseEvaluation(checks=checks, gate=gate)


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


async def _recordBlockedAudit(
    session: AsyncSession,
    row: SemanticRelease,
    *,
    phase: str,
    evaluation: ReleaseEvaluation,
    actor: str,
    departments: tuple[str, ...] | None,
) -> None:
    """**失败尝试也留痕**（与证据同一事务提交）。

    为什么必须：check 表是**覆盖写**的（每次评估代表最新一轮判断），
    而唯一会写 `audit_log` 的地方是**成功**的状态迁移（`_applyStatus`）。
    ⇒ 被闸门拦下的那次尝试，在 check 表与 audit_log **两边都没有痕迹**：
    下一轮重评估把上一轮的 FAIL 行删掉之后，「那次被哪一条拦下的」就再也查不到了 ——
    而这正是这张表被建起来要回答的问题。

    动作词用既有的 `UPDATE`（审计动作是**封闭词表**，服务层与 DB CHECK 双重限制，
    扩词要动 ORM CHECK + 迁移 ⇒ 代价与收益不成比例）；阻断事实写在 `after` 里
    （`blocked=true` + 阻断项摘要 + 校验项条数）。
    """
    await AuditService().record(
        session,
        entity_type=_AUDIT_ENTITY,
        entity_id=row.id,
        action="UPDATE",
        actor=actor,
        actor_departments=departments,
        before=_auditPayload(row, row.status),
        after={
            **_auditPayload(row, row.status),
            "blocked": True,
            "phase": phase,
            "blockerCount": len(evaluation.gate.blockers),
            "blockers": list(evaluation.gate.blockers),
            "checkCount": len(evaluation.checks),
            "errorCount": sum(
                1
                for check in evaluation.checks
                if check.result == RESULT_FAIL and check.severity == SEVERITY_ERROR
            ),
        },
    )


def _mapCreateIntegrityError(
    exc: IntegrityError, version: str, domainCode: str | None
) -> Exception:
    """唯一索引 / 外键撞库的翻译：领域错误（409/422），而不是 500。

    ⚠️ 必须**按约束归因**，不能一律报「版本冲突」：草稿落库会同时写
    `semantic_release` 与 `semantic_release_check` 两张表 ⇒ 撞的可能是
    check 表的约束（唯一索引 / CHECK）。误报成「版本已存在」会让调用方
    以为要改版本号，而真实原因是校验结论表写不进去。
    """
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)

    if sqlstate == "23503" and domainCode is not None:
        return ValidationError(MSG_SEMANTIC_RELEASE_DOMAIN_NOT_FOUND.format(code=domainCode))
    if constraint and constraint.startswith(("ck_semantic_release_check", "uq_semantic_release_check")):
        # 校验结论表的约束冲突：不是版本号的问题，别误导调用方。
        return ConflictError(
            MSG_SEMANTIC_RELEASE_CHECK_CONSTRAINT.format(constraint=constraint)
        )
    return ConflictError(MSG_SEMANTIC_RELEASE_VERSION_CONFLICT.format(version=version))
