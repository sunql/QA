"""M0-P0.4 (Phase 2 Task 7): backfill external_id on 3 new Milvus collections.

For each row with external_id == "" in the 3 new collections (class/property/metric):
1. Derive expected unified_id = ``buildUnifiedId(business_object, external_id)``
2. UPSERT PG id_mapping placeholder（**先写、再读回，以读回结果判定**）
3. Delete original Milvus row (auto-id PK) + re-insert with external_id set

**幂等契约（有机制保证，不是断言出来的）**：脚本结束时，
(a) 被删的旧行必须已对后续 query 可见地消失，且
(b) 新插入的行必须已可见、且带着本次期望的 external_id。
否则「第二次扫描找不到 external_id == "" 的行」这个前提不成立，
重复运行会再造一批重复向量。

Milvus 的 delete 走 ``Proxy → DML channel → DataNode → QueryNode`` 三段异步管道；
``flush()`` 只持久化（Growing → Sealed → 对象存储），**不保证** QueryNode 已装载并
应用 delta binlog ⇒ 脚本在 insert + flush 之后强制 ``release + load``
（见 ``milvus_client.refreshCollection``），并**复扫校验**：任一条后置条件不成立
⇒ 抛错，绝不返回可能撒谎的 written。

⚠️ release + load 期间该集合处于**未加载**状态（约 2-3s）⇒ 该窗口内针对该集合的
检索拿不到正常结果（可能直接失败，也可能等服务端重载）⇒ **必须在维护窗口执行**。
本脚本是一次性维护工具，不用于在线业务路径。

安全姿态（默认只读）
--------------------
- **默认预演**：不加 ``--apply`` 只读不写（PG 与 Milvus 都不写）；
- **生产双闸**：``--apply`` 时 PG 走 ``assertSafeWriteTarget``（库名白名单 +
  APP_ENV），Milvus 走 ``assertSafeMilvusTarget``（生产库 ``default`` 需显式
  ``--allow-prod``）—— Milvus **没有自动备份**，误写不可逆；
- **delete 之前的体检**：集合 schema 缺字段 / 载荷不完整 ⇒ 在任何 delete 之前
  中止（少一个字段 ⇒ 整批 insert 失败 ⇒ 已 delete 的行会全部丢失）。

⚠️ **未决问题（阻塞生产执行）**：``id_mapping.business_object`` 的**大小写 SSOT**
尚未决议（现存 PG 行用小写 ``class``，而 ``reconcile`` / 线上写入路径用的是
``CLASS`` 大写）。本脚本按**字面相等**判定占位行 ⇒ 若不统一，该批会被判成
``conflict`` 而一行都回填不了。**决议前不得对生产执行本脚本。**

分工
----
- ``scripts/milvus_external_id_apply.py`` —— Milvus 侧原语（读行 / 体检 /
  delete / insert / 刷新 / 复扫校验）；
- ``scripts/milvus_external_id_placeholder.py`` —— PG 侧占位行三态判定；
- 本文件 —— 编排（跑哪几个集合、逐行决策、汇总、CLI）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymilvus import Collection
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.milvus_client import (
    _CLASS_COLLECTION_NAME,
    _METRIC_COLLECTION_NAME,
    _PROPERTY_COLLECTION_NAME,
    _connAlias,
    _connect,
)
from scripts.milvus_external_id_apply import (
    _COLUMN_FIELDS,
    _FULL_FIELDS,
    _META_FIELDS,
    BackfillContractError,
    appendRow,
    applyBackfill,
    assertCollectionUsable,
    embeddingDim,
    queryRows,
    resolveTargetRow,
    targetsNeedingBackfill,
)
from scripts.milvus_external_id_placeholder import (
    PLACEHOLDER_CONFLICT,
    PLACEHOLDER_OK,
    ensurePlaceholder,
)

logger = logging.getLogger(__name__)

_TYPE_TO_COLLECTION: dict[str, str] = {
    "class": _CLASS_COLLECTION_NAME,
    "property": _PROPERTY_COLLECTION_NAME,
    "metric": _METRIC_COLLECTION_NAME,
}


@dataclass(frozen=True)
class _PendingBackfill:
    """一次回填的待写入内容 —— 全部来自「PG 占位行可用且载荷完好」的行。"""

    autoIds: list[int]
    expectedUids: list[str]
    columns: dict[str, list]
    pgFailed: int = 0
    pgConflict: int = 0
    vanished: int = 0
    invalid: int = 0

    @property
    def written(self) -> int:
        return len(self.autoIds)

    @property
    def skipped(self) -> int:
        return self.pgFailed + self.pgConflict + self.vanished + self.invalid


@dataclass(frozen=True)
class _BackfillReport:
    """整个脚本的回填结果（供 CLI 汇报用）。"""

    written: int = 0
    skipped: int = 0
    planned: int = 0
    pgFailed: int = 0
    pgConflict: int = 0
    vanished: int = 0
    invalid: int = 0
    failedCollections: int = 0
    errors: tuple[str, ...] = ()


def _warnIfUidOccupied(
    type_: str, expectedUid: str, occupiedUids: set[str], seenUids: set[str]
) -> None:
    """同一个 uid 被多行共用时**明确告警**。

    回填后 ``external_id`` **不保证唯一**：同一 ontology_id 的历史重复行
    （2026-09-30 事故形态：32 个类堆成 98 行）会拿到同一个 external_id。
    本脚本的职责是「一行换一行」，**不做**跨行去重（那是另一件事）⇒ 这里
    只保证运维**看得见**这件事，而不是让它悄悄发生。
    """
    if expectedUid in occupiedUids or expectedUid in seenUids:
        logger.warning(
            "type=%s: uid %s 已出现在其它行上 ⇒ 回填后 external_id 不唯一"
            "（历史重复行；去重需另行收敛）",
            type_,
            expectedUid,
        )
    seenUids.add(expectedUid)


def _resolveTargets(
    type_: str, targets: list[dict], byAutoId: dict[int, dict], expectedDim: int | None
) -> tuple[list, int, int]:
    """纯解析（不碰 PG）：返回 (可用行, invalid 数, vanished 数)。"""
    rows = []
    invalid = 0
    vanished = 0
    for target in targets:
        resolved, reason = resolveTargetRow(target, type_, byAutoId, expectedDim)
        if resolved is None:
            if reason == "vanished":
                vanished += 1
            else:
                invalid += 1
            continue
        rows.append(resolved)
    return rows, invalid, vanished


async def _ensurePlaceholders(
    session: AsyncSession,
    type_: str,
    collectionName: str,
    rows: list,
    occupiedUids: set[str],
    *,
    apply: bool,
) -> tuple[list, int, int]:
    """PG 占位判定：返回 (可回填行, conflict 数, failed 数)。

    - conflict：槽位被别人占用 ⇒ 整行跳过（照写会造成悬垂引用）；
    - failed：PG 状态不明 ⇒ 整行跳过（下次运行会重试）。
    """
    ok = []
    conflict = 0
    failed = 0
    seenUids: set[str] = set()
    for resolved in rows:
        _warnIfUidOccupied(type_, resolved.expectedUid, occupiedUids, seenUids)
        state, detail = await ensurePlaceholder(
            session,
            businessObject=type_,
            externalId=str(resolved.ontologyId),
            expectedUid=resolved.expectedUid,
            collectionName=collectionName,
            ontologyId=resolved.ontologyId,
            apply=apply,
        )
        if state == PLACEHOLDER_CONFLICT:
            logger.warning("type=%s: PG 占位冲突，跳过该行：%s", type_, detail)
            conflict += 1
            continue
        if state != PLACEHOLDER_OK:
            logger.warning("type=%s: PG 占位不可用，跳过该行：%s", type_, detail)
            failed += 1
            continue
        ok.append(resolved)
    return ok, conflict, failed


async def _collectTargets(
    session: AsyncSession,
    type_: str,
    collectionName: str,
    targets: list[dict],
    byAutoId: dict[int, dict],
    occupiedUids: set[str],
    expectedDim: int | None,
    *,
    apply: bool,
) -> _PendingBackfill:
    """逐行处理待回填目标，收集本次要写入的内容。

    只有「载荷完好 **且** PG 占位行可用」的行才会进入本次回填集合：其余整行跳过
    （不删 Milvus 行、不计入 written）⇒ 该行仍待回填，下次运行会重试它。
    """
    rows, invalid, vanished = _resolveTargets(type_, targets, byAutoId, expectedDim)
    ok, conflict, failed = await _ensurePlaceholders(
        session, type_, collectionName, rows, occupiedUids, apply=apply
    )
    autoIds: list[int] = []
    expectedUids: list[str] = []
    columns: dict[str, list] = {field: [] for field in _COLUMN_FIELDS}
    for resolved in ok:
        autoIds.append(resolved.autoId)
        expectedUids.append(resolved.expectedUid)
        appendRow(columns, resolved.full, type_, resolved.expectedUid)

    return _PendingBackfill(
        autoIds=autoIds,
        expectedUids=expectedUids,
        columns=columns,
        pgFailed=failed,
        pgConflict=conflict,
        vanished=vanished,
        invalid=invalid,
    )


def _logSkipSummary(type_: str, pending: _PendingBackfill) -> None:
    """把各类跳过原因分别记账（混在一起的话运维看不出「哪些行永远修不了」）。"""
    if pending.pgFailed:
        logger.warning(
            "type=%s: %d 行 PG 占位不可用，已跳过（未删 Milvus 行，仍待回填）",
            type_, pending.pgFailed,
        )
    if pending.pgConflict:
        logger.warning(
            "type=%s: %d 行 PG 占位冲突，已跳过（未删 Milvus 行；需先处理 PG 侧冲突）",
            type_, pending.pgConflict,
        )
    if pending.vanished:
        logger.info("type=%s: %d 行在两次扫描之间消失，已跳过", type_, pending.vanished)
    if pending.invalid:
        logger.warning("type=%s: %d 行字段不可用或载荷不完整，已跳过", type_, pending.invalid)


def _reportOf(pending: _PendingBackfill, *, planned: int, wrote: bool) -> _BackfillReport:
    return _BackfillReport(
        written=pending.written if wrote else 0,
        skipped=pending.skipped,
        planned=planned if not wrote else 0,
        pgFailed=pending.pgFailed,
        pgConflict=pending.pgConflict,
        vanished=pending.vanished,
        invalid=pending.invalid,
    )


def _mergeReports(reports: list[_BackfillReport]) -> _BackfillReport:
    """把各集合的报告汇总成一份。"""
    return _BackfillReport(
        written=sum(r.written for r in reports),
        skipped=sum(r.skipped for r in reports),
        planned=sum(r.planned for r in reports),
        pgFailed=sum(r.pgFailed for r in reports),
        pgConflict=sum(r.pgConflict for r in reports),
        vanished=sum(r.vanished for r in reports),
        invalid=sum(r.invalid for r in reports),
        failedCollections=sum(r.failedCollections for r in reports),
        errors=tuple(e for r in reports for e in r.errors),
    )


async def _backfillCollection(
    session: AsyncSession, type_: str, collectionName: str, *, apply: bool
) -> _BackfillReport:
    """回填单个集合；返回写入与跳过的行数。"""
    # ① 便宜的元数据扫描（不含 embedding）——找出待回填行（自带 auto-id）。
    metaRows = queryRows(collectionName, _META_FIELDS)
    targets = targetsNeedingBackfill(metaRows)
    if not targets:
        logger.info("type=%s: no rows need backfill", type_)
        return _BackfillReport()

    logger.info("type=%s: %d rows need backfill", type_, len(targets))

    # ② 全量扫描（带 embedding）——按 auto-id 索引，不按 ontology_id。
    fullRows = queryRows(collectionName, _FULL_FIELDS)
    byAutoId = {r["id"]: r for r in fullRows if r.get("id") is not None}
    occupiedUids = {r.get("external_id") for r in metaRows if r.get("external_id")}

    # ③ 集合体检 —— 必须在任何 delete 之前。
    collection = Collection(collectionName, using=_connAlias())
    assertCollectionUsable(collection, collectionName)
    expectedDim = embeddingDim(collection)

    # ④ 逐行处理：载荷完好且 PG 占位行可用才纳入本次回填。
    pending = await _collectTargets(
        session, type_, collectionName, targets, byAutoId, occupiedUids,
        expectedDim, apply=apply,
    )

    # ⑤ PG 先落盘：Milvus 的写不可回滚，PG 的占位行必须先持久化，
    #    否则「回填后 PG 与 Milvus 对不上」只留下一条日志。
    if apply:
        await session.commit()

    # ⑥ Milvus 变更 + 复扫校验。
    if pending.written:
        if not apply:
            logger.info("type=%s: 预演 —— 将回填 %d 行（未写入）", type_, pending.written)
        else:
            await applyBackfill(
                collection, collectionName, pending.autoIds, pending.expectedUids,
                pending.columns,
            )
            logger.info("type=%s: backfilled %d rows", type_, pending.written)

    _logSkipSummary(type_, pending)
    return _reportOf(pending, planned=pending.written, wrote=apply)


async def runBackfill(session: AsyncSession, *, apply: bool) -> _BackfillReport:
    """回填三个集合；返回写入与跳过的行数（跳过必须让调用方知道）。

    - ``apply=False``：预演，只读不写（``planned`` 是「将要写几行」）；
    - ``apply=True``：真正写。

    单个集合的基础设施类失败**不打断**其它集合（错误进 ``errors``）；但
    ``BackfillContractError``（幂等契约 / 数据安全前提被破坏）**必须中止**。
    """
    _connect()
    reports: list[_BackfillReport] = []
    for type_, collectionName in _TYPE_TO_COLLECTION.items():
        try:
            reports.append(
                await _backfillCollection(session, type_, collectionName, apply=apply)
            )
        except BackfillContractError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单集合失败隔离，错误必须被记录
            logger.exception("type=%s: 集合回填失败，已跳过该集合", type_)
            reports.append(
                _BackfillReport(
                    failedCollections=1,
                    errors=(f"{collectionName}: {type(exc).__name__}: {exc}",),
                )
            )
    return _mergeReports(reports)


async def backfill(session: AsyncSession) -> int:
    """Backfill external_id for rows where it's empty. Returns rows actually written."""
    return (await runBackfill(session, apply=True)).written


