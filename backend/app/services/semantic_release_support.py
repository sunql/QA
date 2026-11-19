"""语义发布写路径的**内部支撑层**（与 `semantic_release_service` 配套）。

为什么单独一个模块：`semantic_release_service` 里的 `SemanticReleaseService`
只负责**编排**（编译 → 切片 → 落库 → 状态迁移 → 审计），而「输入校验 /
状态机判定 / 校验项评估 / 审计留痕」这一层是**纯支撑**。两者混在一个文件里时
文件会超过 800 行（项目上限），且支撑层的改动会淹没编排层的可读性。

分层方向（**单向**，无环）：
    semantic_release_checks ──► semantic_release_domain_checks ──► check_types
                    ▲
    semantic_release_support（支撑层：类型 + 纯函数 + 落库辅助）
                    ▲
    semantic_release_service（编排层：对外的唯一写入口）

⚠️ `ReleaseEvaluation` 住在**支撑层**而不是编排层：编排层要 import 支撑层，
若这个类型留在编排层 ⇒ 支撑层反过来 import 编排层 ⇒ **循环 import**。
状态机判定（`isTransitionAllowed`）则相反 —— 它留在编排层（与状态机文档串同处），
支撑层不需要它 ⇒ 单向依赖成立。
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
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
    validateManifest,
)
from app.domain.semantic_release_models import SemanticRelease
from app.services.audit_service import AuditService
from app.services.messages_zh import (
    MSG_SEMANTIC_RELEASE_ACTOR_INVALID,
    MSG_SEMANTIC_RELEASE_CHECK_CONSTRAINT,
    MSG_SEMANTIC_RELEASE_DEPARTMENTS_TOO_LONG,
    MSG_SEMANTIC_RELEASE_DOMAIN_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_GATE_BLOCKED,
    MSG_SEMANTIC_RELEASE_ID_INVALID,
    MSG_SEMANTIC_RELEASE_NOT_FOUND,
    MSG_SEMANTIC_RELEASE_NOTE_TOO_LONG,
    MSG_SEMANTIC_RELEASE_ROLLBACK_NOT_SLICE,
    MSG_SEMANTIC_RELEASE_ROLLBACK_SCHEMA_MISMATCH,
    MSG_SEMANTIC_RELEASE_ROLLBACK_SOURCE_INVALID,
    MSG_SEMANTIC_RELEASE_VERSION_CONFLICT,
    MSG_SEMANTIC_RELEASE_VERSION_INVALID,
)
from app.services.procurement_binding_candidates import summarizeCandidateBindings
from app.services.semantic_compiler import compileManifest
from app.services.semantic_compiler_bindings import SourceBindingCompileError
from app.services.semantic_domain_slice import (
    sliceManifestForRelease,
    sliceManifestToDomain,
)
from app.services.semantic_registry_service import (
    MAX_RELEASE_ID,
    SemanticReleaseIntegrityError,
    verifySnapshotIntegrity,
)
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
)
from app.services.semantic_release_domain_checks import ReleaseCheckContext

_MAX_VERSION_LENGTH = 32  # 与 semantic_release.version 列宽一致
_MAX_ACTOR_LENGTH = 64  # semantic_release.created_by 列宽（audit_log.actor 是 100，取更严的）
_MAX_DEPARTMENTS_LENGTH = 500  # audit_log.actor_departments 列宽
_MAX_NOTE_LENGTH = 2000  # semantic_release.note 是 Text（无列宽）⇒ 服务层自己闸住
_BLOCKED_VERSION_MARK = "#b"  # 被闸门拦下的回滚副本的版本号后缀标记

_AUDIT_ENTITY = "semantic_release"


@dataclass(frozen=True, slots=True)
class ReleaseEvaluation:
    """一次发布校验的结论：逐项校验项 + 闸门报告 + 失败原因。

    `failure` 非空 ⇒ 本次状态迁移必须被拒绝，且**异常类型沿用既有契约**
    （完整性 / 契约错 = 原异常上抛；闸门阻断 = `ConflictError` 409）。
    """

    checks: tuple[PublishCheck, ...] = ()
    gate: PublishGateResult = PublishGateResult()
    failure: Exception | None = None


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


def _sliceIfDomain(
    manifest: dict[str, Any], domainCode: str | None
) -> dict[str, Any]:
    """域发布 ⇒ 存**切片**；全局发布 ⇒ 原样返回（D-10）。

    切片的三个要点（口径见 `semantic_domain_slice`）：
    1. 业务对象挂域 → 类（+父类闭包）→ JOIN → 指标 → KPI；
    2. **引用闭合补齐**：切片内指标引用到的类、有绑定类的 JOIN 邻居
       （取不动点）及其父类闭包一并补进来 ⇒ 快照满足引用契约；
    3. `domains` 分区取「本域 + 切片内业务对象引用到的域」。

    ⚠️ 切片是**幂等**的：对切片再切一次得到同一份 ⇒ 发布校验项可以只依赖
    已存快照复算（否则「跨域补齐了什么」在 validate 阶段就复算不出来）。
    """
    if domainCode is None:
        return manifest
    return sliceManifestToDomain(manifest, domainCode)


def _validateManifestOrReject(manifest: dict[str, Any]) -> None:
    """校验 manifest 契约；不合法 ⇒ **422** 而不是 500。

    为什么必须翻译：`ManifestSchemaError` 是 `ValueError`（不是 `DomainError`）
    ⇒ `main.py` 只注册了 `DomainError` 的处理器 ⇒ 裸抛会变成 **500**。
    而「manifest 不合契约」是**治理内容问题**，调用方能改（去改本体/绑定），
    必须给一条说得清「哪一条不满足」的 422。
    """
    try:
        validateManifest(manifest)
    except ManifestSchemaError as exc:
        raise ValidationError(f"发布快照不满足 manifest 契约：{exc}") from exc


async def _checkContext(
    session: AsyncSession,
    domainCode: str | None,
    scopeClassNames: Sequence[str] | None = None,
) -> ReleaseCheckContext:
    """校验上下文（域编码 + 活库里的候选绑定事实）。

    候选数只能实时查（候选不进快照 ⇒ 快照里没有这个事实）。

    `scopeClassNames`：本次发布快照里的类名 ⇒ 候选统计**按发布域收窄**
    （否则采购域发布会把别的域的候选债算进自己的报告 ⇒ 报告与快照两个宇宙）。
    """
    summary = await summarizeCandidateBindings(
        session, scopeClassNames=scopeClassNames
    )
    return ReleaseCheckContext(
        domainCode=domainCode,
        candidateBindingCount=summary.count,
        candidateClassNames=summary.classNames,
    )


async def _requireVersionFree(
    session: AsyncSession, version: str, domainCode: str | None
) -> None:
    """版本号在**同一作用域内**唯一。

    - 全局作用域（`domain_code IS NULL`）：库侧 `uq_semantic_release_global_version`；
    - 域作用域：库侧 `uq_semantic_release_domain_version`（version + domain_code）
      ⇒ **不同域可以用同一个版本号**，同一域内不行（版本号要能标识「哪一次发布」）。

    ⚠️ 域作用域以前不查重（ORM 注释写的是「按域发布上线时再补索引」）；
    那个时点就是现在 ⇒ 服务层查重 + 库侧索引双保险。
    """
    statement = select(SemanticRelease.id).where(SemanticRelease.version == version)
    if domainCode is None:
        statement = statement.where(SemanticRelease.domain_code.is_(None))
    else:
        statement = statement.where(SemanticRelease.domain_code == domainCode)
    duplicate = await session.scalar(statement)
    if duplicate is not None:
        raise ConflictError(
            MSG_SEMANTIC_RELEASE_VERSION_CONFLICT.format(version=version)
        )


def _validateNote(note: str | None) -> None:
    """备注长度闸（`semantic_release.note` 是 `Text` ⇒ 库里没有列宽兜底）。

    ⚠️ 不截断：截断会静默改写审计里的人类可读说明 ⇒ 宁可 422 拒绝一次写。
    """
    if note is None:
        return
    if not isinstance(note, str) or len(note) > _MAX_NOTE_LENGTH:
        raise ValidationError(
            MSG_SEMANTIC_RELEASE_NOTE_TOO_LONG.format(max=_MAX_NOTE_LENGTH)
        )


def blockedVersion(requested: str, releaseId: int) -> str:
    """被闸门拦下的回滚副本的版本号（释放用户请求的那个版本号）。

    为什么要有这个「合成版本号」：失败路径必须**先提交证据再抛错**
    （check 行 + 审计），而 check 行以 `release_id` 为外键 ⇒ 副本行必须存在
    ⇒ 副本行占着用户请求的版本号 ⇒ 用户拿同一个版本号重试会收到
    「版本已存在」的 409（**误导**：真实原因是「上一次回滚被闸门拦下了」）。

    ⇒ 失败时把副本的版本号换成 `请求版本#b<副本id>`：用户请求的版本号空出来
    可以重试，而证据行仍然在（带合成版本号，可辨认）。
    合成版本号**长度受列宽约束**（32）⇒ 必要时截断前缀（**不截断**哈希部分：
    副本 id 保证了唯一性）。
    """
    suffix = f"{_BLOCKED_VERSION_MARK}{releaseId}"
    keep = _MAX_VERSION_LENGTH - len(suffix)
    return f"{requested[:keep]}{suffix}"


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


def _draftChecks(
    manifest: dict[str, Any], context: ReleaseCheckContext
) -> tuple[PublishCheck, ...]:
    """草稿编译时的校验项集合。

    编译产物刚刚规范化并算过哈希 ⇒ 完整性**必然通过**（这里仍然显式落一行，
    让「草稿阶段查了什么」与「validate 阶段查了什么」是同一套目录）。
    """
    return capChecks(
        (
            _integrityCheck(True, "编译产物已规范化并计算哈希（manifest_hash 与正文一致）"),
            *evaluateManifestChecks(manifest, context=context),
        )
    )


def _evaluateReleaseChecks(
    row: SemanticRelease, context: ReleaseCheckContext
) -> ReleaseEvaluation:
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
            *evaluateManifestChecks(manifest, context=context),
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


# ---------------------------------------------------------------------------
# 内部：回滚（副本式，D-11）
# ---------------------------------------------------------------------------


def _requireRollbackSource(source: SemanticRelease) -> None:
    """回滚来源的三条硬约束（任一不满足 ⇒ 409，不静默降级）。

    1. **不能是 DRAFT**：草稿从未发布过 ⇒ 回滚是「回到已发布的内容」；
    2. **manifest 版本必须与当前一致**：跨版本回滚会让读路径拿到旧契约的
       快照（v1 没有 `sourceBindings` 分区 ⇒ 采购绑定凭空消失）⇒ 拒绝，
       而不是「先回滚上去再说」；
    3. **域发布的源必须是该域的有效切片**：已存正文与「重切结果」的类集合
       不一致 ⇒ 这份正文不是合法的域切片（多半是更早的算法写的）⇒ 拒绝。
    """
    if source.status == SemanticReleaseStatus.DRAFT.value:
        raise ConflictError(
            MSG_SEMANTIC_RELEASE_ROLLBACK_SOURCE_INVALID.format(
                releaseId=source.id, current=source.status
            )
        )
    if source.schema_version != SCHEMA_VERSION:
        raise ConflictError(
            MSG_SEMANTIC_RELEASE_ROLLBACK_SCHEMA_MISMATCH.format(
                releaseId=source.id,
                source=source.schema_version,
                current=SCHEMA_VERSION,
            )
        )
    if source.domain_code is not None and not _isDomainSlice(source):
        raise ConflictError(
            MSG_SEMANTIC_RELEASE_ROLLBACK_NOT_SLICE.format(releaseId=source.id)
        )


def _isDomainSlice(source: SemanticRelease) -> bool:
    """已存正文是否就是「该域的切片」（对已存正文再切一次 ⇒ 类集合相同）。

    切片是幂等的 ⇒ 这一条是**可复算**的判据：不需要额外存「这是不是切片」
    的标记，重切一次就知道。
    """
    stored = _manifestClassNames(source.manifest_json)
    recomputed = sliceManifestForRelease(source.manifest_json, source.domain_code or "")
    return set(recomputed.classNames) == set(stored)


def _buildRollbackCopy(
    source: SemanticRelease, version: str, actor: str, note: str | None
) -> SemanticRelease:
    """构造回滚副本：正文与哈希**逐字复制**源行。

    ⚠️ 正文必须**深拷贝**：`manifest_json` 是 JSONB，SQLAlchemy **不做可变
    追踪** ⇒ 不拷贝的话源行与副本行在内存里绑定同一个 dict ⇒ 任何就地改写
    都是**静默脏数据**（库里两行都不更新，内存里两行一起变）。
    """
    return SemanticRelease(
        version=version,
        domain_code=source.domain_code,
        status=SemanticReleaseStatus.DRAFT.value,
        schema_version=source.schema_version,
        manifest_json=copy.deepcopy(source.manifest_json),
        manifest_hash=source.manifest_hash,
        created_by=actor,
        note=note,
    )


async def _activateCopy(
    session: AsyncSession,
    copied: SemanticRelease,
    actor: str,
    departments: tuple[str, ...] | None,
) -> None:
    """副本 DRAFT → VALIDATED → ACTIVE；同作用域的旧 ACTIVE 一并归档。

    全程**同一事务** ⇒ 不存在「两个 ACTIVE」的窗口（库侧唯一索引也不会被撞）。
    """
    await _applyStatus(
        session, copied, SemanticReleaseStatus.VALIDATED, actor, departments
    )
    previousActives = await _lockActivesInScope(session, copied)
    for previous in previousActives:
        await _applyStatus(
            session, previous, SemanticReleaseStatus.ARCHIVED, actor, departments
        )
    await session.flush()
    await _applyStatus(session, copied, SemanticReleaseStatus.ACTIVE, actor, departments)
    copied.activated_time = datetime.now(UTC)


def _manifestClassNames(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """manifest 里的类名（发布快照的作用域）。"""
    classes = manifest.get("classes")
    if not isinstance(classes, list):
        return ()
    return tuple(
        str(cls["className"])
        for cls in classes
        if isinstance(cls, Mapping) and isinstance(cls.get("className"), str)
    )


async def _recordAudit(
    session: AsyncSession,
    row: SemanticRelease,
    *,
    fromStatus: str | None,
    toStatus: str,
    actor: str,
    departments: tuple[str, ...] | None,
    extra: dict[str, Any] | None = None,
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
        after={**_auditPayload(row, toStatus), **(extra or {})},
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
    extra: dict[str, Any] | None = None,
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
            **(extra or {}),
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
