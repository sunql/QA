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

⚠️ 本模块只做**编排**（编译 → 切片 → 落库 → 状态迁移 → 审计）；
输入校验 / 校验项评估 / 审计留痕那一层在 `semantic_release_support`
（分层理由与依赖方向见该模块文档串）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import SemanticReleaseStatus
from app.domain.exceptions import ConflictError, ValidationError
from app.domain.semantic_manifest_schema import (
    SCHEMA_VERSION,
    canonicalManifestJson,
    manifestHash,
)
from app.domain.semantic_release_models import SemanticRelease
from app.services.messages_zh import (
    MSG_SEMANTIC_RELEASE_ACTIVE_CONFLICT,
    MSG_SEMANTIC_RELEASE_MANIFEST_TOO_LARGE,
    MSG_SEMANTIC_RELEASE_TRANSITION_INVALID,
)
from app.services.semantic_registry_service import (
    MAX_MANIFEST_BYTES,
    semanticRegistry,
    validateDomainCode,
)
from app.services.semantic_release_check_service import semanticReleaseCheckService
from app.services.semantic_release_checks import (
    PublishGateResult,
    checksToGateResult,
    evaluatePublishChecks,
)
from app.services.semantic_release_support import (
    _activateCopy,
    _applyStatus,
    _buildRollbackCopy,
    _checkContext,
    _compileOrReject,
    _draftChecks,
    _evaluateReleaseChecks,
    _lockActivesInScope,
    _lockRelease,
    _manifestClassNames,
    _mapCreateIntegrityError,
    _normalizeVersion,
    _recordAudit,
    _recordBlockedAudit,
    _requireDomainExists,
    _requireRollbackSource,
    _requireVersionFree,
    _sliceIfDomain,
    _validateActor,
    _validateManifestOrReject,
    _validateNote,
    blockedVersion,
)

logger = logging.getLogger(__name__)

# 合法状态迁移矩阵。ARCHIVED 是终态（回滚 = 重新激活一份历史快照的副本，
# 不是把 ARCHIVED 改回 ACTIVE —— 那会让审计里的历史状态失去意义）。
_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    SemanticReleaseStatus.DRAFT.value: frozenset({SemanticReleaseStatus.VALIDATED.value}),
    SemanticReleaseStatus.VALIDATED.value: frozenset({SemanticReleaseStatus.ACTIVE.value}),
    SemanticReleaseStatus.ACTIVE.value: frozenset({SemanticReleaseStatus.ARCHIVED.value}),
    SemanticReleaseStatus.ARCHIVED.value: frozenset(),
}


@dataclass(frozen=True, slots=True)
class SemanticReleaseOperation:
    """一次写操作的结果：落库后的行 + 本次闸门报告。"""

    release: SemanticRelease
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


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


def _requireTransition(row: SemanticRelease, target: SemanticReleaseStatus) -> None:
    if not isTransitionAllowed(row.status, target.value):
        raise ConflictError(
            MSG_SEMANTIC_RELEASE_TRANSITION_INVALID.format(
                releaseId=row.id, current=row.status, target=target.value
            )
        )