def _parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="回填 Milvus 本体集合的 external_id")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加只预演（默认只读）",
    )
    parser.add_argument(
        "--allow-prod",
        action="store_true",
        help="目标判定为生产时，--apply 必须显式带上（先完成当日备份）",
    )
    return parser.parse_args(argv)


def _describeTargets(settings) -> str:
    """目标的人类可读描述（口令一律不出现）。"""
    from scripts.kpi_metric_db_guard import databaseNameOf, maskDsn
    from scripts.milvus_write_target_guard import describeMilvusTarget

    pgPart = (
        f"pg={maskDsn(settings.databaseUrl)}"
        f"/{databaseNameOf(settings.databaseUrl) or '?'}"
    )
    milvusPart = describeMilvusTarget(settings.milvusUri, settings.milvusDbName)
    return f"{pgPart}  milvus={milvusPart}"


async def main(argv: list[str] | None = None) -> int:
    """CLI 入口：连默认 DB（读 DATABASE_URL）。默认预演，--apply 才写。

    退出码：0 = 全部成功；1 = 有行被跳过 / 有集合失败；2 = 被闸门拦下。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parseArgs(argv)

    from app.config import getSettings
    from app.infrastructure.database import getSessionFactory
    from app.infrastructure.milvus_client import getDbName
    from scripts.backfill_id_mapping import assertSafeWriteTarget
    from scripts.milvus_write_target_guard import assertSafeMilvusTarget

    settings = getSettings()
    milvusDbName = getDbName()
    print(f"[backfill_milvus_external_id] 目标：{_describeTargets(settings)}")

    if args.apply:
        try:
            assertSafeWriteTarget(
                appEnv=settings.appEnv,
                databaseUrl=settings.databaseUrl,
                allowProdWrite=args.allow_prod,
            )
            assertSafeMilvusTarget(milvusDbName, allowProdWrite=args.allow_prod)
        except SystemExit as exc:
            print(f"[backfill_milvus_external_id] {exc}", file=sys.stderr)
            return 2

    factory = getSessionFactory()
    async with factory() as session:
        report = await runBackfill(session, apply=args.apply)

    _printSummary(report, applied=args.apply)
    if report.errors:
        for message in report.errors:
            print(f"[backfill_milvus_external_id] 集合失败：{message}", file=sys.stderr)
        return 1
    if report.skipped or report.failedCollections:
        return 1
    return 0


def _printSummary(report: _BackfillReport, *, applied: bool) -> None:
    """打印汇总；**跳过必须让调用方看见**（否则自动化任务会把「还有行没回填」记成成功）。"""
    if applied:
        print(
            f"[backfill_milvus_external_id] 已回填 {report.written} 行"
            f"（跳过 {report.skipped}：PG 冲突 {report.pgConflict}、PG 不可用 {report.pgFailed}、"
            f"行消失 {report.vanished}、字段不可用 {report.invalid}）"
        )
    else:
        print(
            f"[backfill_milvus_external_id] 预演（未写入）：计划回填 {report.planned} 行；"
            f"确认后加 --apply"
        )
    if report.failedCollections:
        print(
            f"[backfill_milvus_external_id] {report.failedCollections} 个集合处理失败（见 stderr）",
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
