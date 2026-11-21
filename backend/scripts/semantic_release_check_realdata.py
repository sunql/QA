"""M1-1.2 发布校验落表（`semantic_release_check`）的真实数据验证脚本。

在**真实治理数据**上验证「发布校验落表」这件事，而不是在 fixture 上自说自话：

1. **真实清单评估**：从活治理表编译一份 manifest（真实类 / 真实指标 / 真实 SQL），
   跑逐项校验 ⇒ 结论里的指标名、源类名全是库里的真名。
2. **闸门零漂移**：逐项结论聚合出来的闸门报告与既有 `evaluatePublishGate`
   **完全相等**（真实数据上再验一次口径没被「逐项化」改坏）。
3. **落表 → 回读**：走**真实写路径**（createDraft → validate → activate），
   校验结论落进 `semantic_release_check`，回读行数与校验项数**逐条相等**；
   再评估一次 ⇒ 覆盖写 ⇒ 行数不变（不是追加）。
4. **缺陷样本 → 不得激活**：把真实清单里一条比率指标翻成「已发布」但仍缺落点
   （= 治理库里的真实事故形态：指标上线了，落点类还没配），重算哈希 ⇒
   **完整性通过、闸门阻断** ⇒ activate 必须 409，且 `semantic_release_check`
   里能查到那条 ERROR 行（M1-a DoD 第 3 条）。
   **失败尝试也留痕**：`audit_log` 里必须有一条 `blocked=true` 的审计行
   （check 表是覆盖写的 ⇒ 只看 check 表无法回答「上次是哪一次被拦下的」）。
5. **收尾不留痕**：删掉本次建的发布行 ⇒ 校验证据随之 CASCADE ⇒ 库回到跑之前的样子。

**幂等**：重复跑不报错、不残留（每次用带时间戳的版本号，结束时自删；启动时先清
上一轮的残留）。
**审计行保留**：`audit_log` 按设计是 append-only（应用层从不 UPDATE/DELETE），
脚本留下的审计痕迹不清理，actor 固定为 `realdata-script` 以便识别。

用法：
    cd backend
    DATABASE_URL='postgresql+asyncpg://qa_user:***@127.0.0.1:5433/qa_metadata' \\
        uv run python -m scripts.semantic_release_check_realdata --allow-prod-write
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from scripts.kpi_metric_db_guard import assertSafeTarget, databaseNameOf

# 脚本身份：审计与 check 表的 checked_by 都用它 ⇒ 事后一眼认出是脚本留下的行。
_ACTOR = "realdata-script"
_DEPARTMENTS = ("M1-1.2-真机验证",)
# 版本号前缀：启动时用它清上一轮的残留（脚本崩在半路也不会留垃圾）。
# ⚠️ `semantic_release.version` 列宽 32 ⇒ 前缀 + 时间戳必须留出余量。
_VERSION_PREFIX = "rd-m112"
# 写入用的数据域：采购域（M1 的目标域）。**不改全局域** ⇒ 不会顶替现役全局快照。
_DOMAIN_CODE = "PROCUREMENT"
# 报告里最多列多少条明细（闸门报告本身封顶 20 条）。
_MAX_REPORTED = 20
# 比率类指标（缺陷样本的载体：已发布比率缺落点 ⇒ 硬阻断）。
_RATIO_TYPES = frozenset({"RATIO", "DIFFERENCE"})


class VerifyFailure(Exception):
    """一项断言不成立。"""


def _silenceSqlEcho() -> None:
    """关掉 SQL 回显（dev 下 engine `echo=True`），免得把 JSON 报告淹没。

    SQLAlchemy 的 echo 由**实例 logger** 决定（`InstanceLogger` 直接按 echo
    映射成 INFO 并调 `logger._log()`，**绕过** logger 层级）⇒ 改 logger level
    不管用，只能摘掉它自动挂上的默认 handler。
    """
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    engineLogger = logging.getLogger("sqlalchemy.engine.Engine")
    for handler in list(engineLogger.handlers):
        engineLogger.removeHandler(handler)


def _versionSuffix() -> str:
    """版本号后缀（时间戳）⇒ 同一秒内重跑也不撞版本唯一约束。"""
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def _countBy(metrics: list[dict[str, Any]], field: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for metric in metrics:
        key = str(metric.get(field))
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# 阶段 A：真实清单评估（只读）
# ---------------------------------------------------------------------------


def _countsByCode(checks: tuple[Any, ...]) -> dict[str, dict[str, int]]:
    """按校验项编码汇总（总数 / 通过 / 失败）。"""
    summary: dict[str, dict[str, int]] = {}
    for check in checks:
        item = summary.setdefault(check.code, {"total": 0, "passed": 0, "failed": 0})
        item["total"] += 1
        if check.result == "PASS":
            item["passed"] += 1
        else:
            item["failed"] += 1
    return dict(sorted(summary.items()))


def _failedSamples(checks: tuple[Any, ...], limit: int = _MAX_REPORTED) -> list[dict[str, str]]:
    """失败明细样本（真实指标名 / 真实类名）。"""
    samples = [
        {
            "code": check.code,
            "severity": check.severity,
            "targetKey": check.targetKey or "",
            "detail": check.detail,
        }
        for check in checks
        if check.result == "FAIL"
    ]
    return samples[:limit]


async def _evaluatePhase(session) -> tuple[dict[str, Any], dict[str, Any]]:
    """从活治理表编译 manifest，跑逐项校验，并与既有闸门口径比对。

    返回 (报告, manifest) —— manifest 只在内存里流转，不进 JSON 报告（体积 ~1MB）。
    """
    from app.services.semantic_compiler import compileManifest
    from app.services.semantic_release_checks import (
        checksToGateResult,
        evaluatePublishChecks,
    )
    from app.services.semantic_release_service import evaluatePublishGate

    manifest = await compileManifest(session)
    checks = evaluatePublishChecks(manifest)
    gate = checksToGateResult(checks)
    legacyGate = evaluatePublishGate(manifest)
    if gate != legacyGate:
        raise VerifyFailure(
            "真实数据上闸门口径漂移：逐项聚合结果与 evaluatePublishGate 不相等"
        )

    metrics = manifest.get("metrics") or []
    if not metrics:
        raise VerifyFailure(
            "真实治理库里没有指标（manifest.metrics 为空）⇒ 本次验证无意义；"
            "确认目标库是否已灌治理数据"
        )

    report = {
        "manifest": {
            "classes": len(manifest.get("classes") or []),
            "joins": len(manifest.get("joins") or []),
            "metrics": len(metrics),
            "kpis": len(manifest.get("kpis") or []),
            "domains": len(manifest.get("domains") or []),
        },
        "metricStatuses": _countBy(metrics, "status"),
        "metricTypes": _countBy(metrics, "metricType"),
        "checksByCode": _countsByCode(checks),
        "checkTotal": len(checks),
        "gate": {
            "blockers": len(gate.blockers),
            "warnings": len(gate.warnings),
            "canPublish": gate.canPublish,
        },
        "gateMatchesLegacy": True,
        "failedSamples": _failedSamples(checks),
    }
    return report, manifest


# ---------------------------------------------------------------------------
# 阶段 B：缺陷样本矩阵（纯内存，覆盖各失败路径）
# ---------------------------------------------------------------------------


def _injectDefect(manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """把一条真实比率指标翻成「已发布但仍缺落点」⇒ 闸门必须硬阻断。

    这是治理库里的真实事故形态：指标在本体里上线了，分子/分母的**落点类**
    还没配 ⇒ 比率不可执行 ⇒ 不许发布。
    """
    defect = deepcopy(manifest)
    target = next(
        (
            metric
            for metric in defect.get("metrics") or []
            if metric.get("metricType") in _RATIO_TYPES
        ),
        None,
    )
    if target is None:
        raise VerifyFailure("真实清单里没有比率类指标 ⇒ 造不出「已发布比率缺落点」缺陷样本")
    target["status"] = "PUBLISHED"
    numerator = target.get("numeratorMeasure")
    if isinstance(numerator, dict):
        numerator["className"] = None
    else:
        target["numeratorMeasure"] = {"agg": "SUM", "expr": None, "className": None}
    return defect, str(target.get("metricName"))


def _defectMatrix(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """缺陷样本 ⇒ 期望结论（覆盖各条失败路径）。

    期望值写死在脚本里 ⇒ 校验逻辑改口径时这里会红（真实数据的护栏）。
    """
    from app.services.semantic_release_checks import evaluatePublishChecks

    cases: list[dict[str, Any]] = []

    defect, defectName = _injectDefect(manifest)
    errorRows = [
        check
        for check in evaluatePublishChecks(defect)
        if check.severity == "ERROR"
        and check.result == "FAIL"
        and check.targetKey == defectName
    ]
    cases.append(
        {
            "case": "已发布比率缺分子落点",
            "target": defectName,
            "expect": "METRIC_NUMERATOR_LANDING / ERROR / FAIL",
            "actual": [f"{c.code}/{c.severity}/{c.result}" for c in errorRows],
            "ok": any(check.code == "METRIC_NUMERATOR_LANDING" for check in errorRows),
        }
    )

    noSource = deepcopy(manifest)
    noSourceMetric = next(
        (metric for metric in noSource.get("metrics") or [] if metric.get("sources")),
        None,
    )
    if noSourceMetric is not None:
        noSourceMetric["sources"] = []
        sourceRows = [
            check
            for check in evaluatePublishChecks(noSource)
            if check.code == "METRIC_SOURCE_PRESENT"
            and check.targetKey == noSourceMetric.get("metricName")
        ]
        cases.append(
            {
                "case": "指标无源类引用",
                "target": str(noSourceMetric.get("metricName")),
                "expect": "METRIC_SOURCE_PRESENT / WARNING / FAIL",
                "actual": [f"{c.code}/{c.severity}/{c.result}" for c in sourceRows],
                "ok": bool(sourceRows)
                and sourceRows[0].severity == "WARNING"
                and sourceRows[0].result == "FAIL",
            }
        )

    return cases


# ---------------------------------------------------------------------------
# 阶段 C/D：落表 → 回读 → 缺陷阻断（真实写路径）
# ---------------------------------------------------------------------------


async def _persistPhase(session, version: str, releaseIds: list[int]) -> dict[str, Any]:
    """真实写路径：草稿 → 校验 → 激活，并把校验结论回读出来。"""
    from app.services.semantic_release_check_service import semanticReleaseCheckService
    from app.services.semantic_release_service import semanticReleaseService

    draft = await semanticReleaseService.createDraft(
        session,
        version=version,
        actor=_ACTOR,
        domainCode=_DOMAIN_CODE,
        departments=_DEPARTMENTS,
        note="M1-1.2 真机验证（脚本自建，结束时自删）",
    )
    # 立刻登记 ⇒ 后续任何一步抛错，收尾仍能把它删掉（不留半路残骸）。
    releaseIds.append(int(draft.release.id))
    draftRows = await semanticReleaseCheckService.listChecks(session, draft.release.id)
    if not draftRows:
        raise VerifyFailure("草稿落库后校验结论表里没有行 ⇒ 落表没生效")

    await semanticReleaseService.markValidated(
        session, draft.release.id, actor=_ACTOR, departments=_DEPARTMENTS
    )
    validatedRows = await semanticReleaseCheckService.listChecks(session, draft.release.id)
    if len(validatedRows) != len(draftRows):
        raise VerifyFailure(
            f"重新评估后校验结论行数变了（应为覆盖写）：{len(draftRows)} → {len(validatedRows)}"
        )

    activated = await semanticReleaseService.activate(
        session, draft.release.id, actor=_ACTOR, departments=_DEPARTMENTS
    )
    if activated.release.status != "ACTIVE":
        raise VerifyFailure(f"激活后状态不是 ACTIVE：{activated.release.status}")

    activatedRows = await semanticReleaseCheckService.listChecks(session, draft.release.id)
    if len(activatedRows) != len(validatedRows):
        raise VerifyFailure(
            f"激活后校验结论行数变了（应为覆盖写）：{len(validatedRows)} → {len(activatedRows)}"
        )

    return {
        "releaseId": draft.release.id,
        "version": version,
        "domainCode": _DOMAIN_CODE,
        "status": activated.release.status,
        "checkRows": len(activatedRows),
        "checkCodes": sorted({row.check_code for row in activatedRows}),
        "summary": {
            "passed": sum(1 for row in activatedRows if row.result == "PASS"),
            "failed": sum(1 for row in activatedRows if row.result == "FAIL"),
            "errorRows": sum(
                1
                for row in activatedRows
                if row.result == "FAIL" and row.severity == "ERROR"
            ),
        },
    }


async def _defectPhase(
    session, version: str, manifest: dict[str, Any], releaseIds: list[int]
) -> dict[str, Any]:
    """缺陷发布：完整性通过但闸门阻断 ⇒ activate 必须失败，且结论可查。"""
    from app.domain.exceptions import ConflictError
    from app.domain.semantic_manifest_schema import manifestHash
    from app.domain.semantic_release_models import SemanticRelease
    from app.services.semantic_release_check_service import semanticReleaseCheckService
    from app.services.semantic_release_service import semanticReleaseService

    draft = await semanticReleaseService.createDraft(
        session,
        version=version,
        actor=_ACTOR,
        domainCode=_DOMAIN_CODE,
        departments=_DEPARTMENTS,
        note="M1-1.2 真机验证（缺陷样本，结束时自删）",
    )
    releaseIds.append(int(draft.release.id))
    await semanticReleaseService.markValidated(
        session, draft.release.id, actor=_ACTOR, departments=_DEPARTMENTS
    )

    defectManifest, defectName = _injectDefect(manifest)
    row = await session.get(SemanticRelease, draft.release.id)
    if row is None:
        raise VerifyFailure(f"发布 {draft.release.id} 取不到（不应发生）")
    row.manifest_json = defectManifest
    # 重算哈希 ⇒ **完整性通过**（不是「快照坏了」），阻断原因只可能来自发布闸门。
    row.manifest_hash = manifestHash(defectManifest)
    await session.commit()

    blocked = False
    conflictMessage = ""
    try:
        await semanticReleaseService.activate(
            session, draft.release.id, actor=_ACTOR, departments=_DEPARTMENTS
        )
    except ConflictError as exc:
        blocked = True
        conflictMessage = str(exc)
    if not blocked:
        raise VerifyFailure("缺陷发布竟然激活成功 ⇒ 发布闸门失效")

    await session.refresh(row)
    if row.status != "VALIDATED":
        raise VerifyFailure(f"缺陷发布被拦下后状态不是 VALIDATED：{row.status}")

    rows = await semanticReleaseCheckService.listChecks(session, draft.release.id)
    errorRows = [item for item in rows if item.severity == "ERROR" and item.result == "FAIL"]
    if not errorRows:
        raise VerifyFailure("缺陷发布被拦下，但校验结论表里查不到 ERROR 行 ⇒ 证据没落表")

    blockedAudits = await _blockedAuditRows(session, draft.release.id)
    if not blockedAudits:
        raise VerifyFailure(
            "缺陷发布被拦下，但 audit_log 里没有 blocked 留痕 ⇒ "
            "「上次是哪一次被拦下的」将随 check 表覆盖写而彻底消失"
        )

    return {
        "releaseId": draft.release.id,
        "version": version,
        "defectMetric": defectName,
        "activationBlocked": True,
        "statusAfterBlock": row.status,
        "conflictMessage": conflictMessage[:300],
        "blockedAudits": blockedAudits,
        "errorRows": [
            {"code": item.check_code, "targetKey": item.target_key, "detail": item.detail}
            for item in errorRows
        ],
    }


async def _blockedAuditRows(session, releaseId: int) -> list[dict[str, Any]]:
    """该发布上「失败尝试」的审计行（`after_json.blocked == true`）。

    check 表是**覆盖写**的 ⇒ 上一轮的 FAIL 行会被下一轮删掉；
    唯一能回答「那次被哪一条拦下的」的地方就是审计行。
    """
    from sqlalchemy import select

    from app.domain.models import AuditLog

    rows = (
        await session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "semantic_release",
                AuditLog.entity_id == releaseId,
            )
        )
    ).scalars().all()

    blocked: list[dict[str, Any]] = []
    for audit in rows:
        after = audit.after_json or {}
        if after.get("blocked") is True:
            blocked.append(
                {
                    "action": audit.action,
                    "phase": after.get("phase"),
                    "blockerCount": after.get("blockerCount"),
                    "status": after.get("status"),
                }
            )
    return blocked


# ---------------------------------------------------------------------------
# 阶段 E：收尾（不留痕）
# ---------------------------------------------------------------------------


async def _cleanupPhase(session, releaseIds: list[int]) -> dict[str, Any]:
    """删掉本次建的发布行 ⇒ 校验证据 CASCADE 消失 ⇒ 库回到跑之前的样子。"""
    from app.domain.semantic_release_check_models import SemanticReleaseCheck
    from app.domain.semantic_release_models import SemanticRelease
    from app.services.semantic_registry_service import semanticRegistry

    if not releaseIds:
        return {"deletedReleases": 0, "remainingCheckRows": 0}

    await session.execute(delete(SemanticRelease).where(SemanticRelease.id.in_(releaseIds)))
    await session.commit()
    remaining = await session.scalar(
        select(func.count())
        .select_from(SemanticReleaseCheck)
        .where(SemanticReleaseCheck.release_id.in_(releaseIds))
    )
    if remaining:
        raise VerifyFailure(f"发布已删但校验证据还在（{remaining} 行）⇒ CASCADE 没生效")
    semanticRegistry.invalidate()
    return {"deletedReleases": len(releaseIds), "remainingCheckRows": int(remaining or 0)}


async def _purgeLeftovers(session) -> int:
    """清上一轮残留（脚本崩在半路时的垃圾）：按版本号前缀删。"""
    from app.domain.semantic_release_models import SemanticRelease

    rows = list(
        (
            await session.execute(
                select(SemanticRelease).where(
                    SemanticRelease.version.like(f"{_VERSION_PREFIX}%")
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0
    await session.execute(
        delete(SemanticRelease).where(SemanticRelease.id.in_([row.id for row in rows]))
    )
    await session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def _runPhases(
    sessionMaker, suffix: str, releaseIds: list[int]
) -> dict[str, Any]:
    """跑四个阶段（评估 / 缺陷矩阵 / 落表 / 缺陷阻断），返回报告。

    `releaseIds` 由调用方持有并**就地追加** ⇒ 中途抛错时收尾仍能删掉已建的发布行。
    """
    report: dict[str, Any] = {}
    async with sessionMaker() as session:
        purged = await _purgeLeftovers(session)
        if purged:
            print(f"[semantic_release_check_realdata] 清理上一轮残留发布：{purged} 行")

        evaluateReport, manifest = await _evaluatePhase(session)
        report["evaluate"] = evaluateReport
        report["defectMatrix"] = _defectMatrix(manifest)

        persist = await _persistPhase(session, f"{_VERSION_PREFIX}-{suffix}", releaseIds)
        report["persist"] = persist

        defect = await _defectPhase(
            session, f"{_VERSION_PREFIX}-defect-{suffix}", manifest, releaseIds
        )
        report["defect"] = defect
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description="M1-1.2 发布校验落表真实数据验证")
    parser.add_argument(
        "--allow-prod-write",
        action="store_true",
        help="允许写正式库 qa_metadata（prod 同源；默认拒绝）",
    )
    args = parser.parse_args()

    envValue = os.environ.get("DATABASE_URL", "").strip()
    if not envValue:
        print(
            "[semantic_release_check_realdata] 未设置 DATABASE_URL 环境变量。"
            "本脚本要连元数据库，请显式指定（口令走环境变量，不要写进仓库）。",
            file=sys.stderr,
        )
        return 2

    from app.config import getSettings

    settings = getSettings()
    databaseUrl = settings.databaseUrl
    dbName = databaseNameOf(databaseUrl)
    print(f"[semantic_release_check_realdata] 元数据库：{dbName}（不打印凭据）")

    try:
        assertSafeTarget(databaseUrl, allowProdWrite=args.allow_prod_write)
    except SystemExit as exc:
        print(f"[semantic_release_check_realdata] {exc}", file=sys.stderr)
        return 2

    from app.infrastructure.database import getSessionFactory

    sessionMaker = getSessionFactory()
    _silenceSqlEcho()

    suffix = _versionSuffix()
    releaseIds: list[int] = []
    report: dict[str, Any] = {}
    exitCode = 0
    try:
        report = await _runPhases(sessionMaker, suffix, releaseIds)
    except (VerifyFailure, SQLAlchemyError) as exc:
        print(f"[semantic_release_check_realdata] FAIL: {exc}", file=sys.stderr)
        exitCode = 1
    except Exception as exc:  # noqa: BLE001 - 脚本化退出，不抛栈
        print(
            f"[semantic_release_check_realdata] FAIL: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        exitCode = 1

    # 收尾无条件执行：脚本崩在半路也不能把发布行留在库里。
    try:
        async with sessionMaker() as session:
            report["cleanup"] = await _cleanupPhase(session, releaseIds)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[semantic_release_check_realdata] FAIL（收尾失败，库里可能残留 "
            f"{releaseIds}）：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if exitCode != 0:
        return exitCode

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        "[semantic_release_check_realdata] PASS：真实清单评估 + 落表回读 + "
        "缺陷阻断（不得激活）+ 收尾无残留"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