class SemanticReleaseService:
    """发布写路径：`createDraft` / `markValidated` / `activate` / `archive` / `rollback`。"""

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

        **域发布只带该域的东西**（D-10）：`domainCode` 非空 ⇒ 编译全量后
        按域切片（引用闭合补齐），存进快照的是**切片**，不是全量。
        """
        _validateActor(actor, departments)
        _validateNote(note)
        normalizedVersion = _normalizeVersion(version)
        validateDomainCode(domainCode)
        if domainCode is not None:
            await _requireDomainExists(session, domainCode)
        # 先查再编译：重复版本不值得付一次 ~1MB 的全表编译。
        # 并发窗口由库侧唯一索引兜底（见 except）。
        await _requireVersionFree(session, normalizedVersion, domainCode)

        compiled = await _compileOrReject(session)
        manifest = _sliceIfDomain(compiled, domainCode)
        _validateManifestOrReject(manifest)
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
        # 候选统计按**快照里的类**收窄 ⇒ 报告与快照同一宇宙。
        draftChecks = _draftChecks(
            manifest,
            await _checkContext(session, domainCode, _manifestClassNames(manifest)),
        )
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

        ⚠️ 候选统计按**已存快照里的类**收窄（与草稿阶段同一口径）⇒
        同一份快照在草稿 / validate / activate 三个阶段算出的候选数**一致**
        （否则「报告可复算」破功：草稿说 0 个候选、validate 说 3 个候选）。

        ⚠️ **失败路径会提交本 session**（证据先行）：校验结论先落库并提交，
        再把原异常上抛（409 / 500，契约不变）。⇒ 调用方**不得**把本方法
        包进更大的事务里，否则「抛异常 ⇒ 外层回滚」会把刚落下的证据一起回滚。
        """
        _validateActor(actor, departments)
        row = await _lockRelease(session, releaseId)
        _requireTransition(row, SemanticReleaseStatus.VALIDATED)

        evaluation = _evaluateReleaseChecks(
            row,
            await _checkContext(
                session, row.domain_code, _manifestClassNames(row.manifest_json)
            ),
        )
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

        evaluation = _evaluateReleaseChecks(
            row,
            await _checkContext(
                session, row.domain_code, _manifestClassNames(row.manifest_json)
            ),
        )
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

    async def rollback(
        self,
        session: AsyncSession,
        releaseId: int,
        *,
        version: str,
        actor: str,
        departments: tuple[str, ...] | None = None,
        note: str | None = None,
    ) -> SemanticReleaseOperation:
        """回滚到某个历史版本 —— **副本式**（D-11）。

        形态：以目标历史版本为源**新建**一份 DRAFT → VALIDATED → ACTIVE，
        同作用域原有的 ACTIVE 在同一事务里归档。

        为什么不是「把 ARCHIVED 改回 ACTIVE」（状态回退式）：
        那样会让历史行「复活」，审计里「曾经下线」的事实被抹掉，
        且与状态机文档串（ARCHIVED 是终态）冲突。

        为什么副本的 `manifest_hash` 与源**逐字相同**：
        副本直接复制源行的正文与哈希 ⇒ 内容完全一致 ⇒ 回滚的语义就是
        「回到那个内容」，而不是「重新编译一次」（重编译会带上治理表的最新变化）。

        ⚠️ 失败路径同样**先提交证据再抛错**（与 markValidated / activate 一致）：
        闸门不通过时，副本的 check 行与审计必须落库可查；此时副本的版本号会
        换成**合成版本号**（`请求版本#b<副本id>`）⇒ 用户请求的那个版本号空出来
        可以重试（否则重试只会收到「版本已存在」的误导性 409）。
        """
        _validateActor(actor, departments)
        _validateNote(note)
        normalizedVersion = _normalizeVersion(version)
        source = await _lockRelease(session, releaseId)
        _requireRollbackSource(source)
        await _requireVersionFree(session, normalizedVersion, source.domain_code)

        # 复制正文与哈希：回滚 = 回到那个内容，不是重新编译。
        copied = _buildRollbackCopy(source, normalizedVersion, actor, note)
        session.add(copied)
        await session.flush()
        # 副本也是一次「创建」⇒ 必须有 CREATE 审计（否则审计里只有 UPDATE，
        # 查不到「谁创建了这条发布」）。
        await _recordAudit(
            session,
            copied,
            fromStatus=None,
            toStatus=SemanticReleaseStatus.DRAFT.value,
            actor=actor,
            departments=departments,
            extra={
                "rolledFromReleaseId": source.id,
                "manifestHash": source.manifest_hash,
                "requestedVersion": normalizedVersion,
            },
        )

        context = await _checkContext(
            session, source.domain_code, _manifestClassNames(copied.manifest_json)
        )
        evaluation = _evaluateReleaseChecks(copied, context)
        await semanticReleaseCheckService.replaceChecks(
            session, copied.id, evaluation.checks, actor=actor
        )
        if evaluation.failure is not None:
            await _recordBlockedAudit(
                session,
                copied,
                phase="rollback",
                evaluation=evaluation,
                actor=actor,
                departments=departments,
                extra={
                    "rolledFromReleaseId": source.id,
                    "requestedVersion": normalizedVersion,
                },
            )
            copied.version = blockedVersion(normalizedVersion, copied.id)
            await session.commit()
            raise evaluation.failure

        # DRAFT → VALIDATED → ACTIVE：两个状态迁移都在同一事务里，
        # 归档旧 ACTIVE 与激活新副本也是同一事务（不存在「两个 ACTIVE」的窗口）。
        await _activateCopy(session, copied, actor, departments)

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise _mapCreateIntegrityError(exc, normalizedVersion, source.domain_code) from exc

        semanticRegistry.invalidate()
        logger.info(
            "语义发布已回滚（副本式）：源 id=%s 新副本 id=%s version=%r",
            source.id,
            copied.id,
            copied.version,
        )
        return SemanticReleaseOperation(
            release=copied, blockers=(), warnings=evaluation.gate.warnings
        )


semanticReleaseService = SemanticReleaseService()
